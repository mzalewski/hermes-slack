#!/usr/bin/env python3
"""Install / remove the Claude Code hook that feeds Slack progress cards.

    python3 claude_code_install.py install   [--settings ~/.claude/settings.json] [--dry-run]
    python3 claude_code_install.py uninstall [--settings ...] [--dry-run]
    python3 claude_code_install.py status    [--settings ...]

Merges into the existing settings (other hooks are kept), backs the file up first, refuses to touch a
file that isn't valid JSON, and is idempotent. The hook command exits immediately unless Hermes launched
this Claude Code from Slack, so normal Claude Code use is unaffected.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")
MARKER = "claude_code_hook.py"
HOOK_PATH = Path(__file__).resolve().with_name(MARKER)


def hook_command(hook_path: Path = HOOK_PATH) -> str:
    # Skip Python entirely unless Hermes launched this run from Slack.
    return f'[ -z "$HERMES_SESSION_CHAT_ID" ] && exit 0; exec python3 "{hook_path}"'


def _is_ours(entry: dict) -> bool:
    return any(MARKER in str(h.get("command", "")) for h in (entry.get("hooks") or []) if isinstance(h, dict))


def _entry(event: str, command: str) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": command}]}
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        entry["matcher"] = "*"
    return entry


def merged(settings: dict, install: bool, command: str) -> dict:
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' in settings is not an object")
    for event in EVENTS:
        kept = [e for e in (hooks.get(event) or []) if not (isinstance(e, dict) and _is_ours(e))]
        if install:
            kept.append(_entry(event, command))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        out.pop("hooks", None)
    return out


def run(action: str, settings_path: Path, dry_run: bool = False, hook_path: Path = HOOK_PATH) -> str:
    raw = settings_path.read_text() if settings_path.exists() else "{}"
    try:
        settings = json.loads(raw or "{}")
    except ValueError as exc:
        raise SystemExit(f"{settings_path} is not valid JSON ({exc}); not touching it.")
    if not isinstance(settings, dict):
        raise SystemExit(f"{settings_path} is not a JSON object; not touching it.")
    present = sorted(e for e in EVENTS if any(isinstance(x, dict) and _is_ours(x) for x in (settings.get("hooks") or {}).get(e, [])))
    if action == "status":
        return f"installed for: {', '.join(present)}" if present else "not installed"
    new = merged(settings, action == "install", hook_command(hook_path))
    if new == settings:
        return "no changes needed"
    if dry_run:
        return json.dumps(new, indent=2)
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_name(f"{settings_path.name}.bak-hermes-slack-{int(time.time())}"))
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n")
    os.replace(tmp, settings_path)
    return f"{action}ed ({settings_path})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(run(args.action, Path(args.settings).expanduser(), args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
