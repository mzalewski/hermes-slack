import asyncio
import importlib.util
import json
import pathlib
import sys
import types

_dir = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_dir))
_spec = importlib.util.spec_from_file_location("slack_findings_view", _dir / "findings_view.py")
fv = importlib.util.module_from_spec(_spec)
sys.modules["slack_findings_view"] = fv
_spec.loader.exec_module(fv)

REPO = "Org/Repo"


def finding(n, pr=4067, status="open", **kw):
    base = {"id": f"{REPO}#{pr}-F{n}", "pr": f"{REPO}#{pr}", "status": status, "body": f"**Issue {n}**\nmore", "blocking": False,
            "path": "a/b.js", "line": n, "note": "why", "url": f"https://x/{n}", "pr_url": f"https://github.com/{REPO}/pull/{pr}"}
    base.update(kw)
    return base["id"], base


def table(*items):
    return dict(items)


KNOWN = {f"{REPO}#4067", f"{REPO}#4068"}


def test_extract_prs_trusts_full_refs_but_filters_bare_numbers():
    assert fv.extract_prs("look at Org/Repo#12 and https://github.com/Org/Repo/pull/13", KNOWN) == [f"{REPO}#13", f"{REPO}#12"]
    assert fv.extract_prs("review PR 4067 and #4068", KNOWN) == [f"{REPO}#4067", f"{REPO}#4068"]
    assert fv.extract_prs("PR #5000", KNOWN) == [f"{REPO}#5000"]  # explicit PR wording is trusted
    assert fv.extract_prs("issue #9999, colour #123456, ticket #77", KNOWN) == []  # bare numbers must be known PRs
    assert fv.extract_prs("", KNOWN) == [] and fv.extract_prs(None, set()) == []
    assert fv.extract_prs("PR 5", set()) == []  # nothing known, no repo to resolve against


def test_changed_prs_detects_add_edit_remove():
    a, b = table(finding(1), finding(2, pr=4068)), table(finding(1), finding(2, pr=4068))
    assert fv.changed_prs(fv.snapshot(a), fv.snapshot(b), b) == set()
    c = table(finding(1, status="rejected"), finding(2, pr=4068))
    assert fv.changed_prs(fv.snapshot(a), fv.snapshot(c), c) == {f"{REPO}#4067"}
    d = table(finding(1), finding(2, pr=4068), finding(3, pr=4070))
    assert fv.changed_prs(fv.snapshot(a), fv.snapshot(d), d) == {f"{REPO}#4070"}
    e = table(finding(1))  # removed finding: PR comes from the key
    assert fv.changed_prs(fv.snapshot(a), fv.snapshot(e), e) == {f"{REPO}#4068"}


def test_links_persist_merge_and_cap(tmp_path):
    p = tmp_path / "links.json"
    a, b = fv.ChannelLinks(p), fv.ChannelLinks(p)
    assert a.add("C1", ["x#1", "x#2"]) and not a.add("C1", ["x#2"])
    b.add("C2", ["x#3"])  # another profile's adapter writes its own channel; C1 must survive
    assert json.loads(p.read_text()) == {"C1": ["x#1", "x#2"], "C2": ["x#3"]}
    assert fv.ChannelLinks(p).channels_for({"x#3"}) == {"C2"}
    for i in range(30):
        a.add("C1", [f"x#{100 + i}"])
    assert len(a.for_channel("C1")) == fv._MAX_PRS_PER_CHANNEL and a.for_channel("C1")[-1] == "x#129"


