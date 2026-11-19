"""Subagent monitoring and controls for Slack.

Observation uses only public plugin hooks (``subagent_start``, ``subagent_stop``, ``pre_tool_call``,
``post_tool_call``, ``pre_llm_call``) plus ``gateway.session_context.get_session_env`` to learn which
Slack chat a turn belongs to. Each tracked subagent is shown as an extra row on the adapter's native
task card (quiet, throttled) and, in Code channels, in a "Subagents" view tab with Stop / Result
buttons (updated only when a subagent starts or finishes, to avoid notification noise).

Stop uses ``tools.delegate_tool_registry.interrupt_subagent``. That module is not on Hermes's public
plugin API list, so it is imported lazily, guarded, and gated by ``SLACK_SUBAGENT_CONTROLS``.
Subagents spawned by ``delegate_task`` are not controllable through ``ctx.subagent_lifecycle``
(its handles only cover children it launched itself).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from .task_labels import friendly_title
except ImportError:  # flat import (tests)
    from task_labels import friendly_title  # type: ignore

logger = logging.getLogger(__name__)

_MAX_RECORDS = 200
_MAX_TARGETS = 512
_VIEW_KEY = "hermes-subagents"
_VIEW_MIN_INTERVAL = 10.0
STOP_ACTION = "hermes_subagent_stop"
RESULT_ACTION = "hermes_subagent_result"


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def subagent_panel_enabled() -> bool:
    return _flag("SLACK_SUBAGENT_PANEL")


def subagent_view_enabled() -> bool:
    return _flag("SLACK_SUBAGENT_VIEW", "false")


def subagent_controls_enabled() -> bool:
    return _flag("SLACK_SUBAGENT_CONTROLS")


@dataclass
class SubagentRecord:
    subagent_id: str
    goal: str
    role: str
    parent_session_id: str
    child_session_id: str
    chat_id: str
    thread_ts: str
    status: str = "running"  # running | stopping | complete | failed | interrupted
    tool_count: int = 0
    last_tool: str = ""
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    summary: str = ""
    kind: str = "hermes"  # "hermes" (delegate_task child) | "claude" (Claude Code run launched by Hermes)
    log: list = field(default_factory=list)  # one plain line per step; Slack appends `details`, so only new lines are sent
    open_calls: dict = field(default_factory=dict)  # Claude Code tool_use_id -> label, for calls with no result yet
    pid: int = 0  # Claude Code's process id (for a future per-run Stop)
    cwd: str = ""

    @property
    def live(self) -> bool:
        return self.status in ("running", "stopping")


def _session_env() -> tuple[str, str, str]:
    """(platform, chat_id, thread_id) of the turn running in this context; blanks when unknown."""
    try:
        from gateway.session_context import get_session_env
        return (str(get_session_env("HERMES_SESSION_PLATFORM", "") or ""),
                str(get_session_env("HERMES_SESSION_CHAT_ID", "") or ""),
                str(get_session_env("HERMES_SESSION_THREAD_ID", "") or ""))
    except Exception:
        return "", "", ""


def _arg_preview(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "query", "command", "url", "goal", "pattern", "name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:48]
    return ""


def _closing_line(rec: "SubagentRecord") -> str:
    secs = f" in {int(rec.ended - rec.started)}s" if rec.ended and rec.ended > rec.started else ""
    return {"complete": f"✓ Done{secs}", "failed": f"✗ Failed{secs}", "interrupted": f"■ Stopped{secs}"}.get(rec.status, "")


def _short_title(goal: str, limit: int = 60) -> str:
    """A prompt is often a paragraph: keep its first sentence-ish, cut at a word boundary."""
    text = " ".join(str(goal or "").split())
    first = re.split(r"(?<=[.!?:])\s", text, maxsplit=1)[0]
    if len(first) > limit:
        first = first[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return first


def _short_error(error: Any) -> str:
    """First line of a tool error, cut at the first run of output; "Exit code 1" stays as is."""
    text = " ".join(str(error or "").split())
    match = re.match(r"(Exit code \d+)", text)
    if match:
        return match.group(1)
    return text[:60]


def _claude_preview(tool_input: Any, cwd: str) -> str:
    """Short, readable argument for a Claude Code tool call (paths relative to the working directory)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "notebook_path", "path", "pattern", "command", "url", "query", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            value = " ".join(value.split())
            if cwd:
                prefix = cwd.rstrip("/") + "/"
                value = value[len(prefix):] if value.startswith(prefix) else value.replace(prefix, "")
            return value[:60]
    return ""


