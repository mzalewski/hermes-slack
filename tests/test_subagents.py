import asyncio
import importlib.util
import pathlib
import sys
import types

_dir = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_dir))  # flat import of task_labels, as the loader may do
_spec = importlib.util.spec_from_file_location("slack_subagents", _dir / "subagents.py")
sa = importlib.util.module_from_spec(_spec)
sys.modules["slack_subagents"] = sa
_spec.loader.exec_module(sa)


def run(coro):
    return asyncio.run(coro)


def slack_env(monkeypatch, chat="C1", thread=""):
    monkeypatch.setattr(sa, "_session_env", lambda: ("slack", chat, thread))


def tracker(monkeypatch):
    t = sa.SubagentTracker()
    monkeypatch.setattr(sa, "TRACKER", t)
    return t


def start(t, sid="s1", goal="Audit tokens", session="cs1", parent="p1"):
    t.on_start(child_subagent_id=sid, child_goal=goal, child_session_id=session, parent_session_id=parent, child_role="leaf")


def test_start_tracked_only_for_slack_turns(monkeypatch):
    t = tracker(monkeypatch)
    monkeypatch.setattr(sa, "_session_env", lambda: ("telegram", "123", ""))
    start(t)
    assert t.get("s1") is None
    slack_env(monkeypatch)
    start(t)
    assert t.get("s1").chat_id == "C1" and t.get("s1").status == "running"


def test_target_falls_back_to_parent_session(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch, chat="C7", thread="9.9")
    t.remember_turn_target("p1")
    monkeypatch.setattr(sa, "_session_env", lambda: ("", "", ""))  # hook ran off the turn's context
    start(t, parent="p1")
    assert (t.get("s1").chat_id, t.get("s1").thread_ts) == ("C7", "9.9")
    start(t, sid="s2", parent="unknown")
    assert t.get("s2") is None


