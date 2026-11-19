"""Native task cards in Code channels: flat, throttled, headed by the current step, handed over when a message
lands below them, and with Claude Code runs in their own stream. Needs the Hermes venv (skips without it)."""
import asyncio
import importlib.util
import pathlib
import sys
import time

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
HERMES = pathlib.Path.home() / ".hermes" / "hermes-agent"


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(HERMES))
    try:
        import gateway.config  # noqa: F401
    except Exception:
        pytest.skip("Hermes not importable")
    # Load as the real plugin loader does (hermes_plugins.* name, relative imports), NOT as
    # plugins.platforms.slack, which is Hermes's bundled copy and lacks our modules.
    name = "hermes_plugins.platforms__slack__home_test"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    return sys.modules[name + ".adapter"]


class Client:
    def __init__(self):
        self.calls = []
        self.n = 0

    async def api_call(self, method, json=None):
        self.calls.append((method, json))
        if method == "chat.startStream":
            self.n += 1
            return {"ok": True, "ts": f"100.{self.n}"}
        return {"ok": True}


def make(mod, interval=0.05, code=True):
    from gateway.config import PlatformConfig
    a = mod.SlackAdapter(PlatformConfig(enabled=True, token="xoxb-x", extra={"native_task_card_interval": interval}))
    a._app = object()
    a.client = Client()
    a._get_client = lambda *args, **kw: a.client
    if code:
        a._code_channels["C1"] = True
    return a


def task(i, status="in_progress", title=None):
    return {"id": f"t{i}", "title": title or f"read_file - f{i}.py", "status": status}


def run(coro):
    return asyncio.run(coro)


def methods(a):
    return [c[0] for c in a.client.calls]


def appends(a):
    return [c for c in a.client.calls if c[0] == "chat.appendStream"]


def tasks_of(call):
    return [c for c in call[1]["chunks"] if c["type"] == "task_update"]


def heading_of(call):
    return next((c["title"] for c in call[1]["chunks"] if c["type"] == "plan_update"), None)


def subagents_mod(mod):
    return sys.modules[mod.__name__.rsplit(".", 1)[0] + ".subagents"]


# ── flat cards, heading, throttle ────────────────────────────────────────────
def test_flat_start_has_no_thread_and_heads_the_card_with_the_current_step(mod):
    a = make(mod)
    r = run(a.send_native_task_card_progress("C1", [task(1)], metadata={}))
    assert r.success
    method, payload = a.client.calls[0]
    assert method == "chat.startStream" and "thread_ts" not in payload and payload["task_display_mode"] == "plan"
    chunks = a.client.calls[1][1]["chunks"]
    assert chunks[0] == {"type": "plan_update", "title": "Opening f1.py"}  # the heading is the step in progress
    assert chunks[1]["title"] == "Opening f1.py" and chunks[1]["type"] == "task_update"


def test_heading_follows_the_current_step_and_closes_as_done_with_the_last_update(mod):
    async def go():
        a = make(mod, interval=0)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2)], metadata={})
        assert [heading_of(c) for c in appends(a)] == ["Opening f1.py", "Opening f2.py"]
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2, "complete"), task(3, "complete")], metadata={})
        await a.stop_native_task_card_progress("C1", metadata={})
        assert heading_of(appends(a)[-1]) == "Done · Opening f3.py"
        assert methods(a)[-1] == "chat.stopStream"
    run(go())


def test_heading_can_be_switched_off(mod, monkeypatch):
    monkeypatch.setenv("SLACK_CARD_HEADING", "false")
    a = make(mod)
    run(a.send_native_task_card_progress("C1", [task(1)], metadata={}))
    assert heading_of(appends(a)[0]) is None  # flat cards back to no heading (Slack's own default)


def test_updates_throttled_and_latest_wins_then_flushed(mod):
    async def go():
        a = make(mod, interval=0.2)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2, "complete"), task(3)], metadata={})
        assert len(appends(a)) == 1  # the two later frames are held back
        await asyncio.sleep(0.4)
        assert len(appends(a)) == 2 and len(tasks_of(appends(a)[1])) == 3  # only the latest frame
    run(go())


