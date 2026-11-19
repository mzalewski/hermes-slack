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
