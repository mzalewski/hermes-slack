import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import types

_dir = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_dir))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _dir / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cc = load("slack_claude_code", "claude_code.py")
inst = load("slack_cc_install", "claude_code_install.py")
sa = load("slack_subagents_cc", "subagents.py")
HOOK = _dir / "claude_code_hook.py"


def run_hook(tmp_path, event, env_extra=None, base=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_SLACK_CLAUDE_EVENTS": str(tmp_path / "ev")}
    env.update(base if base is not None else {"HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_PLATFORM": "slack",
                                               "HERMES_SESSION_THREAD_ID": "1.0"})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True, env=env, capture_output=True)


# ── hook script ──
def test_hook_writes_trimmed_private_event(tmp_path):
    ev = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Edit", "tool_use_id": "u1", "cwd": "/r",
          "tool_input": {"file_path": "/r/a.py", "new_string": "SECRET=hunter2", "old_string": "x"},
          "tool_response": {"big": "x" * 5000}}
    r = run_hook(tmp_path, ev)
    assert r.returncode == 0
    f = tmp_path / "ev" / "C1.jsonl"
    rec = json.loads(f.read_text())
    assert rec["chat"] == "C1" and rec["thread"] == "1.0" and isinstance(rec["pid"], int)
    assert rec["event"]["tool_input"] == {"file_path": "/r/a.py"}  # contents never copied
    assert "tool_response" not in rec["event"] and "hunter2" not in f.read_text()
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(f.parent.stat().st_mode & 0o777) == "0o700"


def test_hook_noop_outside_slack_runs_and_never_fails(tmp_path):
    for env in ({}, {"HERMES_SESSION_CHAT_ID": "C1"}, {"HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_PLATFORM": "telegram"}):
        assert run_hook(tmp_path, {"hook_event_name": "Stop"}, base=env).returncode == 0
    assert not (tmp_path / "ev").exists()
    bad = subprocess.run([sys.executable, str(HOOK)], input="not json", text=True, capture_output=True,
                         env={"PATH": os.environ["PATH"], "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_PLATFORM": "slack"})
    assert bad.returncode == 0


def test_hook_sanitises_chat_id_in_filename(tmp_path):
    run_hook(tmp_path, {"hook_event_name": "Stop"}, base={"HERMES_SESSION_CHAT_ID": "../../etc/x", "HERMES_SESSION_PLATFORM": "slack"})
    assert [p.name for p in (tmp_path / "ev").iterdir()] == [".._.._etc_x.jsonl"]


def test_installed_command_skips_python_without_chat_env(tmp_path):
    cmd = inst.hook_command(pathlib.Path("/nonexistent/claude_code_hook.py"))
    r = subprocess.run(["sh", "-c", cmd], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""  # exits before ever touching python or the path


# ── reader ──
class Rec:
    def __init__(self):
        self.items = []

    def on_claude_event(self, item):
        self.items.append(item)


def write(path, *items, partial=""):
    with open(path, "a") as f:
        for i in items:
            f.write(json.dumps(i) + "\n")
        f.write(partial)


def test_reader_incremental_partial_lines_and_old_events(tmp_path):
    rec, f = Rec(), tmp_path / "C1.jsonl"
    reader = cc.EventReader(rec, tmp_path, max_age=60)
    now = time.time()
    write(f, {"t": now - 3600, "chat": "C1", "event": {"n": "old"}}, {"t": now, "chat": "C1", "event": {"n": 1}},
          partial='{"t": 1, "chat": "C1", "ev')
    assert reader.poll() == 1 and [i["event"]["n"] for i in rec.items] == [1]  # old skipped, partial line waits
    with open(f, "a") as h:
        h.write('ent": {"n": 2}}\n')
    write(f, {"t": time.time(), "chat": "C1", "event": {"n": 3}})
    assert reader.poll() == 2 and rec.items[-1]["event"]["n"] == 3
    assert reader.poll() == 0  # nothing new


def test_reader_missing_dir_and_garbage_lines(tmp_path):
    assert cc.EventReader(Rec(), tmp_path / "nope").poll() == 0
    f = tmp_path / "C1.jsonl"
    f.write_text("garbage\n" + json.dumps({"t": time.time(), "chat": "C1", "event": {}}) + "\n")
    assert cc.EventReader(Rec(), tmp_path).poll() == 1


def test_reader_cleanup_removes_only_stale_files(tmp_path):
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    old.write_text("{}\n"); new.write_text("{}\n")
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    cc.EventReader(Rec(), tmp_path).cleanup()
    assert not old.exists() and new.exists()


# ── installer ──
def test_install_merges_keeps_other_hooks_backs_up_and_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]},
            "permissions": {"allow": ["Read"]}}
    p.write_text(json.dumps(mine))
    inst.run("install", p)
    data = json.loads(p.read_text())
    assert data["permissions"] == mine["permissions"]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == mine["hooks"]["PreToolUse"][0] and "claude_code_hook.py" in pre[1]["hooks"][0]["command"]
    assert set(data["hooks"]) >= set(inst.EVENTS)
    assert list(tmp_path.glob("settings.json.bak-hermes-slack-*"))
    assert inst.run("install", p) == "no changes needed"
    assert "installed for" in inst.run("status", p)
    inst.run("uninstall", p)
    assert json.loads(p.read_text()) == mine  # exactly what the user had
    assert inst.run("status", p) == "not installed"


def test_install_refuses_invalid_json_and_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    try:
        inst.run("install", p)
        raise AssertionError("should refuse")
    except SystemExit as e:
        assert "not valid JSON" in str(e)
    assert p.read_text() == "{not json"
    q = tmp_path / "new.json"
    out = inst.run("install", q, dry_run=True)
    assert "claude_code_hook.py" in out and not q.exists()


# ── card scoping ──
class M(sa.SlackSubagentMixin):
    def _is_known_code_channel(self, c):
        return False


def test_card_only_shows_runs_overlapping_its_lifetime(monkeypatch):
    t = sa.SubagentTracker()
    monkeypatch.setattr(sa, "TRACKER", t)
    base = time.time()
    t.on_claude_event({"t": base - 500, "chat": "C1", "event": {"hook_event_name": "UserPromptSubmit", "session_id": "old", "prompt": "yesterday"}})
    t.on_claude_event({"t": base - 400, "chat": "C1", "event": {"hook_event_name": "Stop", "session_id": "old"}})
    t.on_claude_event({"t": base, "chat": "C1", "event": {"hook_event_name": "UserPromptSubmit", "session_id": "new", "prompt": "today"}})
    st = types.SimpleNamespace(channel="C1", thread_ts="", log_cursor={}, cursor_next={}, closing=False, created_wall=base - 10)
    assert [r["title"] for r in M()._subagent_task_chunks(st)] == ["🤖 Claude Code: today"]
