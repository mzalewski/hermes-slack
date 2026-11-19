import asyncio
import importlib.util
import json
import pathlib
import sys

_dir = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("slack_pr_stack", _dir / "pr_stack.py")
ps = importlib.util.module_from_spec(_spec)
sys.modules["slack_pr_stack"] = ps
_spec.loader.exec_module(ps)


def pr(n, head, base, title=""):
    return {"number": n, "headRefName": head, "baseRefName": base, "title": title or f"pr{n}", "url": f"https://u/{n}"}


PRS = [pr(1, "a", "dev"), pr(2, "b", "a"), pr(3, "c", "b"), pr(4, "d", "b"), pr(5, "x", "dev"), pr(6, "y", "x")]


def test_parse_remote():
    assert ps.parse_remote("git@github.com:Org/Repo.git") == "Org/Repo"
    assert ps.parse_remote("https://github.com/Org/Re.po") == "Org/Re.po"
    assert ps.parse_remote("https://github.com/Org/Repo.git/\n") == "Org/Repo"
    assert ps.parse_remote("") == "" and ps.parse_remote("git@gitlab.com:a/b.git") == ""


def test_stack_from_middle_includes_base_chain_and_everything_above():
    got = ps.stack_from_prs("b", PRS)
    assert [p["number"] for p in got] == [1, 2, 3, 4]  # trunk first, then both children of b
    assert [p["number"] for p in got if p["current"]] == [2]


def test_stack_from_top_bottom_and_unrelated():
    assert [p["number"] for p in ps.stack_from_prs("c", PRS)] == [1, 2, 3]
    assert [p["number"] for p in ps.stack_from_prs("a", PRS)] == [1, 2, 3, 4]
    assert [p["number"] for p in ps.stack_from_prs("y", PRS)] == [5, 6]
    assert ps.stack_from_prs("unpushed", PRS) == [] and ps.stack_from_prs("", PRS) == []


def test_stack_survives_a_base_cycle():
    cyc = [pr(1, "a", "b"), pr(2, "b", "a")]
    assert sorted(p["number"] for p in ps.stack_from_prs("a", cyc)) == [1, 2]


def fake_runner(calls, branch="b", prs=PRS, remote="git@github.com:Org/Repo.git"):
    async def runner(cmd, cwd):
        calls.append(cmd[:2])
        if cmd[:2] == ["git", "remote"]:
            return remote
        if cmd[:2] == ["git", "branch"]:
            return branch
        return json.dumps(prs)
    return runner


def test_work_stack_caches_gh_and_survives_failures():
    ps._cache.clear()
    calls = []
    repo, stack = asyncio.run(ps.work_stack("/w", fake_runner(calls), now=100.0))
    assert repo == "Org/Repo" and [p["number"] for p in stack] == [1, 2, 3, 4]
    asyncio.run(ps.work_stack("/w", fake_runner(calls), now=110.0))
    assert calls.count(["gh", "pr"]) == 1  # second call inside the TTL reuses the PR list
    _, again = asyncio.run(ps.work_stack("/w", fake_runner([], prs=[]), now=200.0))  # gh returned nothing: keep last good
    assert [p["number"] for p in again] == [1, 2, 3, 4]
    assert asyncio.run(ps.work_stack("", fake_runner([]))) == ("", [])
    ps._cache.clear()
    assert asyncio.run(ps.work_stack("/w", fake_runner([], remote=""), now=1.0)) == ("", [])
    assert asyncio.run(ps.work_stack("/w", fake_runner([], branch=""), now=1.0))[1] == []  # detached HEAD
