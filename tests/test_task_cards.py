"""Flat, throttled native task cards in Code channels. Needs the Hermes venv (skips without it)."""
import asyncio
import importlib
import pathlib
import sys
import types

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
    import importlib.util
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

    async def api_call(self, method, json=None):
        self.calls.append((method, json))
        return {"ok": True, "ts": "100.1"}


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


def test_flat_start_has_no_thread_and_no_plan_title(mod):
    a = make(mod)
    r = run(a.send_native_task_card_progress("C1", [task(1)], metadata={}))
    assert r.success
    method, payload = a.client.calls[0]
    assert method == "chat.startStream" and "thread_ts" not in payload and payload["task_display_mode"] == "plan"
    chunks = a.client.calls[1][1]["chunks"]
    assert all(c["type"] == "task_update" for c in chunks)
    assert chunks[0]["title"] == "Opening f1.py"


def test_updates_throttled_and_latest_wins_then_flushed(mod):
    async def go():
        a = make(mod, interval=0.2)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete"), task(2, "complete"), task(3)], metadata={})
        appends = [c for c in a.client.calls if c[0] == "chat.appendStream"]
        assert len(appends) == 1  # the two later frames are held back
        await asyncio.sleep(0.4)
        appends = [c for c in a.client.calls if c[0] == "chat.appendStream"]
        assert len(appends) == 2 and len(appends[1][1]["chunks"]) == 3  # only the latest frame
        return a
    run(go())


def test_stop_flushes_pending_then_stops(mod):
    async def go():
        a = make(mod, interval=30)
        await a.send_native_task_card_progress("C1", [task(1)], metadata={})
        await a.send_native_task_card_progress("C1", [task(1, "complete")], metadata={})
        await a.stop_native_task_card_progress("C1", metadata={})
        methods = [c[0] for c in a.client.calls]
        assert methods == ["chat.startStream", "chat.appendStream", "chat.appendStream", "chat.stopStream"]
        assert a.client.calls[2][1]["chunks"][0]["status"] == "complete"
    run(go())


def test_non_code_channel_without_thread_is_not_cardable(mod):
    a = make(mod, code=False)
    r = run(a.send_native_task_card_progress("C2", [task(1)], metadata={}))
    assert not r.success


def test_flag_disables_flat_cards(mod, monkeypatch):
    monkeypatch.setenv("SLACK_CODE_CHANNEL_TASK_CARDS", "false")
    a = make(mod)
    assert not run(a.send_native_task_card_progress("C1", [task(1)], metadata={})).success


def test_friendly_titles(mod):
    f = mod._friendly_title
    assert f("web_search - refresh tokens") == "Checking the web: refresh tokens"
    assert f("terminal - pytest -q") == "Running pytest -q"
    assert f("todo") == "Updating plan"
    assert f("mystery_tool - x") == "mystery_tool - x"


def test_subagent_rows_appear_on_card_and_refresh_pushes_them(mod, monkeypatch):
    sa = sys.modules[mod.__name__.rsplit(".", 1)[0] + ".subagents"]
    tracker = sa.SubagentTracker()
    monkeypatch_target, env_target = sa.TRACKER, sa._session_env
    sa.TRACKER = tracker
    try:
        monkeypatch.setenv("SLACK_SUBAGENT_PANEL", "true")
        sa._session_env = lambda: ("slack", "C1", "")

        async def go():
            a = make(mod, interval=0)
            await a.send_native_task_card_progress("C1", [task(1)], metadata={})
            tracker.on_start(child_subagent_id="s1", child_goal="Audit token expiry", child_session_id="cs1",
                             parent_session_id="p1")
            tracker.on_tool(task_id="s1", tool_name="read_file", args={"path": "auth.py"})
            await a._subagent_apply(tracker.get("s1"), "tool")
            last = [c for c in a.client.calls if c[0] == "chat.appendStream"][-1][1]["chunks"]
            rows = [c for c in last if c["id"].startswith("sub-")]
            assert len(rows) == 1 and rows[0]["title"].startswith("🔀 Audit token expiry")
            assert rows[0]["details"] == "Opening auth.py"
            # core's own task is still there and keeps its plain-language title
            assert last[0]["title"] == "Opening f1.py"
            tracker.on_stop(child_session_id="cs1", child_status="completed", child_summary="done")
            await a._subagent_apply(tracker.get("s1"), "stop")
            last = [c for c in a.client.calls if c[0] == "chat.appendStream"][-1][1]["chunks"]
            assert [c for c in last if c["id"].startswith("sub-")][0]["status"] == "complete"
        run(go())
    finally:
        sa.TRACKER, sa._session_env = monkeypatch_target, env_target