def test_tool_events_attributed_by_task_or_session_id(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    t.on_tool(task_id="s1", session_id="x", tool_name="read_file", args={"path": "a.py"})
    t.on_tool(task_id="", session_id="cs1", tool_name="terminal", args={"command": "pytest -q"})
    t.on_tool(task_id="other", session_id="other", tool_name="read_file", args={})
    rec = t.get("s1")
    assert rec.tool_count == 2 and rec.last_tool == "Running pytest -q"


def test_stop_sets_status_summary_and_ignores_late_tools(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    t.on_stop(child_session_id="cs1", child_status="completed", child_summary="All good")
    rec = t.get("s1")
    assert rec.status == "complete" and rec.summary == "All good" and not rec.live
    t.on_tool(task_id="s1", tool_name="read_file", args={})
    assert rec.tool_count == 0
    start(t, sid="s2", session="cs2")
    t.on_stop(child_session_id="cs2", child_status="interrupted")
    assert t.get("s2").status == "interrupted"
    start(t, sid="s3", session="cs3")
    t.on_stop(child_session_id="cs3", child_status="failed")
    assert t.get("s3").status == "failed"


def test_chats_and_threads_are_isolated(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch, chat="C1", thread="1.0")
    start(t, sid="a")
    slack_env(monkeypatch, chat="C2", thread="2.0")
    start(t, sid="b")
    slack_env(monkeypatch, chat="C1", thread="3.0")
    start(t, sid="c")
    assert [r.subagent_id for r in t.for_chat("C1")] == ["a", "c"]          # flat: whole channel
    assert [r.subagent_id for r in t.for_chat("C1", "1.0", flat=False)] == ["a"]  # thread: its own
    assert t.for_chat("C3") == []


class FakeClient:
    def __init__(self):
        self.ephemerals = []

    async def chat_postEphemeral(self, channel, user, text):
        self.ephemerals.append((channel, user, text))


class Mixin(sa.SlackSubagentMixin):
    def __init__(self):
        self.client = FakeClient()
        self.authorized = True
        self.gated = False
        self.rejected = False
        self._native_task_card_streams = {}
        self._subagent_loop = None
        self._subagent_view_last = {}
        self._subagent_view_pending = {}

    async def _begin_interaction(self, ack, body, action, kind):
        await ack()
        if not self.authorized:
            return None
        return ("T1", action["action_id"], action["value"], {}, "", "", "name", body["user"]["id"])

    def _slash_channel_gated(self, chat_id):
        return self.gated

    def _early_reject_unauthorized(self, user_id, chat_id, is_dm, *, thread_id=None, is_bot=False):
        return self.rejected

    def _get_client(self, chat_id, team_id=None):
        return self.client

    def _is_known_code_channel(self, chat_id):
        return False


def click(m, action_id, value, user="U1"):
    async def ack():
        pass
    run(m._handle_subagent_action(ack, {"user": {"id": user}}, {"action_id": action_id, "value": value}))


def setup_stop(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    calls = []
    fake = types.ModuleType("tools.delegate_tool_registry")
    fake.interrupt_subagent = lambda sid: calls.append(sid) or True
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", fake)
    return t, calls


def test_stop_authorized_interrupts_and_marks_stopping(monkeypatch):
    t, calls = setup_stop(monkeypatch)
    m = Mixin()
    click(m, sa.STOP_ACTION, "s1")
    assert calls == ["s1"] and t.get("s1").status == "stopping"
    assert "Stop requested" in m.client.ephemerals[-1][2]


def test_stop_ignored_when_unauthorized_gated_rejected_or_unknown(monkeypatch):
    t, calls = setup_stop(monkeypatch)
    for attr in ("authorized", "gated", "rejected"):
        m = Mixin()
        setattr(m, attr, attr == "gated" or attr == "rejected")
        if attr == "authorized":
            m.authorized = False
        click(m, sa.STOP_ACTION, "s1")
    click(Mixin(), sa.STOP_ACTION, "does-not-exist")
    assert calls == [] and t.get("s1").status == "running"


def test_stop_noop_when_finished_or_controls_disabled(monkeypatch):
    t, calls = setup_stop(monkeypatch)
    m = Mixin()
    monkeypatch.setenv("SLACK_SUBAGENT_CONTROLS", "false")
    click(m, sa.STOP_ACTION, "s1")
    monkeypatch.delenv("SLACK_SUBAGENT_CONTROLS")
    t.on_stop(child_session_id="cs1", child_status="completed")
    click(m, sa.STOP_ACTION, "s1")
    assert calls == []
    assert "already finished" in m.client.ephemerals[-1][2]


def test_result_button_shows_summary_privately(monkeypatch):
    t, _ = setup_stop(monkeypatch)
    t.on_stop(child_session_id="cs1", child_status="completed", child_summary="Found 3 issues")
    m = Mixin()
    click(m, sa.RESULT_ACTION, "s1", user="U5")
    chat, user, text = m.client.ephemerals[-1]
    assert (chat, user) == ("C1", "U5") and "Found 3 issues" in text


def test_card_rows_per_flat_and_thread_streams(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch, chat="C1", thread="1.0")
    start(t)
    t.on_tool(task_id="s1", tool_name="read_file", args={"path": "a.py"})
    m = Mixin()
    S = lambda ch, th: types.SimpleNamespace(channel=ch, thread_ts=th, log_cursor={}, cursor_next={})
    flat, mine, other_thread, other_chat = S("C1", ""), S("C1", "1.0"), S("C1", "2.0"), S("C2", "")
    row = m._subagent_task_chunks(flat)[0]
    assert row["id"] == "sub-s1" and row["status"] == "in_progress" and row["details"] == "Opening a.py"
    assert len(m._subagent_task_chunks(mine)) == 1
    assert m._subagent_task_chunks(other_thread) == [] and m._subagent_task_chunks(other_chat) == []


def test_panel_flag_disables_rows_and_hooks(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    monkeypatch.setenv("SLACK_SUBAGENT_PANEL", "false")
    assert Mixin()._subagent_task_chunks(types.SimpleNamespace(channel="C1", thread_ts="", log_cursor={}, cursor_next={})) == []
    registered = {}
    ctx = types.SimpleNamespace(register_hook=lambda name, fn, **k: registered.setdefault(name, fn))
    sa.register_hooks(ctx)
    assert set(registered) == {"pre_llm_call", "subagent_start", "subagent_stop", "pre_tool_call"}
    registered["subagent_start"](child_subagent_id="zz", child_goal="x")  # disabled: no record
    assert t.get("zz") is None


# ── details are appended by Slack: send only new lines ───────────────────────
def S(ch="C1", th=""):
    return types.SimpleNamespace(channel=ch, thread_ts=th, log_cursor={}, cursor_next={}, closing=False)


def test_details_send_only_new_lines_with_leading_newline(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    m, st = Mixin(), S()
    t.on_tool(task_id="s1", tool_name="read_file", args={"path": "a.py"})
    first = m._subagent_task_chunks(st)[0]
    assert first["details"] == "Opening a.py"
    st.log_cursor.update(st.cursor_next)  # the caller commits after a successful send
    assert "details" not in m._subagent_task_chunks(st)[0]  # nothing new: status-only update
    t.on_tool(task_id="s1", tool_name="terminal", args={"command": "pytest"})
    t.on_tool(task_id="s1", tool_name="read_file", args={"path": "b.py"})
    second = m._subagent_task_chunks(st)[0]
    assert second["details"] == "\nRunning pytest\nOpening b.py"  # starts with \n so it lands on its own line


def test_cursor_not_advanced_until_committed(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    m, st = Mixin(), S()
    t.on_tool(task_id="s1", tool_name="read_file", args={"path": "a.py"})
    m._subagent_task_chunks(st)
    again = m._subagent_task_chunks(st)  # the send failed, so the line must go out again
    assert again[0]["details"] == "Opening a.py"


def test_closing_hands_off_live_row_once(monkeypatch):
    t = tracker(monkeypatch)
    slack_env(monkeypatch)
    start(t)
    m, st = Mixin(), S()
    st.closing = True
    row = m._subagent_task_chunks(st)[0]
    assert row["status"] == "complete" and "background" in row["details"]


# ── Claude Code runs listed as subagents ─────────────────────────────────────
def cc(t, name, sid="sess1", ts=100.0, chat="C1", thread="", **ev):
    t.on_claude_event({"t": ts, "chat": chat, "thread": thread, "pid": 4242,
                       "event": {"hook_event_name": name, "session_id": sid, "cwd": "/repo", **ev}})


def test_claude_run_lifecycle_and_titles(monkeypatch):
    t = tracker(monkeypatch)
    cc(t, "UserPromptSubmit", prompt="fix the add bug", ts=100)
    cc(t, "PreToolUse", tool_name="Read", tool_use_id="u1", tool_input={"file_path": "/repo/calc.py"}, ts=101)
    cc(t, "PostToolUse", tool_name="Read", tool_use_id="u1", ts=102)
    cc(t, "PreToolUse", tool_name="Edit", tool_use_id="u2", tool_input={"file_path": "/repo/calc.py"}, ts=103)
    cc(t, "PostToolUseFailure", tool_name="Edit", tool_use_id="u2", error="old_string not found", ts=104)
    cc(t, "Stop", last_assistant_message="Fixed it.", ts=110)
    rec = t.for_chat("C1")[0]
    assert rec.kind == "claude" and rec.status == "complete" and rec.summary == "Fixed it."
    assert rec.log[:3] == ["Reading calc.py", "Editing calc.py", "✗ Failed: old_string not found"]
    assert rec.log[-1].startswith("✓ Done")
    row = Mixin()._subagent_task_chunks(S())[0]
    assert row["title"] == "🤖 Claude Code: fix the add bug" and row["status"] == "complete"


def test_claude_denied_call_is_closed_as_not_run_on_stop(monkeypatch):
    t = tracker(monkeypatch)
    cc(t, "UserPromptSubmit", prompt="run tests", ts=100)
    cc(t, "PreToolUse", tool_name="Bash", tool_use_id="u1", tool_input={"command": "pytest -q"}, ts=101)  # never gets a result
    cc(t, "Stop", ts=105)
    rec = t.for_chat("C1")[0]
    assert "✗ Not run: Running pytest -q" in rec.log and not rec.open_calls and not rec.live


def test_claude_killed_run_closes_on_session_end_as_interrupted(monkeypatch):
    t = tracker(monkeypatch)
    cc(t, "UserPromptSubmit", prompt="x", ts=100)
    cc(t, "SessionEnd", ts=101)
    assert t.for_chat("C1")[0].status == "interrupted"


def test_claude_events_without_prompt_still_tracked_and_title_has_prefix(monkeypatch):
    t = tracker(monkeypatch)
    cc(t, "PreToolUse", tool_name="Grep", tool_use_id="u1", tool_input={"pattern": "TODO"}, ts=100)
    row = Mixin()._subagent_task_chunks(S())[0]
    assert row["title"] == "🤖 Claude Code" and row["details"] == "Searching TODO"


def test_claude_second_prompt_in_same_session_is_a_new_row_and_chats_isolated(monkeypatch):
    t = tracker(monkeypatch)
    cc(t, "UserPromptSubmit", prompt="one", ts=100)
    cc(t, "Stop", ts=101)
    cc(t, "UserPromptSubmit", prompt="two", ts=200)
    cc(t, "UserPromptSubmit", prompt="other chat", sid="sess2", chat="C2", ts=201)
    assert [r.goal for r in t.for_chat("C1")] == ["one", "two"]
    assert [r.goal for r in t.for_chat("C2")] == ["other chat"]


def test_polish_title_error_and_paths(monkeypatch):
    long_prompt = 'The file test.js currently just contains: echo "hi"; This is not even valid JS (echo is not a JS function) so rewrite it'
    assert sa._short_title(long_prompt) == "The file test.js currently just contains:"
    assert sa._short_title("Fix the add bug in calc.py") == "Fix the add bug in calc.py"
    assert len(sa._short_title("word " * 40)) <= 60 and sa._short_title("word " * 40).endswith("…")
    assert sa._short_error("Exit code 1\n. .. .git test.js") == "Exit code 1"
    assert sa._short_error("File does not exist. Note: your current working directory is /x") == "File does not exist. Note: your current working directory is /x"[:60]
    assert sa._claude_preview({"command": "node /home/me/dev/test1/test.js"}, "/home/me/dev/test1") == "node test.js"
    assert sa._claude_preview({"file_path": "/home/me/dev/test1/src/a.py"}, "/home/me/dev/test1") == "src/a.py"
    assert sa._claude_preview({"command": "ls /etc"}, "/home/me/dev/test1") == "ls /etc"  # other paths untouched
