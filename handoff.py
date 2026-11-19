"""Hand a project/coding task from a chat to a project bot, in a new Slack Code channel.

Hermes (the dispatcher) calls the ``slack_code_channel`` tool. The tool finds the delegate bot's Slack
adapter (all profiles share one gateway process; each adapter registers itself in a process-wide registry
below), and that adapter, acting as the delegate, creates ``#<project>-<task>`` linked back to the
requesting message (``agents.conversations.create``) and starts the delegate's first turn by dispatching
the task as the requesting user's first message in the new channel, exactly as if they had typed it.

Slack makes the *calling* bot the channel's agent, which is why creation runs inside the delegate's own
adapter and token, not the dispatcher's.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import types
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "slack_code_channel"
TOOLSET = "slack-handoff"
_REGISTRY_MODULE = "hermes_slack_handoff_registry"  # one module object shared by every profile's plugin copy
_NAME_MAX = 80
_CALL_TIMEOUT = 90.0


def handoff_enabled() -> bool:
    return os.getenv("SLACK_HANDOFF", "true").strip().lower() not in ("0", "false", "no", "off")


# ── process-wide registry: profile name -> that profile's Slack adapter ─────────────────────────────
def _registry() -> dict:
    module = sys.modules.get(_REGISTRY_MODULE)
    if module is None:
        module = sys.modules.setdefault(_REGISTRY_MODULE, types.ModuleType(_REGISTRY_MODULE))
    if not hasattr(module, "adapters"):
        module.adapters = {}
    return module.adapters


def _profile_key(profile: Optional[str]) -> str:
    name = (profile or "").strip()
    return "default" if name in ("", "default", "main") else name


def register_adapter(adapter: Any) -> None:
    _registry()[_profile_key(getattr(adapter, "_owner_profile", None))] = adapter


def unregister_adapter(adapter: Any) -> None:
    reg = _registry()
    for key, value in list(reg.items()):
        if value is adapter:
            reg.pop(key, None)


# ── projects, names ──────────────────────────────────────────────────────────────────────────────────
def discover_projects(profiles_dir: Optional[Path] = None) -> dict[str, str]:
    """``{project: profile}``: every ``<project>-bot`` profile directory, plus ``SLACK_HANDOFF_PROJECTS``
    (``"curate=curate-bot,sip=sip-bot"``) which wins on conflict."""
    projects: dict[str, str] = {}
    root = profiles_dir or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "profiles"
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and entry.name.endswith("-bot") and len(entry.name) > 4:
                projects[entry.name[:-4]] = entry.name
    except OSError:
        pass
    for pair in os.environ.get("SLACK_HANDOFF_PROJECTS", "").split(","):
        project, _, profile = pair.partition("=")
        if project.strip() and profile.strip():
            projects[project.strip().lower()] = profile.strip()
    return projects


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:limit].rstrip("-")


def channel_name(project: str, task_name: str) -> str:
    return f"{slugify(project, 20)}-{slugify(task_name) or 'task'}"[:_NAME_MAX]


def _result(ok: bool, **fields: Any) -> str:
    return json.dumps({"ok": ok, **fields})


# ── the tool ─────────────────────────────────────────────────────────────────────────────────────────
def tool_schema(projects: Optional[dict[str, str]] = None) -> dict:
    known = projects if projects is not None else discover_projects()
    listing = ", ".join(sorted(known)) or "(none configured)"
    return {
        "name": TOOL_NAME,
        "description": (
            "Hand a coding or project task over to a project bot in its own new Slack Code channel, named "
            "<project>-<task>. The bot gets the task as its first message and works on it there; you stay "
            "available for general chat. Use it for substantial work belonging to a project (code changes, "
            "bugs, PRs, backlog items), not for quick questions. Only works from Slack. "
            f"Projects: {listing}."),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": f"The project to hand the task to. One of: {listing}."},
                "task_name": {"type": "string", "description": "2-5 words naming the task, used in the channel name, e.g. 'fix login redirect'."},
                "task": {"type": "string", "description": "The complete instructions for the project bot: goal, relevant details from the conversation, and what done looks like. It sees nothing else."},
            },
            "required": ["project", "task_name", "task"],
        },
    }


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return os.environ.get(name, "")


def _dispatchers() -> set[str]:
    raw = os.environ.get("SLACK_HANDOFF_FROM", "default")
    return {_profile_key(p) for p in raw.split(",") if p.strip()}


def handoff_tool(args: dict, **_kwargs: Any) -> str:
    """Tool handler. Never raises; always returns a JSON string."""
    try:
        if not handoff_enabled():
            return _result(False, error="Handing off to Code channels is disabled.")
        if _session_env("HERMES_SESSION_PLATFORM") != "slack":
            return _result(False, error="This only works from a Slack conversation.")
        here = _profile_key(_session_env("HERMES_SESSION_PROFILE"))
        if here not in _dispatchers():
            return _result(False, error="This bot isn't set up to hand work over; do the task yourself.")
        project = str((args or {}).get("project") or "").strip().lower()
        task_name = str((args or {}).get("task_name") or "").strip()
        task = str((args or {}).get("task") or "").strip()
        if not (project and task_name and task):
            return _result(False, error="project, task_name and task are all required.")
        projects = discover_projects()
        if project not in projects:
            return _result(False, error=f"Unknown project '{project}'. Available: {', '.join(sorted(projects)) or 'none'}.")
        delegate = _registry().get(_profile_key(projects[project]))
        loop = getattr(delegate, "_main_loop", None) if delegate is not None else None
        if delegate is None or loop is None or loop.is_closed():
            return _result(False, error=f"The {projects[project]} bot isn't connected to Slack right now.")
        try:
            if asyncio.get_running_loop() is loop:
                return _result(False, error="Cannot hand off from the delegate's own event loop.")
        except RuntimeError:
            pass  # no loop in this thread: the normal case for a tool call
        origin_chat, origin_ts, user = (_session_env("HERMES_SESSION_CHAT_ID"), _session_env("HERMES_SESSION_MESSAGE_ID"),
                                        _session_env("HERMES_SESSION_USER_ID"))
        name = channel_name(project, task_name)
        session_id = f"handoff-{origin_chat}-{origin_ts or 'x'}-{slugify(task_name)}"[:120]
        future = asyncio.run_coroutine_threadsafe(delegate.handoff_create(
            name=name, session_id=session_id, task=task, origin_channel=origin_chat, origin_ts=origin_ts,
            user_id=user), loop)
        outcome = future.result(timeout=_CALL_TIMEOUT)
        return json.dumps({**outcome, "delegate": projects[project], "project": project})
    except Exception as exc:
        logger.warning("[Slack] slack_code_channel failed", exc_info=True)
        return _result(False, error=f"Handoff failed: {exc}")


# ── adapter side ─────────────────────────────────────────────────────────────────────────────────────
class SlackHandoffMixin:
    """Runs inside the DELEGATE bot's adapter: creates the channel as that bot and starts its first turn."""

    def _init_handoff_state(self) -> None:
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._handoffs: dict[str, str] = {}  # session_id -> channel id (a retry never dispatches the task twice)

    def _handoff_online(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        register_adapter(self)

    def _handoff_offline(self) -> None:
        unregister_adapter(self)
        self._main_loop = None

    async def handoff_create(self, *, name: str, session_id: str, task: str, origin_channel: str,
                             origin_ts: str, user_id: str) -> dict:
        if not user_id or self._early_reject_unauthorized(user_id, origin_channel or "", False):
            return {"ok": False, "error": "The requesting user isn't authorised to use this bot."}
        if session_id in self._handoffs:  # same request again: same channel, and the task is already running
            channel = self._handoffs[session_id]
            return {"ok": True, "channel_id": channel, "channel_mention": f"<#{channel}>", "name": name, "repeat": True}
        client = self._get_client(origin_channel or "")
        payload: dict[str, Any] = {"name": name, "session_id": session_id}
        if origin_channel and origin_ts:
            payload.update(origin_channel_id=origin_channel, origin_message_ts=origin_ts)
        try:
            created = await client.api_call("agents.conversations.create", json=payload)
        except Exception as exc:
            if "origin_channel_id" not in payload:
                return {"ok": False, "error": f"Slack refused to create the channel: {exc}"}
            logger.info("[Slack] handoff create with origin failed (%s); retrying without the origin link", exc)
            payload.pop("origin_channel_id"), payload.pop("origin_message_ts")
            try:
                created = await client.api_call("agents.conversations.create", json=payload)
            except Exception as exc2:
                return {"ok": False, "error": f"Slack refused to create the channel: {exc2}"}
        channel = str((created or {}).get("channel_id") or "")
        if not channel:
            return {"ok": False, "error": "Slack didn't return a channel."}
        self._code_channels[channel] = True
        if self._slash_channel_gated(channel):  # the delegate's allowed_channels / ignored_channels would drop the task
            return {"ok": False, "channel_id": channel, "channel_mention": f"<#{channel}>",
                    "error": "The channel was created but this bot ignores it (allowed_channels/ignored_channels)."}
        self._handoffs[session_id] = channel
        footer = f"\n\n(Handed over from <#{origin_channel}> by <@{user_id}>.)" if origin_channel else ""
        stamp = f"{time.time():.6f}"
        team = self._event_team_id({}, None, install_set=getattr(self, "_team_clients", None)) or ""
        event = {"type": "message", "channel": channel, "channel_type": "channel", "user": user_id,
                 "text": task + footer, "ts": stamp, "event_ts": stamp, "team": team}
        asyncio.ensure_future(self._dispatch_handoff(event, {"team_id": team, "event": event}))
        return {"ok": True, "channel_id": channel, "channel_mention": f"<#{channel}>", "name": name}

    async def _dispatch_handoff(self, event: dict, body: dict) -> None:
        try:
            await self._handle_slack_message(event, body)
        except Exception:
            logger.warning("[Slack] handoff: starting the delegate's first turn failed", exc_info=True)


def register_tool(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL_NAME, toolset=TOOLSET, schema=tool_schema(), handler=handoff_tool,
        description="Hand a coding or project task to a project bot in a new Slack Code channel.", emoji="↗")
