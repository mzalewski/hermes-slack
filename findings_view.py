"""A "Findings" tab in Code channels, kept in step with the PR-review ``findings.json``.

The review skills (and the poller) load-merge-save one shared file. This module watches it and, when it
changes, re-renders a ``setView`` tab in every Code channel whose current work touches the PRs involved.

A channel's work is learned from what is said in it: PR references in messages (``owner/repo#123``, a
``…/pull/123`` URL, ``PR 123``, or ``#123`` when 123 is a PR the review state knows). A change to the file
made while a channel has a live run is also attributed to that channel (the run most likely wrote it).
PRs with nothing actionable and no history are never shown; PRs whose findings are all closed show counts.

Flags: ``SLACK_FINDINGS_VIEW`` (default off), ``SLACK_FINDINGS_FILE`` (default
``~/.hermes/workflows/pr-review/findings.json``), ``SLACK_FINDINGS_LINKS_FILE`` (where the channel to PR
links persist).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

try:
    from . import pr_stack
except ImportError:  # pragma: no cover - loaded outside a package
    import pr_stack  # type: ignore

logger = logging.getLogger(__name__)

VIEW_KEY = "hermes-findings"
VIEW_NAME = "Findings"
_POLL_SECONDS = 3.0
_STACK_REFRESH = 60.0
_MIN_PUBLISH_INTERVAL = 5.0
_MAX_PRS_PER_CHANNEL = 10
_MAX_FINDINGS_PER_PR = 8
_MAX_BLOCKS = 45
_ACTIONABLE = ("open", "planned")
_STATUS_ICON = {"open": "🔴", "planned": "🛠️"}


def findings_view_enabled() -> bool:
    return os.getenv("SLACK_FINDINGS_VIEW", "false").strip().lower() in ("1", "true", "yes", "on")


def findings_path() -> Path:
    return Path(os.path.expanduser(os.getenv("SLACK_FINDINGS_FILE") or "~/.hermes/workflows/pr-review/findings.json"))


def links_path() -> Path:
    override = os.getenv("SLACK_FINDINGS_LINKS_FILE")
    return Path(os.path.expanduser(override)) if override else findings_path().with_name("slack_channel_prs.json")


# ── reading the review files ─────────────────────────────────────────────────────────────────────────
def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_prs(path: Optional[Path] = None) -> dict[str, dict]:
    """``{pr key: {title, url}}`` from the poller's ``state.json`` beside the findings file."""
    state = load_json((path or findings_path()).with_name("state.json"))
    prs = state.get("prs")
    return prs if isinstance(prs, dict) else {}


def fingerprint(finding: dict) -> str:
    keys = ("status", "note", "body", "blocking", "status_updated_at", "path", "line")
    return hashlib.sha1(json.dumps([finding.get(k) for k in keys], default=str).encode()).hexdigest()[:12]


def snapshot(findings: dict) -> dict[str, str]:
    return {key: fingerprint(f) for key, f in findings.items() if isinstance(f, dict)}


def changed_prs(before: dict[str, str], after: dict[str, str], findings: dict) -> set[str]:
    """PRs with a finding that was added, edited or removed between two snapshots."""
    touched: set[str] = set()
    for key in set(before) | set(after):
        if before.get(key) != after.get(key):
            f = findings.get(key)
            pr = (f or {}).get("pr") if isinstance(f, dict) else key.rsplit("-F", 1)[0]
            if pr:
                touched.add(pr)
    return touched


# ── which PRs a channel is about ─────────────────────────────────────────────────────────────────────
_FULL = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d{1,6})\b")
_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d{1,6})")
_WORD = re.compile(r"\b(?:PRs?|pull request|pull)\s*#?(\d{2,6})\b", re.I)
_BARE = re.compile(r"(?<![\w/])#(\d{2,6})\b")


