"""From a working directory to the stack of PRs it belongs to.

``git`` gives the repo and the checked-out branch; ``gh pr list`` gives the open PRs with their head and base
branches. A PR whose base branch is another PR's head branch sits on top of it, so the stack is that chain
(and anything stacked on top of the branch's own PR). All of it is best-effort: any failure means "no stack".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_TTL = 60.0
_TIMEOUT = 20.0
_cache: dict[str, tuple[float, list[dict]]] = {}

Runner = Callable[[list[str], str], Awaitable[str]]


async def run(cmd: list[str], cwd: str) -> str:
    """stdout of a command, or ``""`` on any failure (missing binary, non-zero exit, timeout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd or None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        return out.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
    except (OSError, asyncio.TimeoutError):
        return ""


def parse_remote(url: str) -> str:
    """``git@github.com:Org/Repo.git`` / ``https://github.com/Org/Repo`` -> ``Org/Repo``."""
    m = re.search(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$", (url or "").strip())
    return m.group(1) if m else ""


def stack_from_prs(branch: str, prs: list[dict]) -> list[dict]:
    """The PRs in the stack containing ``branch``'s PR, base first. ``[]`` if the branch has no open PR.
    Each item is the ``gh`` dict plus ``"current": bool``."""
    by_head = {p.get("headRefName"): p for p in prs if p.get("headRefName")}
    mine = by_head.get(branch)
    if not mine:
        return []
    chain = [mine]
    seen = {mine["number"]}
    node = mine
    while by_head.get(node.get("baseRefName")) and by_head[node["baseRefName"]]["number"] not in seen:  # down to the trunk
        node = by_head[node["baseRefName"]]
        seen.add(node["number"])
        chain.insert(0, node)
    frontier = [mine]
    while frontier:  # everything stacked on top, breadth first
        nxt = []
        for parent in frontier:
            for p in sorted(prs, key=lambda x: x.get("number", 0)):
                if p.get("baseRefName") == parent.get("headRefName") and p["number"] not in seen:
                    seen.add(p["number"])
                    chain.append(p)
                    nxt.append(p)
        frontier = nxt
    return [{**p, "current": p["number"] == mine["number"]} for p in chain]


async def work_stack(cwd: str, runner: Runner = run, now: Optional[float] = None) -> tuple[str, list[dict]]:
    """``(repo, stack)`` for a working directory. ``stack`` is empty when not on a branch with an open PR."""
    if not cwd:
        return "", []
    repo = parse_remote(await runner(["git", "remote", "get-url", "origin"], cwd))
    branch = await runner(["git", "branch", "--show-current"], cwd)
    if not (repo and branch):
        return repo, []
    clock = time.monotonic() if now is None else now
    hit = _cache.get(repo)
    if hit and clock - hit[0] < _TTL:
        prs = hit[1]
    else:
        raw = await runner(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json",
                            "number,headRefName,baseRefName,title,url"], cwd)
        try:
            prs = json.loads(raw) if raw else []
        except ValueError:
            prs = []
        if prs:
            _cache[repo] = (clock, prs)
        elif hit:
            prs = hit[1]  # keep the last good answer through a gh hiccup
    return repo, stack_from_prs(branch, prs)


def pr_key(repo: str, number: Any) -> str:
    return f"{repo}#{int(number)}"
