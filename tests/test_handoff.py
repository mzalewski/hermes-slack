import asyncio
import importlib.util
import json
import pathlib
import sys
import threading
import types

_dir = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_dir))
_spec = importlib.util.spec_from_file_location("slack_handoff", _dir / "handoff.py")
h = importlib.util.module_from_spec(_spec)
sys.modules["slack_handoff"] = h
_spec.loader.exec_module(h)


def run(coro):
    return asyncio.run(coro)


# ── names and projects ──
def test_slug_and_channel_name():
    assert h.slugify("Fix login-redirect bug!!") == "fix-login-redirect-bug"
    assert h.slugify("  ///  ") == ""
    assert h.channel_name("curate", "Fix login redirect") == "curate-fix-login-redirect"
    assert h.channel_name("curate", "!!!") == "curate-task"
    assert len(h.channel_name("curate", "x" * 200)) <= 80
    assert h.channel_name("Curate", "Add CSV export (v2)") == "curate-add-csv-export-v2"


def test_discover_projects_from_profile_dirs_and_env(tmp_path, monkeypatch):
    for name in ("curate-bot", "lifedash-bot", "sip-bot", "notabot", "-bot"):
        (tmp_path / name).mkdir()
    (tmp_path / "somefile-bot").write_text("x")  # a file, not a profile
    monkeypatch.delenv("SLACK_HANDOFF_PROJECTS", raising=False)
    assert h.discover_projects(tmp_path) == {"curate": "curate-bot", "lifedash": "lifedash-bot", "sip": "sip-bot"}
    monkeypatch.setenv("SLACK_HANDOFF_PROJECTS", "web=web-profile, curate=curate-v2")
    found = h.discover_projects(tmp_path)
    assert found["web"] == "web-profile" and found["curate"] == "curate-v2"  # env wins on conflict
    assert h.discover_projects(tmp_path / "missing") == {"web": "web-profile", "curate": "curate-v2"}


def test_tool_schema_lists_projects_and_requires_the_three_fields():
    s = h.tool_schema({"curate": "curate-bot", "sip": "sip-bot"})
    assert s["name"] == "slack_code_channel" and "curate, sip" in s["description"]
    assert s["parameters"]["required"] == ["project", "task_name", "task"]


# ── the handler: every refusal is a JSON error, never an exception ──
def env(monkeypatch, **values):
    base = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_PROFILE": "", "HERMES_SESSION_CHAT_ID": "C1",
            "HERMES_SESSION_MESSAGE_ID": "1.5", "HERMES_SESSION_USER_ID": "U1"}
    base.update(values)
    monkeypatch.setattr(h, "_session_env", lambda name: base.get(name, ""))
    monkeypatch.setattr(h, "discover_projects", lambda *a, **k: {"curate": "curate-bot"})
    monkeypatch.delenv("SLACK_HANDOFF_FROM", raising=False)
    monkeypatch.delenv("SLACK_HANDOFF", raising=False)


def call(**args):
    return json.loads(h.handoff_tool(args))


GOOD = dict(project="curate", task_name="fix login", task="Fix the redirect.")


def test_handler_refusals(monkeypatch):
    env(monkeypatch, HERMES_SESSION_PLATFORM="telegram")
    assert "only works from a Slack" in call(**GOOD)["error"]
    env(monkeypatch, HERMES_SESSION_PROFILE="curate-bot")
    assert "isn't set up to hand work over" in call(**GOOD)["error"]  # a project bot is not a dispatcher
    env(monkeypatch)
    assert "all required" in call(project="curate", task_name="", task="x")["error"]
    r = call(project="nope", task_name="a", task="b")
    assert r["ok"] is False and "Unknown project 'nope'" in r["error"] and "curate" in r["error"]
    assert "isn't connected" in call(**GOOD)["error"]  # no adapter registered
    monkeypatch.setenv("SLACK_HANDOFF", "false")
    assert "disabled" in call(**GOOD)["error"]


def test_dispatchers_can_be_configured(monkeypatch):
    env(monkeypatch, HERMES_SESSION_PROFILE="curate-bot")
    monkeypatch.setenv("SLACK_HANDOFF_FROM", "default, curate-bot")
    assert "isn't connected" in call(**GOOD)["error"]  # allowed through to the next check


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class FakeDelegate:
    def __init__(self, loop):
        self._main_loop = loop
        self.calls = []
        self._owner_profile = "curate-bot"

    async def handoff_create(self, **kw):
        self.calls.append(kw)
        return {"ok": True, "channel_id": "C99", "channel_mention": "<#C99>", "name": kw["name"]}


def test_handler_calls_the_delegate_adapter_on_its_loop(monkeypatch):
    env(monkeypatch)
    lt = LoopThread()
    try:
        d = FakeDelegate(lt.loop)
        h.register_adapter(d)
        r = call(**GOOD)
        assert r["ok"] and r["channel_id"] == "C99" and r["delegate"] == "curate-bot" and r["project"] == "curate"
        k = d.calls[0]
        assert k["name"] == "curate-fix-login" and k["task"] == "Fix the redirect." and k["user_id"] == "U1"
        assert (k["origin_channel"], k["origin_ts"]) == ("C1", "1.5")
        assert k["session_id"] == "handoff-C1-1.5-fix-login"  # stable: a retry maps to the same channel
        h.unregister_adapter(d)
        assert "isn't connected" in call(**GOOD)["error"]
    finally:
        lt.stop()


