"""Slack Code channels: one shared agent session per channel.

Mixed into ``SlackAdapter``. Behaviour is unchanged from the baseline patch; the feature is
gated by ``SLACK_CODE_CHANNELS`` (default on) so it can be disabled without removing the plugin.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def code_channel_cards_enabled() -> bool:
    """Flat task cards in Code channels (still needs ``native_task_cards`` for the platform)."""
    return os.getenv("SLACK_CODE_CHANNEL_TASK_CARDS", "true").strip().lower() not in ("0", "false", "no", "off")


def code_channel_shared_session_enabled() -> bool:
    """One shared session per Code channel (default) instead of one per participant."""
    return os.getenv("SLACK_CODE_CHANNEL_SHARED_SESSION", "true").strip().lower() not in ("0", "false", "no", "off")


def code_channels_enabled() -> bool:
    return os.getenv("SLACK_CODE_CHANNELS", "true").strip().lower() not in ("0", "false", "no", "off")


class SlackCodeChannelMixin:
    def _init_code_channel_state(self) -> None:
        self._code_channels: dict[str, bool] = {}
        self._code_channel_roots: dict[str, None] = {}
        self._code_channel_threaded: set[str] = set()
        self._code_channel_busy: set[str] = set()

    # ── Slack Code channels ────────────────────────────────────────────────
    # A code channel (Slack Code, GA 2026-10-28) is one agent session per channel: people talk to the
    # agent without @mentioning it, it replies top-level, the session status is channel-level
    # (``agents.sessions.setStatus`` without ``thread_ts``) and Slack sends ``agent_session_stopped``
    # for the Stop button. https://docs.slack.dev/ai/slack-code/
    _CODE_CHANNEL_ROOTS_MAX = 5000

    def build_source(self, *args: Any, **kwargs: Any):
        """A Code channel is one session shared by everyone in it. ``shared_session`` drops the
        participant from the session key but keeps ``user_id`` (slash access checks) and labels each
        message with its sender. DMs and other channels are untouched."""
        source = super().build_source(*args, **kwargs)
        chat_id = getattr(source, "chat_id", "")
        if (getattr(source, "chat_type", "dm") != "dm" and code_channel_shared_session_enabled()
                and self._is_known_code_channel(chat_id)):
            import dataclasses
            return dataclasses.replace(source, shared_session=True)
        return source

    @staticmethod
    def _message_shared_session(source: Any, event: dict) -> bool:
        """Whether an inbound message's session is shared by everyone in the chat: a group DM (mpim), or
        whatever ``build_source`` already decided (a Code channel). Must not reset a flag already set."""
        return bool(getattr(source, "shared_session", False)) or (event or {}).get("channel_type") == "mpim"

    def _code_channel_cards_enabled(self) -> bool:
        return code_channel_cards_enabled()

    def _is_known_code_channel(self, channel_id: str) -> bool:
        return bool(channel_id) and getattr(self, "_code_channels", {}).get(channel_id) is True

    async def _detect_code_channel(self, channel_id: str) -> None:
        """Cache whether ``channel_id`` is a code channel. The documented signal is
        ``properties.record_channel.record_type == "agent_channel"``; ``properties.code_channel`` can be
        ``{}`` on a new channel, so it is not used. Lookup failures are not cached (retried next message)."""
        cache = getattr(self, "_code_channels", None)
        if cache is None or not code_channels_enabled() or not channel_id or channel_id in cache or channel_id[:1] not in ("C", "G"):
            return
        try:
            resp = await self._get_client(channel_id).conversations_info(channel=channel_id)
            props = (resp.get("channel") or {}).get("properties") or {}
            cache[channel_id] = (props.get("record_channel") or {}).get("record_type") == "agent_channel"
        except Exception as e:  # never block inbound on this lookup
            logger.debug("[Slack] code-channel lookup for %s failed: %s", channel_id, e, exc_info=True)

    async def _note_code_channel_message(self, event: dict) -> None:
        """Before an inbound message is handled: recognise its channel and remember top-level message
        ts (replies to them go top-level) and roots a user replied under (those stay threaded). Only
        plain user messages count; joins and edits are dropped later anyway."""
        try:
            channel = str(event.get("channel") or "")
            await self._detect_code_channel(channel)
            if not self._is_known_code_channel(channel) or event.get("subtype") not in (
                    None, "file_share", "thread_broadcast"):
                return
            ts, thread_ts = str(event.get("ts") or ""), str(event.get("thread_ts") or "")
            if ts and (not thread_ts or thread_ts == ts):
                roots = self._code_channel_roots
                roots[ts] = None
                while len(roots) > self._CODE_CHANNEL_ROOTS_MAX:
                    roots.pop(next(iter(roots)))
            elif thread_ts:
                self._code_channel_threaded.add(thread_ts)
        except Exception:
            logger.debug("[Slack] code-channel bookkeeping failed", exc_info=True)

    def _code_channel_reply_is_flat(self, reply_to: Optional[str], metadata: Optional[dict[str, Any]]) -> bool:
        """A reply to a top-level code-channel message goes top-level. Inbound stamps the message's own
        ts as ``metadata.thread_id`` and the streaming path passes no ``reply_to``, so key on either."""
        roots = getattr(self, "_code_channel_roots", None)
        if not roots:
            return False
        md = metadata or {}
        thread = md.get("thread_id") or md.get("thread_ts")
        anchor = thread or reply_to
        return (anchor in roots and anchor not in self._code_channel_threaded
                and (not thread or not reply_to or thread == reply_to))

    async def _set_code_channel_status(self, chat_id: str, status: str) -> None:
        """Channel-level session status (``processing`` while working, ``active`` when the turn ends);
        ``processing`` is sent once per turn because ``_keep_typing`` refreshes often."""
        busy = self._code_channel_busy
        if status == "processing":
            if chat_id in busy:
                return
            busy.add(chat_id)
        else:
            busy.discard(chat_id)
        try:
            await self._get_client(chat_id).api_call(
                "agents.sessions.setStatus", json={"channel_id": chat_id, "status": status})
        except Exception as e:
            logger.debug("[Slack] agents.sessions.setStatus %s in %s failed: %s", status, chat_id, e,
                         exc_info=True)

    async def _handle_agent_session_stopped(self, event: dict, body: Optional[dict] = None) -> None:
        """Slack's native Stop (code channel or thread); see ``native_stop.py``."""
        try:
            from .native_stop import handle_agent_session_stopped
        except ImportError:  # flat import (tests, standalone)
            from native_stop import handle_agent_session_stopped  # type: ignore
        await handle_agent_session_stopped(self, event, body)