def test_stop_flushes_pending_then_sets_the_final_heading_then_stops(mod):
    async def go():
        a = make(mod, interval=30)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete")], metadata={})
        await a.stop_native_task_card_progress("C1", metadata={})
        assert methods(a) == ["chat.startStream", "chat.appendStream", "chat.appendStream", "chat.appendStream", "chat.stopStream"]
        assert tasks_of(appends(a)[1])[0]["status"] == "complete"  # the held-back frame is flushed
        assert heading_of(appends(a)[2]) == "Done · Opening f1.py"
    run(go())


def test_non_code_channel_without_thread_is_not_cardable(mod):
    a = make(mod, code=False)
    assert not run(a.send_native_task_card_progress("C2", [task(1)], metadata={})).success


def test_flag_disables_flat_cards(mod, monkeypatch):
    monkeypatch.setenv("SLACK_CODE_CHANNEL_TASK_CARDS", "false")
    assert not run(make(mod).send_native_task_card_progress("C1", [task(1)], metadata={})).success


def test_friendly_titles(mod):
    f = mod._friendly_title
    assert f("web_search - refresh tokens") == "Checking the web: refresh tokens"
    assert f("terminal - pytest -q") == "Running pytest -q"
    assert f("todo") == "Updating plan"
    assert f("mystery_tool - x") == "mystery_tool - x"


# ── Hermes subagents: a stream per child, following it after the parent turn ends ─
def subagent_env(mod):
    sa = subagents_mod(mod)
    tracker, orig_t, orig_e = sa.SubagentTracker(), sa.TRACKER, sa._session_env
    sa.TRACKER = tracker
    sa._session_env = lambda: ("slack", "C1", "")
    return sa, tracker, (orig_t, orig_e)


def test_subagent_gets_its_own_stream_and_keeps_going_after_the_parent_turn_ends(mod):
    sa, tracker, (orig_t, orig_e) = subagent_env(mod)
    try:
        async def go():
            a = make(mod, interval=0)
            rec = lambda: tracker.get("s1")
            # the parent turn dispatches an async delegation, then its card closes
            await a.send_native_task_card_progress("C1", [task(1, title="delegate_task - 1 task")], metadata={})
            tracker.on_start(child_subagent_id="s1", child_goal="Count Python files. Recursively.", child_session_id="cs1", parent_session_id="p1")
            await a._subagent_apply(rec(), "start")
            assert methods(a) == ["chat.startStream", "chat.appendStream"]  # no steps yet: no stream yet
            tracker.on_tool(task_id="s1", tool_call_id="c1", tool_name="terminal", args={"command": "find . -name '*.py'"})
            await a._subagent_apply(rec(), "tool")
            # the child's own stream opened below the Hermes card; the Hermes card was finished and closed
            assert methods(a) == ["chat.startStream", "chat.appendStream", "chat.startStream", "chat.appendStream",
                                  "chat.appendStream", "chat.stopStream"]
            child_first = appends(a)[1]
            assert child_first[1]["ts"] == "100.2" and heading_of(child_first) == "🔀 Count Python files."
            assert a.client.calls[-1][1]["ts"] == "100.1"
            await a.stop_native_task_card_progress("C1", metadata={})  # parent turn ends: nothing left to close
            assert methods(a).count("chat.stopStream") == 1
            # the child carries on AFTER the parent turn: its stream keeps updating until it finishes
            tracker.on_tool_done(task_id="s1", tool_call_id="c1", status="ok")
            tracker.on_tool(task_id="s1", tool_call_id="c2", tool_name="terminal", args={"command": "wc -l"})
            await a._subagent_apply(rec(), "tool")
            call = appends(a)[-1]
            assert call[1]["ts"] == "100.2" and [(c["title"], c["status"]) for c in tasks_of(call)] == [
                ("Running find . -name '*.py'", "complete"), ("Running wc -l", "in_progress")]
            tracker.on_tool_done(task_id="s1", tool_call_id="c2", status="ok")
            tracker.on_stop(child_session_id="cs1", child_status="completed", child_summary="There are 42 Python files.")
            await a._subagent_apply(rec(), "stop")
            final = appends(a)[-1]
            assert final[1]["ts"] == "100.2" and heading_of(final) == "🔀 Subagent · There are 42 Python files."
            assert all(c["status"] == "complete" for c in tasks_of(final))
            assert a.client.calls[-1][0] == "chat.stopStream" and a.client.calls[-1][1]["ts"] == "100.2" and not a._run_cards
            # the parent's next steps (e.g. reading the delegation result) open a NEW card below
            await a.send_native_task_card_progress("C1", [task(1, "complete", "delegate_task - 1 task"), task(2, title="terminal - echo done")], metadata={})
            new = appends(a)[-1]
            assert new[1]["ts"] == "100.3" and [c["title"] for c in tasks_of(new)] == ["Running echo done"]
        run(go())
    finally:
        sa.TRACKER, sa._session_env = orig_t, orig_e