def test_registry_is_shared_across_module_copies():
    spec = importlib.util.spec_from_file_location("slack_handoff_copy2", _dir / "handoff.py")
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)  # a second profile's copy of the plugin
    d = types.SimpleNamespace(_owner_profile="sip-bot")
    other.register_adapter(d)
    assert h._registry().get("sip-bot") is d
    h.unregister_adapter(d)
    assert "sip-bot" not in other._registry()
    assert h._profile_key(None) == "default" and h._profile_key("main") == "default"


# ── adapter side ──
class FakeClient:
    def __init__(self, fail_with_origin=False, fail_all=False, channel="C99"):
        self.calls, self.fail_with_origin, self.fail_all, self.channel = [], fail_with_origin, fail_all, channel

    async def api_call(self, method, json=None):
        self.calls.append((method, dict(json)))
        if self.fail_all or (self.fail_with_origin and "origin_channel_id" in json):
            raise RuntimeError("invalid_origin")
        return {"ok": True, "channel_id": self.channel}


class Delegate(h.SlackHandoffMixin):
    def __init__(self, client, authorized=True, gated=False):
        self.client, self.authorized, self.gated = client, authorized, gated
        self._code_channels, self.dispatched = {}, []
        self._init_handoff_state()

    def _early_reject_unauthorized(self, user_id, channel_id, is_dm, **kw):
        return not self.authorized

    def _get_client(self, chat_id, team_id=None):
        return self.client

    def _slash_channel_gated(self, channel_id):
        return self.gated

    @staticmethod
    def _event_team_id(event, body=None, install_set=None):
        return "T1"

    async def _handle_slack_message(self, event, body=None):
        self.dispatched.append((event, body))


def create(d, **over):
    kw = dict(name="curate-fix-login", session_id="s1", task="Fix it.", origin_channel="C1", origin_ts="1.5", user_id="U1")
    kw.update(over)

    async def go():
        r = await d.handoff_create(**kw)
        await asyncio.sleep(0)  # let the scheduled first turn run
        return r
    return run(go())


def test_creates_the_channel_as_the_delegate_and_starts_its_first_turn_as_the_user():
    d = Delegate(FakeClient())
    r = create(d)
    assert r == {"ok": True, "channel_id": "C99", "channel_mention": "<#C99>", "name": "curate-fix-login"}
    assert d.client.calls == [("agents.conversations.create", {
        "name": "curate-fix-login", "session_id": "s1", "origin_channel_id": "C1", "origin_message_ts": "1.5"})]
    assert d._code_channels["C99"] is True
    (event, body), = d.dispatched
    assert event["channel"] == "C99" and event["user"] == "U1" and event["channel_type"] == "channel"
    assert event["text"].startswith("Fix it.") and "<#C1>" in event["text"] and "<@U1>" in event["text"]
    assert body["team_id"] == "T1" and body["event"] is event


def test_retry_returns_the_same_channel_and_never_runs_the_task_twice():
    d = Delegate(FakeClient())
    create(d)
    again = create(d)
    assert again["repeat"] is True and again["channel_id"] == "C99"
    assert len(d.dispatched) == 1 and len(d.client.calls) == 1


def test_falls_back_to_no_origin_link_when_slack_refuses_it():
    d = Delegate(FakeClient(fail_with_origin=True))
    r = create(d)
    assert r["ok"] and [list(c[1]) for c in d.client.calls] == [["name", "session_id", "origin_channel_id", "origin_message_ts"], ["name", "session_id"]]
    assert "<#C1>" in d.dispatched[0][0]["text"]  # the footer still says where it came from


def test_no_origin_message_means_no_origin_params():
    d = Delegate(FakeClient())
    create(d, origin_ts="")
    assert list(d.client.calls[0][1]) == ["name", "session_id"]


def test_refusals_create_nothing():
    d = Delegate(FakeClient(), authorized=False)
    assert "isn't authorised" in create(d)["error"] and d.client.calls == [] and d.dispatched == []
    assert "isn't authorised" in create(Delegate(FakeClient()), user_id="")["error"]
    f = Delegate(FakeClient(fail_all=True))
    assert "refused to create" in create(f)["error"] and f.dispatched == []
    g = Delegate(FakeClient(), gated=True)  # the delegate's own allowed/ignored channel rules would drop the task
    r = create(g)
    assert r["ok"] is False and r["channel_id"] == "C99" and "ignores it" in r["error"] and g.dispatched == []
    assert create(g, session_id="s2")["ok"] is False  # and nothing is remembered as handed off


def test_adapter_registers_while_online_and_clears_when_offline():
    async def go():
        d = Delegate(FakeClient())
        d._owner_profile = "wishlist-bot"
        d._handoff_online()
        assert h._registry()["wishlist-bot"] is d and d._main_loop is asyncio.get_running_loop()
        d._handoff_offline()
        assert "wishlist-bot" not in h._registry() and d._main_loop is None
    run(go())