def default_repo(known: set[str]) -> str:
    counts: dict[str, int] = {}
    for key in known:
        repo = key.rsplit("#", 1)[0]
        counts[repo] = counts.get(repo, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def extract_prs(text: str, known: set[str]) -> list[str]:
    """PR keys mentioned in ``text``. Fully qualified references are trusted; a bare ``#123`` only counts
    when it names a PR we know, so an issue number or a hex colour never links a channel."""
    found: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key not in seen:
            seen.add(key)
            found.append(key)

    text = str(text or "")
    for m in _URL.finditer(text):
        add(f"{m.group(1)}#{int(m.group(2))}")
    for m in _FULL.finditer(text):
        if "/" in m.group(1) and not m.group(1).startswith("http"):
            add(f"{m.group(1)}#{int(m.group(2))}")
    repo = default_repo(known)
    if repo:
        for m in _WORD.finditer(text):
            add(f"{repo}#{int(m.group(1))}")
        for m in _BARE.finditer(text):
            key = f"{repo}#{int(m.group(1))}"
            if key in known:
                add(key)
    return found


class ChannelLinks:
    """channel id -> PR keys (most recent last), persisted as one small JSON file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or links_path()
        self.links: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        raw = load_json(self.path)
        self.links = {c: [p for p in prs if isinstance(p, str)] for c, prs in raw.items() if isinstance(prs, list)}

    def for_channel(self, channel: str) -> list[str]:
        return list(self.links.get(channel, []))

    def channels_for(self, prs: set[str]) -> set[str]:
        return {c for c, linked in self.links.items() if prs.intersection(linked)}

    def add(self, channel: str, prs: list[str]) -> bool:
        """Link PRs to a channel (the newest end of the list). Returns True if the set changed."""
        current = self.links.get(channel, [])
        merged = [p for p in current if p not in prs] + [p for p in prs]
        merged = merged[-_MAX_PRS_PER_CHANNEL:]
        if merged == current:
            return False
        self.links[channel] = merged
        self._save(channel)
        return True

    def _save(self, channel: str) -> None:
        try:
            disk = load_json(self.path)  # merge: other profiles' adapters own other channels
            disk[channel] = self.links[channel]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.debug("[Slack] couldn't save findings links", exc_info=True)


# ── rendering ────────────────────────────────────────────────────────────────────────────────────────
def _clip(text: Any, limit: int) -> str:
    one = re.sub(r"\s+", " ", str(text or "")).strip()
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _headline(body: str) -> str:
    for line in str(body or "").splitlines():
        line = re.sub(r"[*_`>#]+", "", line).strip()
        if line:
            return line
    return ""


def _mrkdwn_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pr_short(pr: str) -> str:
    return "#" + pr.rsplit("#", 1)[-1]


def render_blocks(prs: list[str], findings: dict, pr_info: dict[str, dict]) -> list[dict]:
    """Blocks for the PRs, in the order given. PRs with no findings at all are skipped."""
    by_pr: dict[str, list[dict]] = {}
    for f in findings.values():
        if isinstance(f, dict) and f.get("pr") in prs:
            by_pr.setdefault(f["pr"], []).append(f)
    blocks: list[dict] = []
    for pr in prs:
        items = by_pr.get(pr)
        if not items:
            continue
        actionable = [f for f in items if f.get("status") in _ACTIONABLE]
        actionable.sort(key=lambda f: (f.get("status") != "open", not f.get("blocking"), str(f.get("path") or "~")))
        closed = len(items) - len(actionable)
        info = pr_info.get(pr, {})
        url = info.get("url") or next((f.get("pr_url") for f in items if f.get("pr_url")), "")
        label = f"{pr_short(pr)} {_mrkdwn_escape(_clip(info.get('title'), 90))}".strip()
        head = f"<{url}|*{label}*>" if url else f"*{label}*"
        counts = [f"{sum(1 for f in actionable if f.get('status') == s)} {s}" for s in _ACTIONABLE
                  if any(f.get("status") == s for f in actionable)]
        if closed:
            counts.append(f"{closed} closed")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{' · '.join(counts) or 'nothing open'}"}})
        for f in actionable[:_MAX_FINDINGS_PER_PR]:
            icon = "⛔" if f.get("blocking") else _STATUS_ICON.get(f.get("status"), "•")
            where = f"`{f['path']}{':' + str(f['line']) if f.get('line') else ''}` " if f.get("path") else ""
            title = _mrkdwn_escape(_clip(_headline(f.get("body")), 150))
            line = f"{icon} {where}{title}"
            note = _clip(f.get("note"), 200)
            if note:
                line += f"\n_{_mrkdwn_escape(note)}_"
            if f.get("url"):
                line += f"  <{f['url']}|view>"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line[:2900]}})
        if len(actionable) > _MAX_FINDINGS_PER_PR:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"+{len(actionable) - _MAX_FINDINGS_PER_PR} more on {pr_short(pr)}"}]})
        blocks.append({"type": "divider"})
        if len(blocks) >= _MAX_BLOCKS:
            break
    if blocks and blocks[-1].get("type") == "divider":
        blocks.pop()
    return blocks[:_MAX_BLOCKS]


def blocks_digest(blocks: list[dict]) -> str:
    return hashlib.sha1(json.dumps(blocks, sort_keys=True).encode()).hexdigest()


def profile_cwd(profile: Optional[str]) -> str:
    """``terminal.cwd`` from a profile's config.yaml (the directory its terminal tool starts in)."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    name = (profile or "").strip()
    path = home / "config.yaml" if name in ("", "default", "main") else home / "profiles" / name / "config.yaml"
    try:
        import yaml
        cwd = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("terminal", {}).get("cwd", "")
    except Exception:
        return ""
    cwd = os.path.expanduser(str(cwd or ""))
    return cwd if cwd.startswith("/") else ""


# ── adapter side ─────────────────────────────────────────────────────────────────────────────────────
class SlackFindingsMixin:
    """Watches the findings file and keeps each linked Code channel's Findings tab current."""

    def _init_findings_state(self) -> None:
        self._findings_links: Optional[ChannelLinks] = None
        self._findings_task: Optional[Any] = None
        self._findings_stat: Optional[tuple] = None
        self._findings_snap: dict[str, str] = {}
        self._findings_digest: dict[str, str] = {}
        self._findings_last: dict[str, float] = {}
        self._findings_stacks: dict[str, tuple[float, list[str], dict]] = {}
        self._findings_refresh_at = 0.0

    def _findings_online(self) -> None:
        if not findings_view_enabled() or self._findings_task is not None:
            return
        self._findings_links = ChannelLinks()
        self._findings_snap = snapshot(load_json(findings_path()))  # start from now: no replay of old edits
        self._findings_stat = self._findings_file_stat()
        self._findings_task = asyncio.ensure_future(self._findings_loop())

    def _findings_offline(self) -> None:
        task, self._findings_task = self._findings_task, None
        if task is not None:
            task.cancel()

    @staticmethod
    def _findings_file_stat() -> Optional[tuple]:
        try:
            st = findings_path().stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    async def _findings_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_POLL_SECONDS)
                await self._findings_tick()
                if time.monotonic() - self._findings_refresh_at >= _STACK_REFRESH:
                    await self._findings_refresh_stacks()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[Slack] findings watcher tick failed", exc_info=True)

    def _live_run_channels(self) -> set[str]:
        """Code channels with a run in progress right now (a live progress card)."""
        live: set[str] = set()
        for stream in list(getattr(self, "_native_task_card_streams", {}).values()):
            if not getattr(stream, "stopped", True) and self._is_known_code_channel(stream.channel):
                live.add(stream.channel)
        return live

    def _code_channel_ids(self) -> list[str]:
        return [c for c, v in getattr(self, "_code_channels", {}).items() if v is True]

    # ── a channel's working directory, its branch's PR stack ──
    def _findings_cwd(self, channel: str) -> str:
        """Where this channel's work happens: the directory its Claude Code runs report, else the bot's
        configured terminal directory."""
        try:
            try:
                from .subagents import TRACKER
            except ImportError:  # pragma: no cover
                from subagents import TRACKER  # type: ignore
            for rec in reversed(TRACKER.for_chat(channel)):
                if getattr(rec, "cwd", ""):
                    return rec.cwd
        except Exception:
            pass
        return profile_cwd(getattr(self, "_owner_profile", None))

    async def _findings_stack(self, channel: str, force: bool = False) -> tuple[list[str], dict]:
        """``(PR keys in display order, {key: {title, url}})`` for the channel's current branch stack."""
        hit = self._findings_stacks.get(channel)
        if hit and not force and time.monotonic() - hit[0] < _STACK_REFRESH / 2:
            return hit[1], hit[2]
        repo, stack = await pr_stack.work_stack(self._findings_cwd(channel))
        stack = sorted(stack, key=lambda p: (not p.get("current"), p["number"]))  # the branch's own PR first
        keys = [pr_stack.pr_key(repo, p["number"]) for p in stack]
        info = {pr_stack.pr_key(repo, p["number"]): {"title": p.get("title"), "url": p.get("url")} for p in stack}
        self._findings_stacks[channel] = (time.monotonic(), keys, info)
        return keys, info

    async def _findings_prs_for(self, channel: str) -> tuple[list[str], dict]:
        """The stack for the channel's branch first, then PRs mentioned or touched in the channel."""
        keys, info = await self._findings_stack(channel)
        extra = [p for p in reversed(self._findings_links.for_channel(channel) if self._findings_links else []) if p not in keys]
        return keys + extra, info

    async def _findings_refresh_stacks(self) -> None:
        """Pick up a branch switch, a new stacked PR or a merged one without any file change."""
        self._findings_refresh_at = time.monotonic()
        findings = None
        for channel in self._code_channel_ids():
            before = (self._findings_stacks.get(channel) or (0, None))[1]
            keys, _ = await self._findings_stack(channel, force=True)
            if keys and keys != before:
                findings = findings if findings is not None else load_json(findings_path())
                await self._publish_findings_view(channel, findings)

    async def _findings_tick(self) -> None:
        links = self._findings_links
        if links is None:
            return
        stat = self._findings_file_stat()
        if stat == self._findings_stat:
            return
        self._findings_stat = stat
        findings = load_json(findings_path())
        after = snapshot(findings)
        touched = changed_prs(self._findings_snap, after, findings)
        self._findings_snap = after
        if not touched:
            return
        for channel in self._live_run_channels():  # a run that is live while the file changes wrote it
            links.add(channel, sorted(touched))
        for channel in self._code_channel_ids():
            prs, _ = await self._findings_prs_for(channel)
            if touched.intersection(prs):
                await self._publish_findings_view(channel, findings)

    def _findings_note_text(self, channel: str, text: str) -> None:
        """Learn which PRs a Code channel is about from a message sent or received there."""
        if self._findings_links is None or not text or not self._is_known_code_channel(channel):
            return
        known = set(load_prs()) | {f.get("pr") for f in load_json(findings_path()).values() if isinstance(f, dict)}
        prs = extract_prs(text, {k for k in known if k})
        if prs and self._findings_links.add(channel, prs):
            asyncio.ensure_future(self._publish_findings_view(channel))

    async def _publish_findings_view(self, channel: str, findings: Optional[dict] = None) -> None:
        if self._findings_links is None:
            return
        wait = _MIN_PUBLISH_INTERVAL - (time.monotonic() - self._findings_last.get(channel, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        findings = findings if findings is not None else load_json(findings_path())
        prs, gh_info = await self._findings_prs_for(channel)
        blocks = render_blocks(prs, findings, {**load_prs(), **gh_info})
        if not blocks:
            return
        digest = blocks_digest(blocks)
        if self._findings_digest.get(channel) == digest:
            return
        self._findings_digest[channel] = digest
        self._findings_last[channel] = time.monotonic()
        try:
            await self._get_client(channel).api_call("agents.conversations.setView", json={
                "channel_id": channel, "type": "block_kit", "view_key": VIEW_KEY, "name": VIEW_NAME,
                "blocks": blocks})
        except Exception as exc:
            self._findings_digest.pop(channel, None)
            logger.debug("[Slack] findings setView failed: %s", exc, exc_info=True)