def test_card_close_hands_off_running_subagent_rows(mod):
    sa = sys.modules[mod.__name__.rsplit(".", 1)[0] + ".subagents"]
    tracker, orig_t, orig_e = sa.SubagentTracker(), sa.TRACKER, sa._session_env
    sa.TRACKER = tracker
    sa._session_env = lambda: ("slack", "C1", "")
    try:
        async def go():
            a = make(mod, interval=0)
            await a.send_native_task_card_progress("C1", [task(1, "complete")], metadata={})
            tracker.on_start(child_subagent_id="s1", child_goal="Count files", child_session_id="cs1", parent_session_id="p1")
            await a._subagent_apply(tracker.get("s1"), "start")
            mid = [c for c in a.client.calls if c[0] == "chat.appendStream"][-1][1]["chunks"]
            assert [c for c in mid if c["id"] == "sub-s1"][0]["status"] == "in_progress"
            assert "details" not in mid[-1]  # no tool lines yet: a status-only update, never "0 tools"
            await a.stop_native_task_card_progress("C1", metadata={})
            methods = [c[0] for c in a.client.calls]
            assert methods[-1] == "chat.stopStream"
            final = [c for c in a.client.calls if c[0] == "chat.appendStream"][-1][1]["chunks"]
            row = [c for c in final if c["id"] == "sub-s1"][0]
            assert row["status"] == "complete" and "background" in row["details"]
            assert not any(c["status"] == "in_progress" for c in final)  # nothing left open at close
        run(go())
    finally:
        sa.TRACKER, sa._session_env = orig_t, orig_e


def test_claude_code_row_streams_one_new_line_per_frame_and_closes_cleanly(mod):
    sa = sys.modules[mod.__name__.rsplit(".", 1)[0] + ".subagents"]
    tracker, orig_t = sa.SubagentTracker(), sa.TRACKER
    sa.TRACKER = tracker
    try:
        def ev(name, **kw):
            tracker.on_claude_event({"t": __import__("time").time(), "chat": "C1", "thread": "", "pid": 1,
                                     "event": {"hook_event_name": name, "session_id": "s1", "cwd": "/r", **kw}})

        async def go():
            a = make(mod, interval=0)
            rows = lambda: [c for c in [x for x in a.client.calls if x[0] == "chat.appendStream"][-1][1]["chunks"] if c["id"].startswith("sub-")]
            await a.send_native_task_card_progress("C1", [task(1)], metadata={})
            ev("UserPromptSubmit", prompt="fix add")
            await a._subagent_apply(tracker.for_chat("C1")[0], "start")
            assert rows()[0]["title"] == "🤖 Claude Code: fix add" and "details" not in rows()[0]
            ev("PreToolUse", tool_name="Read", tool_use_id="u1", tool_input={"file_path": "/r/calc.py"})
            await a._subagent_apply(tracker.for_chat("C1")[0], "tool")
            assert rows()[0]["details"] == "Reading calc.py"
            ev("PreToolUse", tool_name="Bash", tool_use_id="u2", tool_input={"command": "pytest"})
            await a._subagent_apply(tracker.for_chat("C1")[0], "tool")
            assert rows()[0]["details"] == "\nRunning pytest"  # only the new line, on its own line
            ev("Stop", last_assistant_message="done")  # u1/u2 never got a result event
            await a._subagent_apply(tracker.for_chat("C1")[0], "stop")
            last = rows()[0]
            assert last["status"] == "complete" and "Not run: Reading calc.py" in last["details"] and "Done" in last["details"]
            await a.stop_native_task_card_progress("C1", metadata={})
            assert [c[0] for c in a.client.calls][-1] == "chat.stopStream"
        run(go())
    finally:
        sa.TRACKER = orig_t