def test_render_shows_actionable_counts_closed_and_skips_clean_prs():
    f = table(finding(1), finding(2, status="planned", blocking=True), finding(3, status="rejected"), finding(4, status="wont_fix"))
    info = {f"{REPO}#4067": {"title": "Redesign <menu> & more", "url": "https://github.com/x/pull/4067"}}
    blocks = fv.render_blocks([f"{REPO}#4067", f"{REPO}#9999"], f, info)  # 9999 has no findings: not shown
    text = json.dumps(blocks, ensure_ascii=False)
    assert "9999" not in text
    assert blocks[0]["text"]["text"].startswith("<https://github.com/x/pull/4067|*#4067 Redesign &lt;menu&gt; &amp; more*>")
    assert "1 open · 1 planned · 2 closed" in blocks[0]["text"]["text"]
    assert "Issue 3" not in text and "Issue 4" not in text  # closed findings are counted, not listed
    assert text.index("Issue 1") < text.index("Issue 2")  # open before planned
    assert "⛔" in text and "`a/b.js:1`" in text and "<https://x/1|view>" in text


def test_render_keeps_given_order_caps_findings_and_blocks():
    items = [finding(n) for n in range(1, 30)] + [finding(1, pr=4068)]
    blocks = fv.render_blocks([f"{REPO}#4067", f"{REPO}#4068"], table(*items), {})
    assert fv.render_blocks([f"{REPO}#4068", f"{REPO}#4067"], table(*items), {})[0]["text"]["text"].startswith("<https://github.com/Org/Repo/pull/4068|*#4068")
    assert "#4067" in blocks[0]["text"]["text"]  # in the order given
    assert any("more on #4067" in json.dumps(b) for b in blocks)
    assert len(blocks) <= fv._MAX_BLOCKS and blocks[-1]["type"] != "divider"
    assert fv.render_blocks([f"{REPO}#4067"], {}, {}) == []


# ── the adapter mixin: watch, attribute, publish ──
class Fake(fv.SlackFindingsMixin):
    def __init__(self, live=(), known=("C1", "C2")):
        self.calls, self._live, self._known = [], set(live), set(known)
        self._code_channels = {c: True for c in known}
        self.stacks = {}  # channel -> [(number, current)]
        self._init_findings_state()
        self._native_task_card_streams = {i: types.SimpleNamespace(channel=c, stopped=False) for i, c in enumerate(live)}
        client = types.SimpleNamespace()

        async def api_call(method, json=None):
            self.calls.append((method, json))
        client.api_call = api_call
        self._client = client

    async def _findings_stack(self, channel, force=False):
        keys = [f"{REPO}#{n}" for n in self.stacks.get(channel, [])]
        return keys, {k: {"title": "stack " + k, "url": "https://u"} for k in keys}

    def _is_known_code_channel(self, c):
        return c in self._known

    def _get_client(self, c):
        return self._client


def setup_files(monkeypatch, tmp_path, findings):
    f = tmp_path / "findings.json"
    f.write_text(json.dumps(findings))
    (tmp_path / "state.json").write_text(json.dumps({"prs": {f"{REPO}#4067": {"title": "T", "url": "https://u"}}}))
    monkeypatch.setenv("SLACK_FINDINGS_FILE", str(f))
    monkeypatch.setenv("SLACK_FINDINGS_LINKS_FILE", str(tmp_path / "links.json"))
    monkeypatch.setenv("SLACK_FINDINGS_VIEW", "true")
    monkeypatch.setattr(fv, "_MIN_PUBLISH_INTERVAL", 0.0)
    return f


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("SLACK_FINDINGS_VIEW", raising=False)
    assert not fv.findings_view_enabled()
    a = Fake()
    a._findings_online()
    assert a._findings_task is None and a._findings_links is None
    a._findings_note_text("C1", "PR 4067")  # no links object: must be a no-op


def test_mention_links_channel_and_publishes_view(monkeypatch, tmp_path):
    async def go():
        setup_files(monkeypatch, tmp_path, table(finding(1)))
        a = Fake()
        a._findings_online()
        a._findings_note_text("C1", "can you review PR #4067?")
        a._findings_note_text("C3", "PR #4067")  # not a Code channel: ignored
        await asyncio.sleep(0.05)
        a._findings_offline()
        assert [c[1]["channel_id"] for c in a.calls] == ["C1"]
        call = a.calls[0]
        assert call[0] == "agents.conversations.setView" and call[1]["view_key"] == "hermes-findings"
        assert "Issue 1" in json.dumps(call[1]["blocks"])
    asyncio.run(go())


