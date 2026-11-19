"""Reads the events claude_code_hook.py writes and feeds them to the subagent tracker, so a Claude Code run
launched by Hermes shows up as a "🤖 Claude Code" row on the Slack card, with one log line per step."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_FILE_AGE = 24 * 3600


def claude_code_enabled() -> bool:
    return os.getenv("SLACK_CLAUDE_CODE", "true").strip().lower() not in ("0", "false", "no", "off")


def events_dir() -> Path:
    return Path(os.environ.get("HERMES_SLACK_CLAUDE_EVENTS") or Path.home() / ".hermes" / "slack-claude-events")


class EventReader:
    """Incremental JSONL tailer. Partial trailing lines wait for the next poll; the first time a file is seen,
    events older than ``max_age`` seconds are ignored so a restart doesn't replay yesterday's runs."""

    def __init__(self, tracker: Any, directory: Optional[Path] = None, max_age: float = 120.0) -> None:
        self._tracker = tracker
        self._dir = directory
        self._max_age = max_age
        self._offsets: dict[str, int] = {}

    def _directory(self) -> Path:
        return self._dir or events_dir()

    def poll(self) -> int:
        """Apply every new complete event; returns how many were applied."""
        applied = 0
        try:
            entries = [e for e in os.scandir(self._directory()) if e.name.endswith(".jsonl")]
        except OSError:
            return 0
        now = time.time()
        for entry in entries:
            try:
                size = entry.stat().st_size
                first = entry.name not in self._offsets
                offset = self._offsets.get(entry.name, 0)
                if size < offset:
                    offset = 0  # truncated
                if size == offset:
                    self._offsets[entry.name] = offset
                    continue
                with open(entry.path, "rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError:
                continue
            cut = data.rfind(b"\n")
            if cut < 0:
                continue
            self._offsets[entry.name] = offset + cut + 1
            for raw in data[:cut + 1].splitlines():
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if first and now - float(item.get("t") or 0) > self._max_age:
                    continue
                try:
                    self._tracker.on_claude_event(item)
                    applied += 1
                except Exception:
                    logger.debug("[Slack] claude-code event failed", exc_info=True)
        return applied

    def cleanup(self) -> None:
        """Drop event files untouched for a day."""
        cutoff = time.time() - _MAX_FILE_AGE
        try:
            for entry in os.scandir(self._directory()):
                if entry.name.endswith(".jsonl") and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
        except OSError:
            pass