def _normalise_status(raw: Any) -> str:
    text = str(raw or "").lower()
    if any(w in text for w in ("interrupt", "cancel", "stop")):
        return "interrupted"
    if any(w in text for w in ("fail", "error", "timeout")):
        return "failed"
    return "complete"


class SubagentTracker:
    """Thread-safe record of subagents whose parent turn is in a Slack chat. Hooks may fire on any
    thread, so everything here is lock-guarded, bounded and exception-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: "OrderedDict[str, SubagentRecord]" = OrderedDict()
        self._by_session: dict[str, str] = {}
        self._targets: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._claude_current: dict[str, str] = {}
        self._adapters: "weakref.WeakSet[Any]" = weakref.WeakSet()

    # ── adapters ──
    def attach(self, adapter: Any) -> None:
        self._adapters.add(adapter)

    def _notify(self, rec: SubagentRecord, kind: str) -> None:
        for adapter in list(self._adapters):
            try:
                adapter._subagent_changed(rec, kind)
            except Exception:
                logger.debug("[Slack] subagent notify failed", exc_info=True)

    # ── queries ──
    def get(self, subagent_id: str) -> Optional[SubagentRecord]:
        with self._lock:
            return self._records.get(str(subagent_id))

    def for_chat(self, chat_id: str, thread_ts: str = "", flat: bool = True) -> list[SubagentRecord]:
        """Records for a chat; a threaded card only shows its own thread's subagents."""
        with self._lock:
            return [r for r in self._records.values()
                    if r.chat_id == chat_id and (flat or r.thread_ts == thread_ts)]

    def mark(self, subagent_id: str, status: str) -> Optional[SubagentRecord]:
        with self._lock:
            rec = self._records.get(str(subagent_id))
            if rec is not None and rec.live:
                rec.status = status
            return rec

    # ── hook callbacks ──
    def remember_turn_target(self, session_id: str) -> None:
        platform, chat_id, thread = _session_env()
        if platform != "slack" or not chat_id or not session_id:
            return
        with self._lock:
            self._targets[session_id] = (chat_id, thread)
            self._targets.move_to_end(session_id)
            while len(self._targets) > _MAX_TARGETS:
                self._targets.popitem(last=False)

    def on_start(self, **kw: Any) -> None:
        sid = str(kw.get("child_subagent_id") or "")
        if not sid:
            return
        platform, chat_id, thread = _session_env()
        if platform != "slack" or not chat_id:
            chat_id, thread = self._targets.get(str(kw.get("parent_session_id") or ""), ("", ""))
        if not chat_id:
            return  # not a Slack turn
        rec = SubagentRecord(
            subagent_id=sid, goal=" ".join(str(kw.get("child_goal") or "").split())[:300],
            role=str(kw.get("child_role") or ""), parent_session_id=str(kw.get("parent_session_id") or ""),
            child_session_id=str(kw.get("child_session_id") or ""), chat_id=chat_id, thread_ts=thread)
        with self._lock:
            self._records[sid] = rec
            if rec.child_session_id:
                self._by_session[rec.child_session_id] = sid
            while len(self._records) > _MAX_RECORDS:
                _, old = self._records.popitem(last=False)
                self._by_session.pop(old.child_session_id, None)
        logger.info("[Slack] subagent %s started in chat=%s thread=%s", sid, chat_id, thread or "-")
        self._notify(rec, "start")

    def on_stop(self, **kw: Any) -> None:
        """``subagent_stop`` carries ``child_session_id`` (not the subagent id), ``child_status``,
        ``child_summary`` and ``duration_ms``; resolve the record by session id."""
        with self._lock:
            sid = str(kw.get("child_subagent_id") or "") or self._by_session.get(str(kw.get("child_session_id") or ""), "")
            rec = self._records.get(sid)
            if rec is None:
                return
            rec.status = _normalise_status(kw.get("child_status") or kw.get("status") or kw.get("outcome"))
            rec.summary = str(kw.get("child_summary") or "")[:2000]
            duration_ms = kw.get("duration_ms")
            rec.ended = rec.started + duration_ms / 1000.0 if isinstance(duration_ms, (int, float)) and duration_ms > 0 else time.time()
            rec.log.append(_closing_line(rec))
        logger.info("[Slack] subagent %s finished: status=%s (raw=%r) tools=%d chat=%s", rec.subagent_id, rec.status,
                    kw.get("child_status"), rec.tool_count, rec.chat_id)
        self._notify(rec, "stop")

    # ── Claude Code (events written by claude_code_hook.py, read by claude_code.py) ──
    def _claude_record(self, session_id: str, chat: str, thread: str, new_prompt: Optional[str], t: float) -> Optional[SubagentRecord]:
        """The record for a Claude Code session's current prompt. ``new_prompt`` (a UserPromptSubmit) opens
        a fresh record; any other event reuses the session's current one, creating it if events began mid-run."""
        with self._lock:
            current = self._claude_current.get(session_id)
            rec = self._records.get(current) if current else None
            if new_prompt is None and rec is not None:
                return rec
            if new_prompt is None and rec is None and not session_id:
                return None
            rid = f"cc-{session_id[:10] or 'x'}-{int(t) % 100000}"
            rec = SubagentRecord(
                subagent_id=rid, goal=" ".join((new_prompt or "").split())[:300], role="claude-code",
                parent_session_id="", child_session_id=session_id, chat_id=chat, thread_ts=thread,
                kind="claude", started=t or time.time())
            self._records[rid] = rec
            self._claude_current[session_id] = rid
            while len(self._records) > _MAX_RECORDS:
                _, old = self._records.popitem(last=False)
                self._claude_current.pop(old.child_session_id, None)
            return rec

    def on_claude_event(self, item: dict) -> None:
        """Apply one hook event: PreToolUse adds a log line, PostToolUseFailure an error line, Stop /
        SessionEnd close the run and mark anything still open as not run."""
        ev = item.get("event") or {}
        name = str(ev.get("hook_event_name") or "")
        chat, thread = str(item.get("chat") or ""), str(item.get("thread") or "")
        sid, t = str(ev.get("session_id") or ""), float(item.get("t") or time.time())
        if not chat or name not in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"):
            return
        fresh = name == "UserPromptSubmit"
        rec = self._claude_record(sid, chat, thread, str(ev.get("prompt") or "") if fresh else None, t)
        if rec is None:
            return
        notify = None
        with self._lock:
            rec.pid = int(item.get("pid") or rec.pid or 0)
            rec.cwd = str(ev.get("cwd") or rec.cwd)
            if fresh:
                notify = "start"
            elif name == "PreToolUse" and rec.live:
                tool = str(ev.get("tool_name") or "tool")
                preview = _claude_preview(ev.get("tool_input"), rec.cwd)
                rec.last_tool = friendly_title(f"{tool} - {preview}" if preview else tool)
                rec.open_calls[str(ev.get("tool_use_id") or rec.last_tool)] = rec.last_tool
                rec.log.append(rec.last_tool)
                rec.tool_count += 1
                notify = "tool"
            elif name == "PostToolUse":
                rec.open_calls.pop(str(ev.get("tool_use_id") or ""), None)
            elif name == "PostToolUseFailure":
                rec.open_calls.pop(str(ev.get("tool_use_id") or ""), None)
                error = _short_error(ev.get("error"))
                rec.log.append("✗ Failed" + (f": {error}" if error else ""))  # the line above already names the step
                notify = "tool"
            elif name in ("Stop", "SessionEnd") and rec.live:
                for label in rec.open_calls.values():
                    rec.log.append(f"✗ Not run: {label}")  # e.g. a call that was denied: it never gets a result event
                rec.open_calls.clear()
                rec.status = "complete" if name == "Stop" else "interrupted"
                rec.summary = str(ev.get("last_assistant_message") or rec.summary)[:2000]
                rec.ended = t
                rec.log.append(_closing_line(rec))
                logger.info("[Slack] claude-code run %s finished: %s tools=%d chat=%s", rec.subagent_id, name, rec.tool_count, chat)
                notify = "stop"
        if notify:
            self._notify(rec, notify)

    def on_tool(self, **kw: Any) -> None:
        with self._lock:
            sid = str(kw.get("task_id") or "")
            rec = self._records.get(sid)
            if rec is None:
                rec = self._records.get(self._by_session.get(str(kw.get("session_id") or ""), ""))
            if rec is None or not rec.live:
                return
            rec.tool_count += 1
            tool = str(kw.get("tool_name") or "tool")
            preview = _arg_preview(kw.get("args"))
            rec.last_tool = friendly_title(f"{tool} - {preview}" if preview else tool)
            rec.log.append(rec.last_tool)
        self._notify(rec, "tool")


