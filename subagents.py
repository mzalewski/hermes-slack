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
    return _flag("SLACK_SUBAGENT_VIEW")


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
        self._notify(rec, "stop")

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
    def _subagent_task_chunks(self, stream: Any) -> list[dict[str, Any]]:
        if not subagent_panel_enabled():
            return []
        recs = TRACKER.for_chat(stream.channel, stream.thread_ts, flat=not stream.thread_ts)[-6:]
        chunks = []
        for rec in recs:
            status = {"complete": "complete", "failed": "error", "interrupted": "error"}.get(rec.status, "in_progress")
            if rec.live:
                bits = [f"{rec.tool_count} tool{'s' if rec.tool_count != 1 else ''}"]
                if rec.last_tool:
                    bits.append(rec.last_tool)
                if rec.status == "stopping":
                    bits.append("stopping…")
                details = " · ".join(bits)
            else:
                dur = f"{int(rec.ended - rec.started)}s" if rec.ended else ""
                details = " · ".join(b for b in (f"{rec.tool_count} tools", dur, rec.status if status == "error" else "") if b)
            chunks.append({"type": "task_update", "id": f"sub-{rec.subagent_id}"[:64],
                           "title": f"🔀 {rec.goal or 'Subagent'}"[:120], "status": status, "details": details[:300]})
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