def test_parallel_subagents_each_get_a_stream_and_the_hermes_card_closes_once(mod):
    sa, tracker, (orig_t, orig_e) = subagent_env(mod)
    try:
        async def go():
            a = make(mod, interval=0)
            await a.send_native_task_card_progress("C1", [task(1, title="delegate_task - 2 tasks")], metadata={})
            for sid, goal in (("s1", "Count files"), ("s2", "List folders")):
                tracker.on_start(child_subagent_id=sid, child_goal=goal, child_session_id=f"c{sid}", parent_session_id="p1")
                tracker.on_tool(task_id=sid, tool_call_id=f"{sid}-1", tool_name="terminal", args={"command": "ls"})
                await a._subagent_apply(tracker.get(sid), "tool")
            starts = [c for c in a.client.calls if c[0] == "chat.startStream"]
            assert len(starts) == 3  # Hermes card + one per child
            headings = {c[1]["ts"]: heading_of(c) for c in appends(a) if c[1]["ts"] in ("100.2", "100.3")}
            assert headings == {"100.2": "🔀 Count files", "100.3": "🔀 List folders"}
            assert methods(a).count("chat.stopStream") == 1  # the Hermes card closed once, at the first child's stream
        run(go())
    finally:
        sa.TRACKER, sa._session_env = orig_t, orig_e


def test_a_quiet_run_is_closed_by_the_watchdog(mod):
    sa, tracker, (orig_t, orig_e) = subagent_env(mod)
    try:
        async def go():
            a = make(mod, interval=0)
            tracker.on_start(child_subagent_id="s1", child_goal="Stuck", child_session_id="cs1", parent_session_id="p1")
            tracker.on_tool(task_id="s1", tool_call_id="c1", tool_name="terminal", args={"command": "sleep 9999"})
            await a._subagent_apply(tracker.get("s1"), "tool")
            card = a._run_cards["s1"]
            card.last_event -= 3600  # quiet for an hour: no stop event ever came
            await a._close_quiet_run_cards()
            assert not a._run_cards and heading_of(appends(a)[-1]) == "🔀 Subagent · Stopped"
            assert a.client.calls[-1][0] == "chat.stopStream"
        run(go())
    finally:
        sa.TRACKER, sa._session_env = orig_t, orig_e


# ── a message lands below the card: later steps move to a new card ───────────
def test_later_steps_go_in_a_new_card_below_the_intervening_message(mod):
    async def go():
        a = make(mod, interval=0)
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2, "complete"), task(3)], metadata={})
        a._code_channel_roots["9.9"] = None  # the user's message ts: replies to it are flat in a Code channel
        a._supersede_task_cards("C1", None, {"thread_id": "9.9"})  # Hermes posts "Using Claude Code…" below the card
        frame = [task(1, "complete"), task(2, "complete"), task(3, "complete"), task(4), task(5)]
        await a.send_native_task_card_progress("C1", frame, metadata={})
        assert methods(a) == ["chat.startStream", "chat.appendStream", "chat.appendStream", "chat.stopStream",
                              "chat.startStream", "chat.appendStream"]
        old_final, new_first = appends(a)[1], appends(a)[2]
        assert old_final[1]["ts"] == "100.1" and new_first[1]["ts"] == "100.2"
        assert [c["id"] for c in tasks_of(old_final)] == ["t1", "t2", "t3"]          # steps 1-3 stay above
        assert [c["id"] for c in tasks_of(new_first)] == ["t4", "t5"]                # 4-5 start below
        assert heading_of(old_final) == "Opening f3.py" and heading_of(new_first) == "Opening f5.py"
        assert "thread_ts" not in a.client.calls[4][1]                               # still a flat card
        await a.send_native_task_card_progress("C1", frame[:3] + [task(4, "complete"), task(5), task(6)], metadata={})
        assert [c["id"] for c in tasks_of(appends(a)[-1])] == ["t4", "t5", "t6"]
        await a.stop_native_task_card_progress("C1", metadata={})
        assert methods(a)[-1] == "chat.stopStream" and a.client.calls[-1][1]["ts"] == "100.2"
    run(go())


