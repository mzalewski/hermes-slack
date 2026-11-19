import asyncio
import importlib.util
import pathlib
import sys

_p = pathlib.Path(__file__).resolve().parents[1] / "code_channels.py"
_spec = importlib.util.spec_from_file_location("slack_code_channels", _p)
cc = importlib.util.module_from_spec(_spec)
sys.modules["slack_code_channels"] = cc
_spec.loader.exec_module(cc)
sys.path.insert(0, str(_p.parent))  # flat import of native_stop, as the plugin loader may do


class FakeClient:
    def __init__(self, record_type="agent_channel", fail=False):
        self.record_type, self.fail, self.calls = record_type, fail, []

    async def conversations_info(self, channel):
        if self.fail:
            raise RuntimeError("boom")
        return {"channel": {"properties": {"record_channel": {"record_type": self.record_type}}}}

    async def api_call(self, method, json=None):
        self.calls.append((method, json))


class Adapter(cc.SlackCodeChannelMixin):
    def __init__(self, client):
        self.client = client
        self.handled = []
        self.slash = []
        self.cleared = []
        self.authorized = True
        self.gated = False
        self._init_code_channel_state()

    @staticmethod
    def _event_team_id(event, body=None):
        return "T1"

    def _slash_channel_gated(self, channel_id):
        return self.gated

    def _early_reject_unauthorized(self, user_id, channel_id, is_dm, *, thread_id=None, is_bot=False):
        return not self.authorized

    async def _handle_slash_command(self, command):
        self.slash.append(command)

    async def stop_typing(self, chat_id, metadata=None):
        self.cleared.append((chat_id, metadata))

    def _get_client(self, _chat_id, team_id=None):
        return self.client

    async def _handle_slack_message(self, event, body=None):
        self.handled.append(event)


def run(coro):
    return asyncio.run(coro)


def test_detects_code_channel_and_caches():
    a = Adapter(FakeClient())
    run(a._detect_code_channel("C1"))
    assert a._is_known_code_channel("C1")


def test_regular_channel_not_code():
    a = Adapter(FakeClient(record_type="standard"))
    run(a._detect_code_channel("C1"))
    assert not a._is_known_code_channel("C1")
    assert a._code_channels["C1"] is False


def test_lookup_failure_not_cached_and_dms_skipped():
    a = Adapter(FakeClient(fail=True))
    run(a._detect_code_channel("C1"))
    assert "C1" not in a._code_channels
    run(a._detect_code_channel("D1"))
    assert "D1" not in a._code_channels


def test_disabled_flag_skips_detection(monkeypatch):
    monkeypatch.setenv("SLACK_CODE_CHANNELS", "false")
    a = Adapter(FakeClient())
    run(a._detect_code_channel("C1"))
    assert not a._is_known_code_channel("C1")


def test_top_level_replies_flat_unless_user_threads():
    a = Adapter(FakeClient())
    run(a._detect_code_channel("C1"))
    run(a._note_code_channel_message({"channel": "C1", "ts": "1.0"}))
    assert a._code_channel_reply_is_flat("1.0", {"thread_id": "1.0"})
    run(a._note_code_channel_message({"channel": "C1", "ts": "2.0", "thread_ts": "1.0"}))
    assert not a._code_channel_reply_is_flat("1.0", {"thread_id": "1.0"})


def test_concurrent_channels_have_independent_state():
    a = Adapter(FakeClient())
    run(a._detect_code_channel("C1"))
    run(a._note_code_channel_message({"channel": "C1", "ts": "1.0"}))
    assert not a._code_channel_reply_is_flat("9.9", {"thread_id": "9.9"})


def test_status_processing_once_then_active():
    a = Adapter(FakeClient())

    async def go():
        await a._set_code_channel_status("C1", "processing")
        await a._set_code_channel_status("C1", "processing")
        await a._set_code_channel_status("C1", "active")

    run(go())
    assert [c[1]["status"] for c in a.client.calls] == ["processing", "active"]


def test_native_stop_in_code_channel_routes_as_channel_message():
    a = Adapter(FakeClient())
    a._code_channel_busy.add("C1")
    run(a._handle_agent_session_stopped({"channel": "C1", "user": "U1", "event_ts": "5.0"}))
    assert a.handled[0]["text"] == "/stop" and "thread_ts" not in a.handled[0]
    assert "C1" not in a._code_channel_busy
    assert a.client.calls[-1] == ("agents.sessions.setStatus", {"channel_id": "C1", "status": "active"})


def test_native_stop_in_thread_runs_slash_stop_and_clears_status():
    a = Adapter(FakeClient())
    run(a._handle_agent_session_stopped({"channel": "C1", "user": "U1", "thread_ts": "1.0"}))
    assert a.slash == [{"command": "/stop", "text": "", "user_id": "U1", "channel_id": "C1",
                        "team_id": "T1", "thread_ts": "1.0"}]
    assert a.cleared and a.handled == []


def test_native_stop_unauthorized_or_gated_changes_nothing():
    for attr, val in (("authorized", False), ("gated", True)):
        a = Adapter(FakeClient())
        setattr(a, attr, val)
        run(a._handle_agent_session_stopped({"channel": "C1", "user": "U1", "thread_ts": "1.0"}))
        run(a._handle_agent_session_stopped({"channel": "C1", "user": "U1"}))
        assert a.slash == [] and a.handled == [] and a.client.calls == []


def test_native_stop_disabled_flag(monkeypatch):
    monkeypatch.setenv("SLACK_NATIVE_STOP", "false")
    a = Adapter(FakeClient())
    run(a._handle_agent_session_stopped({"channel": "C1", "user": "U1", "thread_ts": "1.0"}))
    assert a.slash == [] and a.handled == []
