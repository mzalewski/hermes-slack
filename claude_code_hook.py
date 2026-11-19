#!/usr/bin/env python3
"""Claude Code hook -> Slack card bridge.

Installed (by claude_code_install.py) as a Claude Code hook. For a Claude Code run that Hermes launched
from a Slack chat (HERMES_SESSION_PLATFORM=slack, which Hermes copies into the terminal's environment),
append a trimmed copy of the hook event to ``<events dir>/<chat_id>.jsonl``; the Slack plugin reads that
file and shows the run on the progress card. Anything else (a Claude Code you run yourself) exits at once.

Stdlib only; never fails the hook (always exit 0); file and directory are private (0600 / 0700).
Tool inputs are trimmed to a short allow-list of fields so file contents never leave Claude Code.
"""
import json
import os
import re
import sys
import time

_KEEP_EVENT = ("hook_event_name", "session_id", "cwd", "tool_name", "tool_use_id", "reason", "duration_ms", "is_interrupt")
_KEEP_INPUT = ("file_path", "notebook_path", "path", "pattern", "command", "url", "query", "description", "prompt")


def _events_dir():
    return os.environ.get("HERMES_SLACK_CLAUDE_EVENTS") or os.path.join(os.path.expanduser("~"), ".hermes", "slack-claude-events")


def _trim(event):
    out = {k: event[k] for k in _KEEP_EVENT if k in event}
    if isinstance(event.get("tool_input"), dict):
        out["tool_input"] = {k: str(v)[:300] for k, v in event["tool_input"].items() if k in _KEEP_INPUT}
    for key, limit in (("error", 300), ("prompt", 500), ("last_assistant_message", 2000)):
        if event.get(key) is not None:
            out[key] = str(event[key])[:limit]
    return out


def _narration(event):
    """What Claude wrote just before this tool call: ``(message id, text)``. The assistant message that holds
    the tool call is written to the transcript as several lines sharing one message id; the text block(s) are
    the narration. Reads only the tail of the file; ``("", "")`` on any problem."""
    path, want = event.get("transcript_path"), event.get("tool_use_id")
    if event.get("hook_event_name") != "PreToolUse" or not path or not want:
        return "", ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262144))
            lines = handle.read().decode("utf-8", "replace").splitlines()
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        message_id = ""
        for row in reversed(rows):
            content = (row.get("message") or {}).get("content")
            if row.get("type") == "assistant" and isinstance(content, list) and any(
                    b.get("type") == "tool_use" and b.get("id") == want for b in content if isinstance(b, dict)):
                message_id = row["message"].get("id") or row.get("uuid") or ""
                break
        if not message_id:
            return "", ""
        texts = []
        for row in rows:
            msg = row.get("message") or {}
            if row.get("type") == "assistant" and msg.get("id") == message_id and isinstance(msg.get("content"), list):
                texts += [b.get("text", "") for b in msg["content"] if isinstance(b, dict) and b.get("type") == "text"]
        return message_id, "\n".join(t.strip() for t in texts if t and t.strip())[:800]
    except Exception:
        return "", ""


def main():
    chat = os.environ.get("HERMES_SESSION_CHAT_ID", "")
    if not chat or os.environ.get("HERMES_SESSION_PLATFORM", "") != "slack":
        return
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    safe_chat = re.sub(r"[^A-Za-z0-9_.-]", "_", chat)[:64]
    record = {"t": round(time.time(), 3), "chat": chat, "thread": os.environ.get("HERMES_SESSION_THREAD_ID", ""),
              "pid": os.getppid(), "event": _trim(event)}
    narration_id, narration = _narration(event)
    if narration:
        record["narration_id"], record["narration"] = narration_id, narration
    directory = _events_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(directory, safe_chat + ".jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