def test_file_change_updates_linked_channels_only_and_skips_identical_views(monkeypatch, tmp_path):
    async def go():
        f = setup_files(monkeypatch, tmp_path, table(finding(1), finding(1, pr=4068)))
        a = Fake()
        a._findings_online()
        a._findings_links.add("C1", [f"{REPO}#4067"])
        a._findings_links.add("C2", [f"{REPO}#4068"])
        await a._findings_tick()
        assert a.calls == []  # nothing changed since start
        f.write_text(json.dumps(table(finding(1, status="planned", note="fixed locally"), finding(1, pr=4068))))
        await a._findings_tick()
        assert [c[1]["channel_id"] for c in a.calls] == ["C1"]  # only the PR that changed
        assert "fixed locally" in json.dumps(a.calls[0][1]["blocks"])
        n = len(a.calls)
        f.write_text(json.dumps(table(finding(1, status="planned", note="fixed locally"), finding(1, pr=4068))) + " ")
        await a._findings_tick()
        assert len(a.calls) == n  # touched file, same content: no update
        a._findings_offline()
    asyncio.run(go())


def test_channel_follows_its_branch_stack(monkeypatch, tmp_path):
    async def go():
        f = setup_files(monkeypatch, tmp_path, table(finding(1, pr=4068), finding(1, pr=4070)))
        a = Fake()
        a.stacks["C1"] = [4067, 4068]  # C1's branch is in a stack that includes 4068; nobody mentioned it
        a._findings_online()
        f.write_text(json.dumps(table(finding(1, pr=4068, note="stack finding"), finding(1, pr=4070))))
        await a._findings_tick()
        assert [c[1]["channel_id"] for c in a.calls] == ["C1"]
        assert "stack finding" in json.dumps(a.calls[0][1]["blocks"])
        assert "4070" not in json.dumps(a.calls[0][1]["blocks"])  # a PR outside the stack stays out
        a._findings_offline()
    asyncio.run(go())


def test_branch_switch_republishes_without_a_file_change(monkeypatch, tmp_path):
    async def go():
        setup_files(monkeypatch, tmp_path, table(finding(1, pr=4068), finding(2, pr=4070)))
        a = Fake()
        a.stacks["C1"] = [4068]
        a._findings_online()
        await a._findings_refresh_stacks()
        assert len(a.calls) == 1 and "Issue 1" in json.dumps(a.calls[0][1]["blocks"])
        a.stacks["C1"] = [4070]  # the channel's branch changed
        await a._findings_refresh_stacks()
        assert len(a.calls) == 2 and "Issue 2" in json.dumps(a.calls[1][1]["blocks"])
        await a._findings_refresh_stacks()
        assert len(a.calls) == 2  # unchanged
        a._findings_offline()
    asyncio.run(go())


def test_change_during_a_live_run_is_attributed_to_that_channel(monkeypatch, tmp_path):
    async def go():
        f = setup_files(monkeypatch, tmp_path, table(finding(1)))
        a = Fake(live=["C2"])
        a._findings_online()
        f.write_text(json.dumps(table(finding(1), finding(2))))
        await a._findings_tick()
        assert a._findings_links.for_channel("C2") == [f"{REPO}#4067"]
        assert [c[1]["channel_id"] for c in a.calls] == ["C2"]
        a._findings_offline()
    asyncio.run(go())