TRACKER = SubagentTracker()


def register_hooks(ctx: Any) -> None:
    """Register the observer hooks. Every callback is cheap, fail-open and ignores non-Slack turns."""
    def guard(fn):
        def wrapper(**kw: Any) -> None:
            if not subagent_panel_enabled():
                return
            try:
                fn(**kw)
            except Exception:
                logger.debug("[Slack] subagent hook failed", exc_info=True)
        return wrapper

    ctx.register_hook("pre_llm_call", guard(lambda **kw: TRACKER.remember_turn_target(str(kw.get("session_id") or ""))))
    ctx.register_hook("subagent_start", guard(TRACKER.on_start))
    ctx.register_hook("subagent_stop", guard(TRACKER.on_stop))
    ctx.register_hook("pre_tool_call", guard(TRACKER.on_tool))


_ICONS = {"running": "⏳", "stopping": "⏹️", "complete": "✅", "failed": "❌", "interrupted": "⏹️"}


class SlackSubagentMixin:
    """Adapter side: card rows, the Subagents view tab, and Stop / Result buttons."""

    def _init_subagent_state(self) -> None:
        self._subagent_loop: Optional[asyncio.AbstractEventLoop] = None
        self._subagent_view_last: dict[str, float] = {}
        self._subagent_view_pending: dict[str, Any] = {}
        self._claude_poll_task: Optional[Any] = None
        TRACKER.attach(self)

    # ── from hooks (any thread) ──
    def _subagent_changed(self, rec: SubagentRecord, kind: str) -> None:
        loop = self._subagent_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._subagent_apply(rec, kind)))

    async def _subagent_apply(self, rec: SubagentRecord, kind: str) -> None:
        try:
            await self._refresh_subagent_cards(rec)
            if kind in ("start", "stop"):
                await self._publish_subagent_view(rec.chat_id)
        except Exception:
            logger.debug("[Slack] subagent update failed", exc_info=True)

    # ── card rows ──
    @staticmethod
    def _rows_for(stream: Any) -> list[SubagentRecord]:
        """Runs that overlapped this card's lifetime: still live, or finished after the card opened.
        (Without this, every finished run in a channel would reappear on all later cards.)"""
        opened = getattr(stream, "created_wall", 0.0) - 2.0
        return [r for r in TRACKER.for_chat(stream.channel, stream.thread_ts, flat=not stream.thread_ts)
                if r.live or r.ended >= opened]

    def _live_subagent_rows(self, stream: Any) -> list[SubagentRecord]:
        if not subagent_panel_enabled():
            return []
        return [r for r in self._rows_for(stream) if r.live]

    # ── Claude Code events ──
    def _ensure_claude_poller(self) -> None:
        """Poll the hook event files while a card is open (cheap: one directory scan per second)."""
        try:
            from .claude_code import claude_code_enabled
        except ImportError:  # flat import (tests)
            from claude_code import claude_code_enabled  # type: ignore
        if not claude_code_enabled():
            return
        task = self._claude_poll_task
        if task is None or task.done():
            self._claude_poll_task = asyncio.ensure_future(self._claude_poll_loop())

    async def _claude_poll_loop(self) -> None:
        try:
            from .claude_code import EventReader
        except ImportError:
            from claude_code import EventReader  # type: ignore
        reader = EventReader(TRACKER)
        reader.cleanup()
        idle = 0
        while True:
            try:
                reader.poll()
            except Exception:
                logger.debug("[Slack] claude-code poll failed", exc_info=True)
            idle = 0 if self._native_task_card_streams else idle + 1
            if idle > 30:  # no card open for 30s: nothing to render
                return
            await asyncio.sleep(1.0)

    def _subagent_task_chunks(self, stream: Any) -> list[dict[str, Any]]:
        """One ``task_update`` row per tracked run. Slack APPENDS ``details`` across updates, so a row's
        ``details`` carries only the log lines this card hasn't been sent yet (``stream.log_cursor``);
        the cursor is committed by the caller after a successful send (``stream.cursor_next``)."""
        stream.cursor_next = {}
        stream.started_next = set()
        started = getattr(stream, "log_started", None)
        if started is None:  # plain stand-in objects: infer from the cursor
            started = {k for k, v in stream.log_cursor.items() if v}
        if not subagent_panel_enabled():
            return []
        recs = self._rows_for(stream)[-6:]
        closing = getattr(stream, "closing", False)
        chunks = []
        for rec in recs:
            lines = list(rec.log)
            status = {"complete": "complete", "failed": "error", "interrupted": "error"}.get(rec.status, "in_progress")
            if rec.live and closing:
                # The card is closing but the run keeps going: never leave a row in progress.
                status = "complete"
                lines.append("↪ " + (getattr(stream, "closing_note", "") or "Running in background"))
            elif rec.live and rec.status == "stopping":
                lines.append("⏹ Stopping…")
            sent = stream.log_cursor.get(rec.subagent_id, 0)
            fresh = lines[sent:]
            stream.cursor_next[rec.subagent_id] = len(lines)
            if rec.kind == "claude":
                title = "🤖 Claude Code" + (f": {_short_title(rec.goal)}" if rec.goal else "")
            else:
                title = f"🔀 {rec.goal or 'Subagent'}"
            chunk = {"type": "task_update", "id": f"sub-{rec.subagent_id}"[:64], "title": title[:120], "status": status}
            if fresh:
                # Slack appends `details`: a row's first lines on THIS card start clean, later ones on a new line.
                chunk["details"] = ("\n" if rec.subagent_id in started else "") + "\n".join(fresh)
                stream.started_next.add(rec.subagent_id)
            chunks.append(chunk)
        return chunks

    async def _refresh_subagent_cards(self, rec: SubagentRecord) -> None:
        for stream in list(self._native_task_card_streams.values()):
            if (stream.channel == rec.chat_id and not stream.stopped and stream.stream_ts and stream.last_tasks
                    and (not stream.thread_ts or stream.thread_ts == rec.thread_ts)):
                await self.send_native_task_card_progress(
                    stream.channel, stream.last_tasks, title=stream.last_title,
                    reply_to=stream.last_reply_to, metadata=stream.last_metadata)

    # ── view tab (Code channels) ──
    def _subagent_view_blocks(self, chat_id: str) -> list[dict[str, Any]]:
        recs = TRACKER.for_chat(chat_id)[-10:]
        blocks: list[dict[str, Any]] = []
        for rec in recs:
            line = f"{_ICONS.get(rec.status, '•')} *{rec.goal or 'Subagent'}*\n{rec.tool_count} tools · {rec.status}"
            if rec.live and rec.last_tool:
                line += f" · {rec.last_tool}"
            block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": line[:2900]}}
            if rec.live and subagent_controls_enabled():
                block["accessory"] = {"type": "button", "text": {"type": "plain_text", "text": "Stop"},
                                      "action_id": STOP_ACTION, "value": rec.subagent_id, "style": "danger"}
            elif not rec.live:
                block["accessory"] = {"type": "button", "text": {"type": "plain_text", "text": "Result"},
                                      "action_id": RESULT_ACTION, "value": rec.subagent_id}
            blocks.append(block)
        return blocks

    async def _publish_subagent_view(self, chat_id: str) -> None:
        if not (subagent_view_enabled() and self._is_known_code_channel(chat_id)):
            return
        wait = _VIEW_MIN_INTERVAL - (time.monotonic() - self._subagent_view_last.get(chat_id, 0.0))
        if wait > 0:  # coalesce bursts (e.g. several children finishing together) into one update
            if chat_id not in self._subagent_view_pending:
                self._subagent_view_pending[chat_id] = asyncio.ensure_future(self._publish_view_later(chat_id, wait))
            return
        blocks = self._subagent_view_blocks(chat_id)
        if not blocks:
            return
        self._subagent_view_last[chat_id] = time.monotonic()
        try:
            await self._get_client(chat_id).api_call("agents.conversations.setView", json={
                "channel_id": chat_id, "type": "block_kit", "view_key": _VIEW_KEY, "name": "Subagents",
                "blocks": blocks})
        except Exception as exc:
            logger.debug("[Slack] subagent setView failed: %s", exc, exc_info=True)

    async def _publish_view_later(self, chat_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._subagent_view_pending.pop(chat_id, None)
        await self._publish_subagent_view(chat_id)

    # ── buttons ──
    async def _ephemeral(self, rec: SubagentRecord, user_id: str, text: str) -> None:
        try:
            await self._get_client(rec.chat_id).chat_postEphemeral(channel=rec.chat_id, user=user_id, text=text)
        except Exception:
            logger.debug("[Slack] subagent ephemeral failed", exc_info=True)

    async def _handle_subagent_action(self, ack, body, action) -> None:
        """Stop / Result button. Authorised like approval buttons; only subagents this adapter's own
        chats started are addressable, and the clicker must also be allowed in that chat."""
        started = await self._begin_interaction(ack, body, action, "subagent")
        if started is None:
            return
        _team, action_id, value, _msg, _ts, _channel, _name, user_id = started
        rec = TRACKER.get(value)
        if rec is None or self._slash_channel_gated(rec.chat_id) or self._early_reject_unauthorized(
                user_id, rec.chat_id, False, thread_id=rec.thread_ts or None):
            logger.info("[Slack] subagent action %s by %s ignored (unknown, gated or unauthorized)", action_id, user_id)
            return
        if action_id == RESULT_ACTION:
            text = rec.summary or "No result text was captured for this subagent."
            await self._ephemeral(rec, user_id, f"*{rec.goal[:80] or 'Subagent'}* ({rec.status})\n{text[:2500]}")
            return
        if not subagent_controls_enabled():
            await self._ephemeral(rec, user_id, "Subagent controls are disabled.")
            return
        if not rec.live:
            await self._ephemeral(rec, user_id, "That subagent has already finished.")
            return
        try:
            from tools.delegate_tool_registry import interrupt_subagent
            accepted = await asyncio.to_thread(interrupt_subagent, rec.subagent_id)
        except Exception as exc:
            logger.warning("[Slack] subagent stop unavailable: %s", exc)
            await self._ephemeral(rec, user_id, "Stopping subagents isn't available in this Hermes version.")
            return
        if accepted:
            TRACKER.mark(rec.subagent_id, "stopping")
            logger.info("[Slack] subagent %s stop requested by %s", rec.subagent_id, user_id)
            await self._subagent_apply(rec, "stop")
            await self._ephemeral(rec, user_id, "Stop requested. The subagent stops at its next step.")
        else:
            await self._ephemeral(rec, user_id, "Couldn't stop that subagent (it may have just finished).")