def test_step_running_at_the_split_is_closed_above_and_continues_below(mod):
    async def go():
        a = make(mod, interval=0)
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2)], metadata={})
        a._supersede_task_cards("C1", None, {})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2), task(3)], metadata={})
        old_final = {c["id"]: c for c in tasks_of(appends(a)[1])}
        assert old_final["t2"]["status"] == "complete" and "details" not in old_final["t2"]  # never left open, no note
        new = {c["id"]: c for c in tasks_of(appends(a)[2])}
        assert set(new) == {"t2", "t3"} and new["t2"]["status"] == "in_progress"  # t1 finished above; t2 carries on
    run(go())


def test_nothing_running_after_the_message_just_closes_the_old_card(mod):
    async def go():
        a = make(mod, interval=0)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        a._supersede_task_cards("C1", None, {})  # e.g. the final reply
        r = await a.send_native_task_card_progress("C1", [task(1, "complete")], metadata={})
        assert r.success and methods(a) == ["chat.startStream", "chat.appendStream", "chat.appendStream", "chat.stopStream"]
        assert tasks_of(appends(a)[1])[0]["status"] == "complete"  # final state lands on the old card, no new card
        await a.stop_native_task_card_progress("C1", metadata={})
        assert methods(a).count("chat.stopStream") == 1
    run(go())


def test_only_a_message_in_the_cards_own_place_supersedes_it(mod):
    async def go():
        a = make(mod, interval=0)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        a._code_channel_roots["5.5"] = None
        a._supersede_task_cards("C1", None, {"thread_id": "5.5", "message_id": "6.6"})  # same channel, top level -> yes
        assert next(iter(a._native_task_card_streams.values())).superseded
        b = make(mod, interval=0)
        await b.send_native_task_card_progress("C1", [task(1)], metadata={})
        b._supersede_task_cards("C2", None, {})  # another chat
        assert not next(iter(b._native_task_card_streams.values())).superseded
        c = make(mod, interval=0)  # a thread card is only superseded by a message in that thread
        stream = mod._NativeTaskCardStream(team_id="", channel="C1", thread_ts="7.7", stream_ts="100.9")
        c._native_task_card_streams[("", "C1", "7.7")] = stream
        c._supersede_task_cards("C1", None, {"thread_id": "8.8"})
        assert not stream.superseded
        c._supersede_task_cards("C1", None, {"thread_id": "7.7"})
        assert stream.superseded
    run(go())


# ── Claude Code: its own stream, its own heading ─────────────────────────────
def claude_env(mod):
    sa = subagents_mod(mod)
    tracker, orig = sa.SubagentTracker(), sa.TRACKER
    sa.TRACKER = tracker

    def ev(name, **kw):
        tracker.on_claude_event({"t": time.time(), "chat": "C1", "thread": "", "pid": 1,
                                 "event": {"hook_event_name": name, "session_id": "s1", "cwd": "/r", **kw}})
    return sa, tracker, orig, ev