def test_unreadable_file_and_failed_publish_do_not_break_the_watcher(monkeypatch, tmp_path):
    async def go():
        f = setup_files(monkeypatch, tmp_path, table(finding(1)))
        a = Fake()
        a._findings_online()
        a._findings_links.add("C1", [f"{REPO}#4067"])
        f.write_text("{not json")
        await a._findings_tick()  # parses as empty: every finding looks removed, nothing to render
        f.write_text(json.dumps(table(finding(1, status="planned"))))

        async def boom(method, json=None):
            raise RuntimeError("slack down")
        a._client.api_call = boom
        await a._findings_tick()  # swallowed, and the digest is cleared so a later change retries
        assert "C1" not in a._findings_digest
        a._findings_offline()
    asyncio.run(go())


# ── /findings ──
def test_parse_command_args():
    known = {f"{REPO}#4067"}
    assert fv.parse_command_args("", known) == {"mode": "stack", "closed": False}
    assert fv.parse_command_args(" ALL ", known) == {"mode": "stack", "closed": True}
    assert fv.parse_command_args("4067", known) == {"mode": "pr", "pr": f"{REPO}#4067", "closed": False}
    assert fv.parse_command_args("#4067 all", known) == {"mode": "pr", "pr": f"{REPO}#4067", "closed": True}
    assert fv.parse_command_args("Other/Repo#5", known)["pr"] == "Other/Repo#5"
    assert fv.parse_command_args("banana", known)["mode"] == "error"


def test_summary_line_and_closed_rendering():
    f = table(finding(1), finding(2, status="planned"), finding(3, status="rejected", body="Rejected one"))
    assert fv.summary_line([f"{REPO}#4067", f"{REPO}#4068"], f) == "2 PRs · 1 with findings · 1 open · 1 planned · 1 closed"
    plain = json.dumps(fv.render_blocks([f"{REPO}#4067"], f, {}), ensure_ascii=False)
    full = json.dumps(fv.render_blocks([f"{REPO}#4067"], f, {}, include_closed=True), ensure_ascii=False)
    assert "Rejected one" not in plain and "Rejected one" in full and "⚪" in full


def test_findings_command_reply_modes(monkeypatch, tmp_path):
    async def go():
        setup_files(monkeypatch, tmp_path, table(finding(1), finding(2, pr=4068, status="rejected", body="Old"), finding(3, pr=4070)))
        a = Fake()
        a.stacks["C1"] = [4067, 4068]
        r = await a.findings_command_reply("C1", "")
        assert r["text"].startswith("2 PRs · 2 with findings") and "4070" not in json.dumps(r["blocks"])
        assert r["blocks"][0]["type"] == "context"
        assert "Old" not in json.dumps(r["blocks"])
        assert "Old" in json.dumps((await a.findings_command_reply("C1", "all"))["blocks"])
        assert "Issue 3" in json.dumps((await a.findings_command_reply("C1", "#4070"))["blocks"])  # any PR, in or out of the stack
        assert "No findings for #4071" in (await a.findings_command_reply("C1", "PR 4071"))["text"]
        assert "No PR stack" in (await a.findings_command_reply("C2", ""))["text"]
        assert "Don't know" in (await a.findings_command_reply("C1", "nonsense"))["text"]
    asyncio.run(go())


def test_command_registered_once_per_channel_and_flag_off(monkeypatch):
    async def go():
        a = Fake()
        await a._register_channel_commands("C1")
        await a._register_channel_commands("C1")
        assert [c[0] for c in a.calls] == ["agents.conversations.setCommands"]
        assert a.calls[0][1] == {"channel_id": "C1", "commands": [fv.COMMAND]}
        monkeypatch.setenv("SLACK_FINDINGS_COMMAND", "false")
        await a._register_channel_commands("C2")
        assert len(a.calls) == 1
        a2 = Fake()

        async def boom(method, json=None):
            raise RuntimeError("no_actions_url")
        a2._client.api_call = boom
        monkeypatch.delenv("SLACK_FINDINGS_COMMAND")
        await a2._register_channel_commands("C1")  # logged, never raised
    asyncio.run(go())
