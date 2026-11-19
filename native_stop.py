"""Slack's native Stop button (``agent_session_stopped``) mapped onto the gateway's ``/stop``.

Slack shows Stop while an agent session is ``processing`` and, on click, sends only this event: the
work keeps running and the session reads "Stopping..." until the app leaves ``processing``. So we
authorise, run ``/stop`` for the right session, then move the status to ``active``.
https://docs.slack.dev/reference/events/agent_session_stopped/
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def native_stop_enabled() -> bool:
    return os.getenv("SLACK_NATIVE_STOP", "true").strip().lower() not in ("0", "false", "no", "off")


async def handle_agent_session_stopped(adapter: Any, event: dict, body: Optional[dict] = None) -> None:
    channel_id = str((event or {}).get("channel") or "")
    user_id = str(event.get("user") or "") if event else ""
    thread_ts = str(event.get("thread_ts") or "") if event else ""
    if not native_stop_enabled() or not (channel_id and user_id):
        logger.debug("[Slack] agent_session_stopped ignored (disabled or missing channel/user): %s", event)
        return
    team_id = adapter._event_team_id(event, body)
    # A click from a gated channel or an unauthorised user changes nothing, status included.
    if adapter._slash_channel_gated(channel_id) or adapter._early_reject_unauthorized(
            user_id, channel_id, channel_id.startswith("D"), thread_id=thread_ts or None):
        return
    logger.info("[Slack] agent_session_stopped in %s (thread %s): stopping the session",
                channel_id, thread_ts or "-")
    if thread_ts:
        # Same session key and command handling as a typed /stop in that thread.
        await adapter._handle_slash_command({
            "command": "/stop", "text": "", "user_id": user_id, "channel_id": channel_id,
            "team_id": team_id, "thread_ts": thread_ts})
        await adapter.stop_typing(channel_id, {"thread_id": thread_ts, "slack_team_id": team_id})
        await _set_active(adapter, channel_id, team_id, thread_ts)
    else:
        # Code channel: one session per channel; route like an ordinary channel message.
        adapter._code_channel_busy.discard(channel_id)
        await adapter._handle_slack_message(
            {"type": "message", "channel": channel_id, "user": user_id, "text": "/stop",
             "ts": str(event.get("event_ts") or "")}, body)
        await _set_active(adapter, channel_id, team_id, None)


async def _set_active(adapter: Any, channel_id: str, team_id: str, thread_ts: Optional[str]) -> None:
    """"active" is the documented way out of "Stopping..."; best effort, /stop already landed."""
    try:
        client = adapter._get_client(channel_id, team_id=team_id or None)
        method = getattr(client, "agents_sessions_setStatus", None)
        if method is None:  # slack-sdk < 3.44
            if thread_ts:
                return
            await client.api_call("agents.sessions.setStatus", json={"channel_id": channel_id, "status": "active"})
            return
        kwargs = {"channel_id": channel_id, "status": "active"}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await method(**kwargs)
    except Exception:
        logger.debug("[Slack] agents.sessions.setStatus active after native stop failed", exc_info=True)