def test_claude_code_gets_its_own_stream_and_the_timeline_stays_in_order(mod):
    sa, tracker, orig, ev = claude_env(mod)
    try:
        async def go():
            a = make(mod, interval=0)
            rec = lambda: tracker.for_chat("C1")[0]
            # Hermes #1: reads the file, launches Claude Code
            await a.send_native_task_card_progress("C1", [task(1, "complete", "terminal - cat test2.js"),
                                                          task(2, title="terminal - claude -p pad")], metadata={})
            assert methods(a) == ["chat.startStream", "chat.appendStream"]
            # Claude's first step arrives: its OWN stream, with its own heading, while Hermes #1 is still open
            ev("UserPromptSubmit", prompt="Pad test2.js to at least 20 lines. Keep it functional.")
            await a._subagent_apply(rec(), "start")
            assert methods(a) == ["chat.startStream", "chat.appendStream"]  # nothing yet: no steps
            ev("PreToolUse", tool_name="Read", tool_use_id="u1", tool_input={"file_path": "/r/test2.js"})
            await a._subagent_apply(rec(), "tool")
            assert methods(a) == ["chat.startStream", "chat.appendStream", "chat.startStream", "chat.appendStream",
                                  "chat.appendStream", "chat.stopStream"]
            claude_first, h1_final, h1_stop = appends(a)[1], appends(a)[2], a.client.calls[-1]
            assert claude_first[1]["ts"] == "100.2" and heading_of(claude_first) == "🤖 Claude Code: Pad test2.js to at least 20 lines."
            assert [c["title"] for c in tasks_of(claude_first)] == ["Reading test2.js"]
            # Hermes #1 is finished (its running row shown complete) and closed: never appended to again
            assert h1_final[1]["ts"] == "100.1" and h1_stop[1]["ts"] == "100.1"
            assert all(c["status"] == "complete" for c in tasks_of(h1_final))
            # Claude carries on in ITS stream, steps updating in place
            ev("PostToolUse", tool_use_id="u1")
            ev("PreToolUse", tool_name="Edit", tool_use_id="u2", tool_input={"file_path": "/r/test2.js"})
            await a._subagent_apply(rec(), "tool")
            call = appends(a)[-1]
            assert call[1]["ts"] == "100.2" and [(c["title"], c["status"]) for c in tasks_of(call)] == [
                ("Reading test2.js", "complete"), ("Editing test2.js", "in_progress")]
            ev("PostToolUse", tool_use_id="u2")
            ev("Stop", last_assistant_message="Padded test2.js to 22 lines.")
            await a._subagent_apply(rec(), "stop")
            final = [c for c in a.client.calls if c[1] and c[1].get("ts") == "100.2" and c[0] == "chat.appendStream"][-1]
            assert heading_of(final) == "🤖 Claude Code · Padded test2.js to 22 lines." and all(c["status"] == "complete" for c in tasks_of(final))
            assert a.client.calls[-1][0] == "chat.stopStream" and a.client.calls[-1][1]["ts"] == "100.2"
            # Hermes carries on: a NEW stream below Claude's, holding only the new steps
            await a.send_native_task_card_progress("C1", [task(1, "complete", "terminal - cat test2.js"),
                                                          task(2, "complete", "terminal - claude -p pad"),
                                                          task(3, title="terminal - node test2.js")], metadata={})
            starts = [c for c in a.client.calls if c[0] == "chat.startStream"]
            assert len(starts) == 3  # Hermes #1, Claude, Hermes #2
            h2 = appends(a)[-1]
            assert h2[1]["ts"] == "100.3" and [c["title"] for c in tasks_of(h2)] == ["Running node test2.js"]
            assert not any(c[1] and c[1].get("ts") == "100.1" for c in a.client.calls[a.client.calls.index(h1_stop) + 1:])
        run(go())
    finally:
        sa.TRACKER = orig


def test_claude_steps_denied_calls_and_a_killed_run(mod):
    sa, tracker, orig, ev = claude_env(mod)
    try:
        async def go():
            a = make(mod, interval=0)
            rec = lambda: tracker.for_chat("C1")[0]
            ev("UserPromptSubmit", prompt="run tests")
            ev("PreToolUse", tool_name="Bash", tool_use_id="u1", tool_input={"command": "pytest -q"})  # denied: no result ever
            await a._subagent_apply(rec(), "tool")
            ev("SessionEnd", reason="other")  # killed: no Stop
            await a._subagent_apply(rec(), "stop")
            final = appends(a)[-1]
            assert heading_of(final) == "🤖 Claude Code · Stopped"
            assert [(c["title"], c["status"]) for c in tasks_of(final)] == [("Running pytest -q · not run", "error")]
            assert a.client.calls[-1][0] == "chat.stopStream" and not a._run_cards
        run(go())
    finally:
        sa.TRACKER = orig


def test_claude_card_is_throttled_and_coalesces_steps(mod):
    sa, tracker, orig, ev = claude_env(mod)
    try:
        async def go():
            a = make(mod, interval=0.2)
            rec = lambda: tracker.for_chat("C1")[0]
            ev("UserPromptSubmit", prompt="x")
            for i in range(3):
                ev("PreToolUse", tool_name="Read", tool_use_id=f"u{i}", tool_input={"file_path": f"/r/f{i}.py"})
                await a._subagent_apply(rec(), "tool")
            claude_appends = [c for c in appends(a) if c[1]["ts"] == "100.1"]
            assert len(claude_appends) == 1 and len(tasks_of(claude_appends[0])) == 1  # first step goes out at once
            await asyncio.sleep(0.5)
            claude_appends = [c for c in appends(a) if c[1]["ts"] == "100.1"]
            assert len(claude_appends) == 2 and len(tasks_of(claude_appends[1])) == 3  # the rest in one update
        run(go())
    finally:
        sa.TRACKER = orig
