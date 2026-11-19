"""Plain-language step titles for Slack task cards.

Core sends titles like ``read_file - auth/session.py`` (tool name, then a short preview). This maps
them to "Opening auth/session.py". Unknown tools keep the core title. Pure and offline: no model call.
"""
from __future__ import annotations

import re

_LABELS: dict[str, str] = {
    "read_file": "Opening {p}", "write_file": "Writing {p}", "patch": "Editing {p}",
    "search_files": "Searching {p}", "web_search": "Checking the web: {p}", "web_extract": "Reading {p}",
    "terminal": "Running {p}", "process": "Checking process {p}", "execute_code": "Running code",
    "delegate_task": "Delegating: {p}", "todo": "Updating plan", "memory": "Checking memory",
    "skill_view": "Loading skill {p}", "clarify": "Asking a question", "vision_analyze": "Looking at {p}",
    # Claude Code tool names (PascalCase, distinct from Hermes's snake_case ones)
    "Read": "Reading {p}", "Write": "Writing {p}", "Edit": "Editing {p}", "MultiEdit": "Editing {p}",
    "NotebookEdit": "Editing {p}", "Bash": "Running {p}", "Grep": "Searching {p}", "Glob": "Finding {p}",
    "WebSearch": "Searching the web: {p}", "WebFetch": "Fetching {p}", "Task": "Delegating: {p}",
    "TodoWrite": "Updating plan", "BashOutput": "Checking background command", "KillShell": "Stopping command",
}
_BROWSER = re.compile(r"^browser_")


def friendly_title(raw: str) -> str:
    raw = (raw or "").strip()
    name, _, preview = raw.partition(" - ")
    name, preview = name.strip(), preview.strip()
    template = _LABELS.get(name) or ("Browsing {p}" if _BROWSER.match(name) else None)
    if template is None:
        return raw
    if "{p}" not in template:
        return template
    return template.format(p=preview).strip() if preview else template.replace("{p}", "").strip(" :")
