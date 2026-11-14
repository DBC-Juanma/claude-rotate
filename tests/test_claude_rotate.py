import datetime as dt
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import claude_rotate as cr

MADRID = ZoneInfo("Europe/Madrid")
NOW = dt.datetime(2026, 10, 22, 10, 0, tzinfo=MADRID).timestamp()
LIMIT_TEXT = "You've hit your limit · resets 9pm (Europe/Madrid)"
RESET = dt.datetime(2026, 10, 22, 21, 0, tzinfo=MADRID).timestamp()
AUTH_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"
OK_RESULT = json.dumps({"type": "result", "is_error": False, "result": "ok"})


def oauth(tag: str) -> dict:
    return {"accessToken": f"at-{tag}", "refreshToken": f"rt-{tag}", "expiresAt": 1800000000000, "subscriptionType": "max"}


def account(email: str) -> dict:
    return {"emailAddress": email, "accountUuid": f"uuid-{email}"}


def make_cd(base: Path, name: str, email: str, tokens: dict) -> str:
    cd = base / name
    cd.mkdir()
    (cd / ".credentials.json").write_text(json.dumps({"claudeAiOauth": tokens, "mcpOAuth": {"keep": 1}}))
    (cd / ".claude.json").write_text(json.dumps({"oauthAccount": account(email), "projects": {"keep": 2}}))
    return str(cd)


def error_line(ts: float, uuid: str, text: str, **fields) -> str:
    return json.dumps({
        "type": "assistant", "isApiErrorMessage": True, "uuid": uuid,
        "timestamp": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}, **fields,
    })


def limit_line(ts: float, uuid: str = "u-limit", text: str = LIMIT_TEXT, resets: float = RESET) -> str:
    return error_line(ts, uuid, text, error="rate_limit", apiError="usage_limit_reached", apiErrorStatus=429,
                      quotaLimits={"status": "rejected", "resetsAt": int(resets), "rateLimitType": "five_hour"})


def auth_line(ts: float, uuid: str = "u-auth") -> str:
    return error_line(ts, uuid, AUTH_TEXT, error="authentication_failed")


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class World:
    """A vault root plus recorded side effects, all under tmp_path."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.clock = Clock(NOW)
        self.sent = []
        self.procs = []
        self.owners = {}
        self.claude_runs = []
        self.claude_reply = (0, OK_RESULT)
        self.claude_writes = None
        self.claude_error = None
        self.screen = "● done\n───\n❯ \n───\n  status line\n"
        self.box = {}
        self.enter_dropped = 0
        self.ctx = cr.Ctx(root=tmp_path / "root", now=self.clock, sleep=lambda s: None,
                          list_procs=lambda: self.procs, tmux_send=self._send, tmux_capture=self._capture,
                          out=io.StringIO(),
                          run_claude=self._run_claude, token_email=lambda at: self.owners.get(at))

    def _send(self, proc, keys, literal=False):
        """Record the keys and play them on the pane's input box: text goes in, Enter submits it (unless an Enter is
        set to be dropped, as a session slow to draw the text does), Ctrl+U empties it."""
        self.sent.append((proc.pane, tuple(keys), literal))
        if literal:
            self.box[proc.pane] = self.box.get(proc.pane, "") + "".join(keys)
        elif keys == ["Enter"] and self.enter_dropped:
            self.enter_dropped -= 1
        elif keys == ["Enter"] or "C-u" in keys:
            self.box[proc.pane] = ""

    def _capture(self, proc):
        """self.screen with the pane's input box (its last ❯ line) holding what was typed and not yet sent, which
        replaces a dim suggestion there."""
        typed = self.box.get(proc.pane, "")
        lines = self.screen.split("\n")
        boxes = [i for i, ln in enumerate(lines) if ln.lstrip().startswith(("❯", "\x1b[39m❯"))]
        if typed and boxes:
            lines[boxes[-1]] = f"❯ {typed}"
        return "\n".join(lines)

    def _run_claude(self, config_dir):
        self.claude_runs.append(json.loads((Path(config_dir) / ".claude.json").read_text())["oauthAccount"]["emailAddress"])
        if self.claude_writes:
            (Path(config_dir) / ".credentials.json").write_text(json.dumps({"claudeAiOauth": self.claude_writes}))
        if self.claude_error:
            transcript = Path(config_dir) / "projects" / "-renew" / "s.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(self.claude_error + "\n")
        return self.claude_reply

    def creds(self, cd: str) -> dict:
        return json.loads((Path(cd) / ".credentials.json").read_text())

    def slot_tokens(self, email: str) -> dict:
        return cr.load_slot(self.ctx, email)["claudeAiOauth"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


@pytest.fixture
def two(w):
    """Accounts a@x and b@x enrolled from their own dirs; cd_a is the watched dir, logged in as a@x."""
    cd_a = make_cd(w.tmp, "a", "a@x", oauth("a1"))
    cd_b = make_cd(w.tmp, "b", "b@x", oauth("b1"))
    w.owners.update({"at-a1": "a@x", "at-b1": "b@x"})
    cr.enroll(w.ctx, cd_a)
    cr.enroll(w.ctx, cd_b)
    cfg = cr.load_config(w.ctx)
    cfg["config_dirs"] = [cd_a]
    cr.save_config(w.ctx, cfg)
    return cd_a


def test_entry_error_reads_a_limit_and_its_reset_from_the_structured_fields():
    assert cr.entry_error(limit_line(NOW, uuid="u7")) == cr.Hit("limit", LIMIT_TEXT, NOW, "u7", RESET)


def test_entry_error_does_not_depend_on_the_wording():
    text = "Has alcanzado tu límite de sesión · se restablece a las 21:00"
    assert cr.entry_error(limit_line(NOW, uuid="u8", text=text)) == cr.Hit("limit", text, NOW, "u8", RESET)


def test_entry_error_ignores_a_short_lived_rate_limit():
    line = error_line(NOW, "u9", "API Error: Request rejected (429) · This request would exceed your account's rate limit.",
                      error="rate_limit", apiErrorStatus=429)
    assert cr.entry_error(line) is None


def test_entry_error_reads_an_expired_login():
    assert cr.entry_error(auth_line(NOW, uuid="u10")) == cr.Hit("auth", AUTH_TEXT, NOW, "u10")


def test_entry_error_ignores_model_text_quoting_the_limit():
    e = json.loads(limit_line(NOW))
    e["message"]["model"] = "claude-opus-5-5"
    assert cr.entry_error(json.dumps(e)) is None


def test_enroll_adds_vault_slot_and_order(w, two):
    assert cr.load_config(w.ctx)["order"] == ["a@x", "b@x"]
    assert w.slot_tokens("b@x") == oauth("b1")
    assert cr.load_slot(w.ctx, "a@x")["oauthAccount"] == account("a@x")


def test_login_enrolls_account_from_throwaway_dir_and_removes_it(w, two):
    seen = []

    def fake_login(cd, email):
        seen.append((cd, email))
        Path(cd, ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth("c1")}))
        Path(cd, ".claude.json").write_text(json.dumps({"oauthAccount": account("c@x")}))

    w.ctx.claude_login = fake_login
    w.owners["at-c1"] = "c@x"
    assert cr.login(w.ctx, "c@x") == "c@x"
    assert seen[0][1] == "c@x"
    assert not Path(seen[0][0]).exists()
    assert w.slot_tokens("c@x") == oauth("c1")
    assert cr.load_config(w.ctx)["order"] == ["a@x", "b@x", "c@x"]


def test_login_aborted_enrolls_nothing(w, two):
    w.ctx.claude_login = lambda cd, email: None
    with pytest.raises(SystemExit):
        cr.login(w.ctx)
    assert cr.load_config(w.ctx)["order"] == ["a@x", "b@x"]


def test_enroll_refuses_token_of_another_account(w, two):
    cd = make_cd(w.tmp, "c", "c@x", oauth("a9"))
    w.owners["at-a9"] = "a@x"
    with pytest.raises(SystemExit):
        cr.enroll(w.ctx, cd)
    assert cr.load_slot(w.ctx, "c@x") is None
    assert cr.load_config(w.ctx)["order"] == ["a@x", "b@x"]


def test_enroll_refuses_unresolved_owner(w, two):
    cd = make_cd(w.tmp, "c", "c@x", oauth("c1"))
    with pytest.raises(SystemExit):
        cr.enroll(w.ctx, cd)
    assert cr.load_slot(w.ctx, "c@x") is None


def test_switch_saves_outgoing_tokens_and_keeps_other_keys(w, two):
    cr.write_live_oauth(two, oauth("a2"))
    cr.switch(w.ctx, two, "b@x")
    assert w.creds(two) == {"claudeAiOauth": oauth("b1"), "mcpOAuth": {"keep": 1}}
    assert json.loads((Path(two) / ".claude.json").read_text()) == {"oauthAccount": account("b@x"), "projects": {"keep": 2}}
    assert w.slot_tokens("a@x") == oauth("a2")
    assert cr.load_state(w.ctx)["guards"][two] == {"target": "b@x", "prev": "a@x", "until": NOW + cr.GUARD_SECONDS}


def test_switch_refuses_dead_account(w, two):
    cr._mark_dead(w.ctx, "b@x", "revoked")
    with pytest.raises(SystemExit, match="b@x is dead"):
        cr.switch(w.ctx, two, "b@x")
    assert w.creds(two)["claudeAiOauth"] == oauth("a1")


def test_next_available_skips_limited_and_dead(w, two):
    make_cd(w.tmp, "c", "c@x", oauth("c1"))
    w.owners["at-c1"] = "c@x"
    cr.enroll(w.ctx, str(w.tmp / "c"))
    st = cr.load_state(w.ctx)
    st["limited"]["b@x"] = NOW + 3600
    cr.save_state(w.ctx, st)
    assert cr.next_available(w.ctx, "a@x") == "c@x"
    cr._mark_dead(w.ctx, "c@x", "revoked")
    assert cr.next_available(w.ctx, "a@x") is None
    w.clock.t = NOW + 3601
    assert cr.next_available(w.ctx, "a@x") == "b@x"


def test_reconcile_stores_own_refresh_in_vault(w, two):
    cr.write_live_oauth(two, oauth("a2"))
    w.owners["at-a2"] = "a@x"
    cr.Rotator(w.ctx).reconcile(two)
    assert w.slot_tokens("a@x") == oauth("a2")
    assert w.creds(two)["claudeAiOauth"] == oauth("a2")


def test_reconcile_routes_stray_token_to_its_owner_and_restores_live(w, two):
    cr.write_live_oauth(two, oauth("b2"))
    w.owners["at-b2"] = "b@x"
    cr.Rotator(w.ctx).reconcile(two)
    assert w.slot_tokens("b@x") == oauth("b2")
    assert w.slot_tokens("a@x") == oauth("a1")
    assert w.creds(two)["claudeAiOauth"] == oauth("a1")


def test_reconcile_writes_nothing_when_owner_unknown(w, two):
    cr.write_live_oauth(two, oauth("zz"))
    cr.Rotator(w.ctx).reconcile(two)
    assert w.slot_tokens("a@x") == oauth("a1")
    assert w.slot_tokens("b@x") == oauth("b1")
    assert w.creds(two)["claudeAiOauth"] == oauth("zz")
    assert "owner_unresolved" in w.ctx.out.getvalue()


def test_renew_stores_rotated_tokens(w, two):
    w.claude_writes = oauth("b2")
    assert cr.renew(w.ctx, "b@x") == "ok"
    assert w.claude_runs == ["b@x"]
    assert w.slot_tokens("b@x") == oauth("b2")
    assert cr.load_state(w.ctx)["renewed"]["b@x"] == NOW


def test_renew_marks_dead_on_expired_login(w, two):
    w.claude_reply = (1, json.dumps({"type": "result", "is_error": True, "result": AUTH_TEXT}))
    w.claude_error = auth_line(NOW)
    assert cr.renew(w.ctx, "b@x") == "dead"
    dead = cr.load_state(w.ctx)["dead"]["b@x"]
    assert dead["reason"] == AUTH_TEXT
    assert w.slot_tokens("b@x") == oauth("b1")



def test_renew_failing_for_any_other_reason_is_an_error_not_a_dead_login(w, two):
    w.claude_reply = (1, json.dumps({"type": "result", "is_error": True, "result": "API Error: 529 overloaded"}))
    assert cr.renew(w.ctx, "b@x") == "error"
    assert "b@x" not in cr.load_state(w.ctx)["dead"]

def test_renew_due_skips_live_and_recently_renewed(w, two):
    assert cr.renew_due(w.ctx) == {"b@x": "ok"}
    w.clock.t = NOW + 60
    assert cr.renew_due(w.ctx) == {}
    w.clock.t = NOW + cr.RENEW_EVERY
    assert cr.renew_due(w.ctx) == {"b@x": "ok"}
    assert w.claude_runs == ["b@x", "b@x"]


def test_limit_in_transcript_rotates_and_wakes_the_session(w, two):
    transcript = Path(two) / "projects" / "-repo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    (Path(two) / "sessions").mkdir()
    (Path(two) / "sessions" / "4242.json").write_text(json.dumps({"sessionId": "s1"}))
    w.procs = [cr.Proc(4242, two, "%1", None)]
    rot = cr.Rotator(w.ctx)
    rot.tick()
    assert cr.current_email(two) == "a@x"

    with transcript.open("a") as fh:
        fh.write(limit_line(NOW + 100) + "\n")
    w.clock.t = NOW + 120
    rot.tick()

    assert cr.current_email(two) == "b@x"
    assert w.creds(two)["claudeAiOauth"] == oauth("b1")
    assert cr.load_state(w.ctx)["limited"]["a@x"] == dt.datetime(2026, 10, 22, 21, 0, tzinfo=MADRID).timestamp()
    assert w.sent == [("%1", (cr.COMPACT_TEXT,), True), ("%1", ("Enter",), False)]


def test_a_session_stopped_by_the_limit_reconnects_by_name_and_is_woken_on_its_new_bridge(w, two):
    (Path(two) / "settings.json").write_text(json.dumps({"remoteControlAtStartup": True}))
    transcript = Path(two) / "projects" / "-repo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(limit_line(NOW - 100) + "\n")
    (Path(two) / "sessions").mkdir()
    (Path(two) / "sessions" / "4242.json").write_text(json.dumps(
        {"sessionId": "s1", "kind": "interactive", "status": "idle", "bridgeSessionId": "session_old", "name": "yed18-s",
         "nameSource": "user"}))
    w.procs = [cr.Proc(4242, two, "%1", None)]
    rot = cr.Rotator(w.ctx)
    rot.rotate(two)
    assert w.sent == []
    name_session(two, bridgeSessionId=None)
    assert rot.wake(two) == []
    assert rot.reconnect(two) == []
    w.clock.t += cr.RC_GRACE
    assert rot.reconnect(two) == ["%1"]
    assert w.sent == [("%1", ("/remote-control yed18-s",), True), ("%1", ("Enter",), False)]
    w.sent.clear()
    name_session(two, bridgeSessionId="session_new")
    assert rot.wake(two) == ["%1"]
    assert w.sent == [("%1", (cr.COMPACT_TEXT,), True), ("%1", ("Enter",), False)]
    w.sent.clear()
    assert rot.compact(two) == []

def test_limit_from_before_the_switch_is_ignored(w, two):
    transcript = Path(two) / "projects" / "-repo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    rot = cr.Rotator(w.ctx)
    rot.tick()
    cr.switch(w.ctx, two, "b@x")
    with transcript.open("a") as fh:
        fh.write(limit_line(NOW + 10) + "\n")
    w.clock.t = NOW + 20
    rot.tick()
    assert cr.current_email(two) == "b@x"
    assert "b@x" not in cr.load_state(w.ctx)["limited"]
    assert "limit_inflight_ignored" in w.ctx.out.getvalue()


RC_FREE = [("%1", (cr.RC_TEXT,), True), ("%1", ("Enter",), False)]
RC_BUSY = [("%1", (cr.RC_TEXT,), True), ("%1", ("Enter",), False)]


def rc_session(w, cd: str, status: str) -> None:
    (Path(cd) / "settings.json").write_text(json.dumps({"remoteControlAtStartup": True}))
    (Path(cd) / "sessions").mkdir()
    (Path(cd) / "sessions" / "4242.json").write_text(json.dumps(
        {"sessionId": "s1", "kind": "interactive", "status": status, "bridgeSessionId": None}))
    w.procs = [cr.Proc(4242, cd, "%1", None)]


def reconnect_after_grace(w, cd: str, rot=None) -> list:
    """Run reconnect on a session first seen without a bridge RC_GRACE seconds ago; returns the panes typed into."""
    rot = rot or cr.Rotator(w.ctx)
    assert rot.reconnect(cd) == []
    w.clock.t += cr.RC_GRACE
    return rot.reconnect(cd)


@pytest.mark.parametrize("status,keys", [("idle", RC_FREE), ("shell", RC_FREE), ("busy", RC_BUSY), ("waiting", [])])
def test_reconnect_runs_remote_control_in_every_session_without_bridge(w, two, status, keys):
    rc_session(w, two, status)
    assert reconnect_after_grace(w, two) == (["%1"] if keys else [])
    assert w.sent == keys



def name_session(cd: str, **fields) -> None:
    info = Path(cd) / "sessions" / "4242.json"
    info.write_text(json.dumps({**json.loads(info.read_text()), **fields}))


def write_transcript(cd: str, *entries: dict) -> None:
    transcript = Path(cd) / "projects" / "-repo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_reconnect_names_the_remote_session_after_the_name_its_user_gave_it(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="docs-research", nameSource="user")
    write_transcript(two, {"type": "ai-title", "aiTitle": "Docs of the research", "sessionId": "s1"})
    assert reconnect_after_grace(w, two) == ["%1"]
    assert w.sent == [("%1", ("/remote-control docs-research",), True), RC_FREE[1]]


def test_reconnect_uses_the_generated_title_not_the_name_derived_from_the_directory(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="calm-your-socks-16", nameSource="derived")
    write_transcript(two, {"type": "ai-title", "aiTitle": "Propuesta calma your sock", "sessionId": "s1"},
                     {"type": "user", "message": {"content": "hi"}},
                     {"type": "ai-title", "aiTitle": "Técnica flip en el QG", "sessionId": "s1"})
    assert reconnect_after_grace(w, two) == ["%1"]
    assert w.sent == [("%1", ("/remote-control Técnica flip en el QG",), True), RC_FREE[1]]


def test_reconnect_without_a_title_leaves_the_remote_session_unnamed(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="vokoban-de", nameSource="derived")
    assert reconnect_after_grace(w, two) == ["%1"]
    assert w.sent == RC_FREE


COMPACT = [("%1", (cr.COMPACT_TEXT,), True), ("%1", ("Enter",), False)]
CONTINUE = [("%1", (cr.CONTINUE_TEXT,), True), ("%1", ("Enter",), False)]
LONG_TEXT = ("Account rotated (claude-rotate) and Remote Control restarted without its history. Write a short status summary "
             "for whoever follows this session remotely: what you were doing, what is done, what is pending and the next step.")
TYPED = [("%1", (LONG_TEXT,), True), ("%1", ("Enter",), False)]


def rotated_session(w, cd: str, status: str, bridge, idle_for: float = 60) -> cr.Rotator:
    """A session of cd with the given status, last changed idle_for seconds ago, a Rotator that just switched cd to b@x
    while the session was on bridge session_old, and the session now on bridge."""
    rc_session(w, cd, status)
    name_session(cd, bridgeSessionId="session_old", statusUpdatedAt=int((NOW - idle_for) * 1000))
    write_transcript(cd, {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}})
    rot = cr.Rotator(w.ctx)
    rot.rotate(cd)
    assert cr.current_email(cd) == "b@x"
    assert w.sent == []
    name_session(cd, bridgeSessionId=bridge)
    return rot


def test_compact_is_not_sent_on_the_bridge_that_dies_with_the_switch(w, two):
    rot = rotated_session(w, two, "idle", "session_old")
    assert rot.compact(two) == []
    assert w.sent == []


def test_after_a_rotation_a_recently_used_session_is_compacted_once(w, two):
    rot = rotated_session(w, two, "idle", "session_new", idle_for=cr.ACTIVE_WINDOW - 1)
    assert rot.compact(two) == ["%1"]
    assert w.sent == COMPACT
    w.sent.clear()
    assert rot.compact(two) == []
    assert w.sent == []


def test_a_session_idle_longer_than_the_active_window_is_not_compacted(w, two):
    rot = rotated_session(w, two, "idle", "session_new", idle_for=cr.ACTIVE_WINDOW + 1)
    assert rot.compact(two) == []
    assert w.sent == []


def test_a_session_busy_at_the_switch_is_compacted_however_old_its_status(w, two):
    rot = rotated_session(w, two, "busy", "session_new", idle_for=10 * cr.ACTIVE_WINDOW)
    assert rot.compact(two) == []
    assert w.sent == []
    name_session(two, status="idle")
    assert rot.compact(two) == ["%1"]
    assert w.sent == COMPACT


def test_compact_waits_for_the_new_remote_control_bridge(w, two):
    rot = rotated_session(w, two, "idle", None)
    assert rot.compact(two) == []
    name_session(two, bridgeSessionId="session_new")
    assert rot.compact(two) == ["%1"]
    assert w.sent == COMPACT


def test_compact_waits_for_a_draft_in_the_input_box(w, two):
    rot = rotated_session(w, two, "idle", "session_new")
    w.screen = "● done\n───\n❯ lanza el benchmark ya\n───\n"
    assert rot.compact(two) == []
    w.screen = "● done\n───\n❯ \n───\n"
    assert rot.compact(two) == ["%1"]


def limit_stopped_session(w, cd: str) -> cr.Rotator:
    """A session of cd stopped by the limit, rotated, on its new bridge and woken: /compact typed into it."""
    rc_session(w, cd, "idle")
    name_session(cd, bridgeSessionId="session_old", statusUpdatedAt=int((NOW - 100) * 1000))
    write_transcript(cd, json.loads(limit_line(NOW - 100)))
    rot = cr.Rotator(w.ctx)
    rot.rotate(cd)
    assert w.sent == []
    name_session(cd, bridgeSessionId="session_new")
    assert rot.wake(cd) == ["%1"]
    assert w.sent == COMPACT
    w.sent.clear()
    return rot


def append_transcript(cd: str, *entries: dict) -> None:
    with (Path(cd) / "projects" / "-repo" / "s1.jsonl").open("a") as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in entries))


def test_a_session_stopped_by_the_limit_is_compacted_then_told_to_continue(w, two):
    rot = limit_stopped_session(w, two)
    assert rot.compact(two) == []
    assert rot.resume(two) == []
    append_transcript(two, {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
                      {"type": "user", "isCompactSummary": True, "message": {"content": "This session is being continued"}})
    name_session(two, status="busy")
    assert rot.resume(two) == []
    name_session(two, status="idle")
    assert rot.resume(two) == ["%1"]
    assert w.sent == CONTINUE
    w.sent.clear()
    assert rot.resume(two) == []
    assert w.sent == []


def test_a_compaction_that_never_shows_still_lets_the_session_continue(w, two):
    rot = limit_stopped_session(w, two)
    w.clock.t = NOW + cr.COMPACT_WAIT - 1
    assert rot.resume(two) == []
    w.clock.t = NOW + cr.COMPACT_WAIT
    assert rot.resume(two) == ["%1"]
    assert w.sent == CONTINUE
    assert "compact_missing" in w.ctx.out.getvalue()


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_reconnect_leaves_a_draft_in_the_input_box_alone(w, two, status):
    rc_session(w, two, status)
    w.screen = "● done\n───\n❯ lanza el benchmark ya\n───\n  status line\n"
    assert cr.Rotator(w.ctx).reconnect(two) == []
    assert w.sent == []


def test_reconnect_types_over_a_dim_suggestion(w, two):
    rc_session(w, two, "idle")
    w.screen = "───\n\x1b[39m❯\xa0\x1b[2mlanza el benchmark ya\x1b[0m\n───\n"
    assert reconnect_after_grace(w, two) == ["%1"]
    assert w.sent == RC_FREE


RC_DIALOG_SCREEN = ("● done\n───\n   Remote Control\n   This session is available in the Claude mobile app and claude.ai/code.\n"
                    "     Disconnect this session\n     Show QR code   Scan with your phone to open this session\n"
                    "   ❯ Continue\n   Enter to select · Esc to continue\n")


def test_a_remote_control_dialog_left_open_is_closed_on_the_next_tick(w, two):
    rc_session(w, two, "idle")
    w.screen = RC_DIALOG_SCREEN
    rot = cr.Rotator(w.ctx)
    assert rot.dismiss_rc_dialogs(two) == []
    assert w.sent == []
    assert rot.dismiss_rc_dialogs(two) == ["%1"]
    assert w.sent == [("%1", ("Escape",), False)]


def test_a_dialog_closed_between_ticks_is_not_escaped(w, two):
    rc_session(w, two, "idle")
    w.screen = RC_DIALOG_SCREEN
    rot = cr.Rotator(w.ctx)
    rot.dismiss_rc_dialogs(two)
    w.screen = "● done\n───\n❯ \n───\n  status line\n"
    assert rot.dismiss_rc_dialogs(two) == []
    w.screen = RC_DIALOG_SCREEN
    assert rot.dismiss_rc_dialogs(two) == []
    assert w.sent == []


def test_reconnect_skips_an_open_dialog(w, two):
    rc_session(w, two, "idle")
    w.screen = "❯ earlier prompt\n   Remote Control\n   ❯ Continue\n   Enter to select · Esc to continue\n"
    assert cr.Rotator(w.ctx).reconnect(two) == []


def test_typing_sends_no_escape_and_waits_for_the_text_before_enter(w, two):
    rc_session(w, two, "idle")
    events = []
    w.ctx.sleep = lambda secs: events.append(("sleep", secs))
    send = w.ctx.tmux_send
    w.ctx.tmux_send = lambda proc, keys, literal=False: (events.append(tuple(keys)), send(proc, keys, literal))
    assert cr.Rotator(w.ctx).type_line(w.procs[0], "Account rotated")
    assert events == [("Account rotated",), ("sleep", cr.POLL), ("Enter",), ("sleep", cr.POLL)]


def test_an_enter_the_session_drops_is_pressed_again(w, two):
    rc_session(w, two, "idle")
    w.enter_dropped = 1
    assert cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == TYPED + [("%1", ("Enter",), False)]
    assert w.box["%1"] == ""


def test_text_slow_to_show_is_waited_for_before_enter(w, two):
    rc_session(w, two, "idle")
    empty = "● done\n───\n❯ \n───\n"
    screens = iter([empty, empty, STUCK_SUMMARY_SCREEN.replace("ccount", "Account"), empty])
    w.ctx.tmux_capture = lambda p: next(screens)
    assert cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == TYPED


RC_NOT_CONNECTED_SCREEN = ("● done\n▔▔▔\n   Remote Control\n   × Remote Control isn't connected.\n"
                           "   signed-in claude.ai account or organization changed on this machine — run /remote-control\n"
                           "   To try again, choose Continue, wait about 10 seconds for Remote Control to switch off, then run /remote-control.\n"
                           "     Disconnect this session   Can archive it now and stop a later --resume reconnecting it\n"
                           "   ❯ Continue\n   Enter to select · Esc to continue\n")


def test_the_remote_control_not_connected_dialog_is_closed_on_the_next_tick(w, two):
    rc_session(w, two, "idle")
    w.screen = RC_NOT_CONNECTED_SCREEN
    rot = cr.Rotator(w.ctx)
    assert rot.reconnect(two) == []
    assert rot.dismiss_rc_dialogs(two) == []
    assert rot.dismiss_rc_dialogs(two) == ["%1"]
    assert w.sent == [("%1", ("Escape",), False)]


def test_reconnect_waits_for_the_old_bridge_to_switch_off_after_a_switch(w, two):
    rc_session(w, two, "idle")
    rot = cr.Rotator(w.ctx)
    w.clock.t = NOW - cr.RC_GRACE
    assert rot.reconnect(two) == []
    w.clock.t = NOW
    cr.switch(w.ctx, two, "b@x")
    w.clock.t = NOW + cr.RC_SETTLE - 1
    assert rot.reconnect(two) == []
    assert w.sent == []
    w.clock.t = NOW + cr.RC_SETTLE
    assert rot.reconnect(two) == ["%1"]
    assert w.sent == RC_FREE


STUCK_SUMMARY_SCREEN = ("● done\n───\n❯ ccount rotated (claude-rotate) and Remote Control restarted without its history. Write\n"
                        "  a short status summary for whoever follows this session remotely\n───\n  status line\n")


def test_a_line_left_in_the_input_box_gets_a_second_enter(w, two):
    rc_session(w, two, "idle")
    screens = iter([STUCK_SUMMARY_SCREEN] * (1 + cr.TYPE_POLLS) + ["● done\n───\n❯ \n───\n"])
    w.ctx.tmux_capture = lambda p: next(screens)
    assert cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == TYPED + [("%1", ("Enter",), False)]


def test_a_line_that_never_submits_is_cleared_not_left_as_a_draft(w, two):
    rc_session(w, two, "idle")
    w.screen = STUCK_SUMMARY_SCREEN
    assert not cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == TYPED + [("%1", ("Enter",), False)] * (cr.ENTER_TRIES - 1) + [("%1", tuple(cr.CLEAR_KEYS), False)]
    assert "type_failed" in w.ctx.out.getvalue()


def test_a_submitted_line_echoed_above_the_input_box_does_not_count_as_stuck():
    screen = "❯ Account rotated (claude-rotate) and Remote Control\n● Session status\n───\n❯ \n───\n"
    assert not cr.input_holds(screen, LONG_TEXT)


def test_a_title_cannot_type_extra_keys_into_the_session(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="x-1", nameSource="derived")
    write_transcript(two, {"type": "ai-title", "aiTitle": "Plan\n/exit\r\x1b[2J " + "y" * 200, "sessionId": "s1"},
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": '{"type": "ai-title", "aiTitle": "evil"}',
                                                                "aiTitle": "evil"}]}})
    assert cr.rc_command(two, json.loads((Path(two) / "sessions" / "4242.json").read_text())) == \
        "/remote-control " + ("Plan /exit [2J " + "y" * 200)[:cr.TITLE_MAX]


def test_a_user_given_name_is_cleaned_too():
    assert cr.clean_title("docs\nresearch\x07") == "docs research"
    assert cr.clean_title("\x1b\n ") is None


RC_MENU_TOP = ("● done\n▔▔▔\n   Remote Control\n   This session is available in the Claude mobile app and at "
               "https://claude.ai/code/session_019pZH.\n")
RC_MENU_ON = {opt: RC_MENU_TOP + "".join(("   ❯ " if o == opt else "     ") + o + "\n"
                                         for o in ("Disconnect this session", "Show QR code", "Continue"))
              + "   Enter to select · Esc to continue\n" for opt in ("Disconnect this session", "Show QR code", "Continue")}


def stale_session(w, cd: str, owner: str) -> None:
    """An idle session of cd connected on bridge session_019pZH, which the transcript says owner registered."""
    rc_session(w, cd, "idle")
    name_session(cd, bridgeSessionId="session_019pZH")
    write_transcript(cd, {"type": "bridge-session", "bridgeSessionId": "cse_01OTHER", "ownerAccountUuid": "uuid-a@x"},
                     {"type": "bridge-session", "bridgeSessionId": "cse_019pZH", "ownerAccountUuid": owner})


def test_a_bridge_registered_by_another_account_is_disconnected_from_its_menu(w, two):
    stale_session(w, two, "uuid-amv@x")
    options = ["Continue", "Show QR code", "Disconnect this session"]

    def screen(p):
        keys = [k for _, k, _ in w.sent]
        if ("Enter",) not in keys:
            return f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
        return RC_MENU_ON[options[min(keys.count(("Up",)), 2)]]
    w.ctx.tmux_capture = screen
    assert cr.Rotator(w.ctx).disconnect_stale(two) == ["%1"]
    assert w.sent == RC_FREE[:3] + [("%1", ("Up",), False), ("%1", ("Up",), False), ("%1", ("Enter",), False)]
    assert "rc_stale_disconnected" in w.ctx.out.getvalue()


def test_a_bridge_of_the_current_account_is_left_connected(w, two):
    stale_session(w, two, "uuid-a@x")
    assert cr.Rotator(w.ctx).disconnect_stale(two) == []
    assert w.sent == []


def test_a_bridge_without_an_owner_in_the_transcript_is_left_connected(w, two):
    rc_session(w, two, "idle")
    name_session(two, bridgeSessionId="session_019pZH")
    assert cr.Rotator(w.ctx).disconnect_stale(two) == []
    assert w.sent == []


def test_a_busy_session_on_a_stale_bridge_waits_for_its_turn_to_end(w, two):
    stale_session(w, two, "uuid-amv@x")
    name_session(two, status="busy")
    assert cr.Rotator(w.ctx).disconnect_stale(two) == []
    assert w.sent == []


def test_a_menu_without_the_disconnect_option_is_closed(w, two):
    stale_session(w, two, "uuid-amv@x")
    w.screen = "● done\n───\n❯ \n───\n"
    assert cr.Rotator(w.ctx).disconnect_stale(two) == []
    assert w.sent == RC_FREE + [("%1", ("Escape",), False)]
    assert "rc_disconnect_failed" in w.ctx.out.getvalue()


ZALO = "Ingeniería inversa Zalo para llamadas"
VIEW_SCREEN = ("● La F3 está en qg.\n✻ Cogitated for 40s\n" + "─" * 40 + f" {ZALO} ─\n❯ \n" + "─" * 60 + "\n  ○ rc ~/projects\n")


def bg_job(w, cd: str, bridge, owner, screen: str = VIEW_SCREEN) -> None:
    """A background session of cd (pid 5151, job d98214a5, no pane) on the given bridge, open in an agent view on %9."""
    (Path(cd) / "settings.json").write_text(json.dumps({"remoteControlAtStartup": True}))
    (Path(cd) / "sessions").mkdir(exist_ok=True)
    (Path(cd) / "sessions" / "5151.json").write_text(json.dumps(
        {"pid": 5151, "sessionId": "d98214a5-7be1", "kind": "bg", "status": "busy", "jobId": "d98214a5", "name": ZALO}))
    job = Path(cd) / "jobs" / "d98214a5"
    job.mkdir(parents=True)
    job.joinpath("state.json").write_text(json.dumps(
        {"state": "working", "name": ZALO, "bridgeSessionId": bridge, "bridgeOwnerAccountUuid": owner}))
    w.procs = [cr.Proc(5151, cd, None, None), cr.Proc(6161, cd, "%9", None)]
    w.screen = screen


BG_RC = [("%9", (f"/remote-control {ZALO}",), True), ("%9", ("Enter",), False)]


def test_a_background_session_on_another_account_reconnects_from_its_agent_view(w, two):
    bg_job(w, two, "cse_01EcKz", "uuid-juanma@x")
    rot = cr.Rotator(w.ctx)
    assert rot.reconnect_bg(two) == ["%9"]
    assert w.sent == BG_RC
    assert rot.reconnect_bg(two) == []


def test_a_background_session_without_a_bridge_reconnects_from_its_agent_view(w, two):
    bg_job(w, two, None, None)
    assert cr.Rotator(w.ctx).reconnect_bg(two) == ["%9"]
    assert w.sent == BG_RC


def test_a_background_session_on_the_current_account_is_left_alone(w, two):
    bg_job(w, two, "cse_01LKAW", "uuid-a@x")
    assert cr.Rotator(w.ctx).reconnect_bg(two) == []
    assert w.sent == []


def test_an_agent_view_showing_another_session_is_left_alone(w, two):
    bg_job(w, two, None, None, screen=VIEW_SCREEN.replace(ZALO, "Miniserie Claude en Blender"))
    assert cr.Rotator(w.ctx).reconnect_bg(two) == []
    assert w.sent == []


def test_an_agent_view_with_a_draft_is_left_alone(w, two):
    bg_job(w, two, None, None, screen=VIEW_SCREEN.replace("❯ \n", "❯ estoy escribiendo\n"))
    assert cr.Rotator(w.ctx).reconnect_bg(two) == []
    assert w.sent == []


def test_viewed_session_reads_the_title_above_the_input_box():
    assert cr.viewed_session(VIEW_SCREEN) == ZALO
    assert cr.viewed_session("● done\n❯ \n") is None


def test_text_that_never_shows_is_not_submitted_and_the_box_is_cleared(w, two):
    rc_session(w, two, "idle")
    w.ctx.tmux_capture = lambda p: "● done\n───\n❯ \n───\n"
    assert not cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == [TYPED[0], ("%1", tuple(cr.CLEAR_KEYS), False)]
    assert "type_failed" in w.ctx.out.getvalue()


def test_two_dropped_enters_still_submit(w, two):
    rc_session(w, two, "idle")
    w.enter_dropped = 2
    assert cr.Rotator(w.ctx).type_line(w.procs[0], LONG_TEXT)
    assert w.sent == TYPED + [("%1", ("Enter",), False)] * 2


def test_a_bridge_missing_for_less_than_the_grace_period_is_not_typed_into(w, two):
    rc_session(w, two, "busy")
    rot = cr.Rotator(w.ctx)
    assert rot.reconnect(two) == []
    w.clock.t += cr.RC_GRACE - 1
    assert rot.reconnect(two) == []
    name_session(two, bridgeSessionId="session_01EcKzGy")
    assert rot.reconnect(two) == []
    name_session(two, bridgeSessionId=None)
    assert rot.reconnect(two) == []
    w.clock.t += cr.RC_GRACE - 1
    assert rot.reconnect(two) == []
    assert w.sent == []
    w.clock.t += 1
    assert rot.reconnect(two) == ["%1"]


def answers_with(w, dialog: str) -> None:
    """The pane shows an empty prompt with what was typed in it, and dialog from an Enter until the next Escape."""
    def screen(p):
        keys = [k for _, k, literal in w.sent if not literal]
        if keys and keys[-1] == ("Enter",):
            return dialog
        return f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
    w.ctx.tmux_capture = screen


def test_a_session_whose_remote_control_answers_connected_is_not_typed_into_again(w, two):
    rc_session(w, two, "idle")
    answers_with(w, RC_DIALOG_SCREEN)
    rot = cr.Rotator(w.ctx)
    assert reconnect_after_grace(w, two, rot) == ["%1"]
    assert w.sent == RC_FREE + [("%1", ("Escape",), False)]
    assert "rc_already_on config_dir=" in w.ctx.out.getvalue() and "dialog=connected" in w.ctx.out.getvalue()
    w.sent.clear()
    w.ctx.tmux_capture = lambda p: f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
    w.clock.t += 10 * cr.RC_RETRY
    assert rot.reconnect(two) == []
    assert w.sent == []
    name_session(two, bridgeSessionId="session_01EcKzGy")
    assert rot.reconnect(two) == []
    name_session(two, bridgeSessionId=None)
    assert reconnect_after_grace(w, two, rot) == ["%1"]


def test_a_session_whose_remote_control_answers_not_connected_is_retried(w, two):
    rc_session(w, two, "idle")
    answers_with(w, RC_NOT_CONNECTED_SCREEN)
    rot = cr.Rotator(w.ctx)
    assert reconnect_after_grace(w, two, rot) == ["%1"]
    assert "dialog=not-connected" in w.ctx.out.getvalue()
    w.sent.clear()
    w.clock.t += cr.RC_RETRY
    assert rot.reconnect(two) == ["%1"]
    assert w.sent[:2] == RC_FREE


HAND_DISCONNECTED = {"type": "system", "subtype": "local_command", "level": "info", "sessionId": "s1",
                     "content": "<local-command-stdout>Remote Control disconnected.</local-command-stdout>",
                     "commandRun": {"command": "remote-control", "args": ""}}


def test_a_session_disconnected_by_hand_is_not_reconnected_until_it_has_a_bridge_again(w, two):
    rc_session(w, two, "idle")
    write_transcript(two, {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}})
    rot = cr.Rotator(w.ctx)
    rot.scan_limits(two)
    append_transcript(two, HAND_DISCONNECTED)
    rot.scan_limits(two)
    assert "rc_disconnected_by_hand" in w.ctx.out.getvalue()
    assert reconnect_after_grace(w, two, rot) == []
    w.clock.t += 100 * cr.RC_GRACE
    assert rot.reconnect(two) == []
    assert w.sent == []
    name_session(two, bridgeSessionId="session_01EcKzGy")
    assert rot.reconnect(two) == []
    name_session(two, bridgeSessionId=None)
    assert reconnect_after_grace(w, two, rot) == ["%1"]


def test_the_login_message_about_remote_control_is_not_a_disconnect_by_hand(w, two):
    rc_session(w, two, "idle")
    write_transcript(two, {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}})
    rot = cr.Rotator(w.ctx)
    rot.scan_limits(two)
    append_transcript(two, {"type": "user", "message": {"role": "user", "content":
                            "<local-command-stdout>Login successful. Remote Control disconnected.</local-command-stdout>"}},
                      {"type": "system", "subtype": "informational", "content": "Remote Control disconnected — signed-in "
                       "claude.ai account or organization changed on this machine — run /remote-control"})
    rot.scan_limits(two)
    assert reconnect_after_grace(w, two, rot) == ["%1"]


def test_a_stale_bridge_disconnected_by_the_daemon_is_reconnected(w, two):
    stale_session(w, two, "uuid-amv@x")
    options = ["Continue", "Show QR code", "Disconnect this session"]

    def screen(p):
        keys = [k for _, k, _ in w.sent]
        if ("Enter",) not in keys:
            return f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
        return RC_MENU_ON[options[min(keys.count(("Up",)), 2)]]
    w.ctx.tmux_capture = screen
    rot = cr.Rotator(w.ctx)
    rot.scan_limits(two)
    assert rot.disconnect_stale(two) == ["%1"]
    append_transcript(two, HAND_DISCONNECTED)
    rot.scan_limits(two)
    assert "rc_disconnected_by_hand" not in w.ctx.out.getvalue()
    w.ctx.tmux_capture = lambda p: f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
    name_session(two, bridgeSessionId=None)
    w.clock.t += cr.RC_RETRY
    assert reconnect_after_grace(w, two, rot) == ["%1"]


def test_a_bridge_from_before_the_switch_with_no_known_owner_is_disconnected(w, two):
    rot = rotated_session(w, two, "idle", "session_old")
    w.clock.t += cr.RC_SETTLE
    w.ctx.tmux_capture = lambda p: (RC_MENU_ON["Disconnect this session"] if ("Enter",) in [k for _, k, _ in w.sent]
                                    else f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n")
    assert rot.disconnect_stale(two) == ["%1"]


def test_a_new_bridge_with_no_known_owner_is_left_connected_and_its_owner_read_again_later(w, two):
    rot = rotated_session(w, two, "idle", "session_019pZH")
    w.clock.t += cr.RC_SETTLE
    assert rot.disconnect_stale(two) == []
    append_transcript(two, {"type": "bridge-session", "bridgeSessionId": "cse_019pZH", "ownerAccountUuid": "uuid-a@x"})
    w.clock.t += cr.OWNER_RECHECK - 1
    assert rot.disconnect_stale(two) == []
    assert w.sent == []
    w.ctx.tmux_capture = lambda p: (RC_MENU_ON["Disconnect this session"] if ("Enter",) in [k for _, k, _ in w.sent]
                                    else f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n")
    w.clock.t += 1
    assert rot.disconnect_stale(two) == ["%1"]


@pytest.mark.parametrize("dialog", [None, RC_NOT_CONNECTED_SCREEN])
def test_a_session_whose_bridge_never_shows_up_is_retried_ever_more_rarely(w, two, dialog):
    rc_session(w, two, "busy")
    if dialog:
        answers_with(w, dialog)
    rot = cr.Rotator(w.ctx)
    sends = []
    for _ in range(36 * 3600 // 15):
        rot.dismiss_rc_dialogs(two)
        sends += rot.reconnect(two)
        w.clock.t += 15
    assert len(sends) == 77
    w.ctx.tmux_capture = lambda p: f"● done\n───\n❯ {w.box.get('%1', '')}\n───\n"
    name_session(two, bridgeSessionId="session_01EcKzGy")
    rot.reconnect(two)
    name_session(two, bridgeSessionId=None)
    assert reconnect_after_grace(w, two, rot) == ["%1"]
    w.clock.t += cr.RC_RETRY
    assert rot.reconnect(two) == ["%1"]


def test_a_connected_dialog_closed_on_the_next_tick_stops_further_sends(w, two):
    rc_session(w, two, "busy")
    rot = cr.Rotator(w.ctx)
    assert reconnect_after_grace(w, two, rot) == ["%1"]
    w.screen = RC_DIALOG_SCREEN
    rot.dismiss_rc_dialogs(two)
    assert rot.dismiss_rc_dialogs(two) == ["%1"]
    assert "dialogs=connected" in w.ctx.out.getvalue()
    w.screen = "● done\n───\n❯ \n───\n"
    w.clock.t += 100 * cr.RC_BACKOFF_MAX
    assert rot.reconnect(two) == []


def test_a_bridge_from_before_the_switch_with_no_known_owner_gets_one_disconnect_try(w, two):
    rot = rotated_session(w, two, "idle", "session_old")
    w.clock.t += cr.RC_SETTLE
    w.screen = "● done\n───\n❯ \n───\n"
    assert rot.disconnect_stale(two) == []
    assert "rc_disconnect_failed" in w.ctx.out.getvalue()
    w.sent.clear()
    w.clock.t += 10 * cr.RC_RETRY
    assert rot.disconnect_stale(two) == []
    assert w.sent == []


def stopped_session(w, cd: str, *lines: str) -> cr.Rotator:
    """An idle session of cd whose transcript is lines, and a Rotator that has already baselined cd."""
    (Path(cd) / "sessions").mkdir()
    (Path(cd) / "sessions" / "4242.json").write_text(json.dumps({"sessionId": "s1", "kind": "interactive", "status": "idle"}))
    write_transcript(cd, *(json.loads(ln) for ln in lines))
    w.procs = [cr.Proc(4242, cd, "%1", None)]
    rot = cr.Rotator(w.ctx)
    rot.tick()
    return rot


def test_a_login_error_with_no_account_to_rotate_to_gets_no_compact_nor_continue(w, two):
    st = cr.load_state(w.ctx)
    st["limited"]["b@x"] = RESET
    cr.save_state(w.ctx, st)
    rot = stopped_session(w, two, json.dumps({"type": "user", "message": {"content": "hi"}}))
    append_transcript(two, json.loads(auth_line(NOW + 100, "u-403-a")))
    w.clock.t = NOW + 120
    rot.tick()
    assert cr.is_dead(cr.load_state(w.ctx), "a@x")
    assert "all_limited" in w.ctx.out.getvalue()
    st = cr.load_state(w.ctx)
    st["dead"].pop("a@x")
    cr.save_state(w.ctx, st)
    rot.tick()
    append_transcript(two, json.loads(auth_line(NOW + 150, "u-403-b")))
    w.clock.t = NOW + 180
    rot.tick()
    w.clock.t = NOW + cr.COMPACT_WAIT + 200
    rot.tick()
    assert w.sent == []
    assert "woke" not in w.ctx.out.getvalue()


def test_a_limit_resolved_by_a_rotation_is_compacted_once_then_told_to_continue(w, two):
    rot = stopped_session(w, two, json.dumps({"type": "user", "message": {"content": "hi"}}))
    append_transcript(two, json.loads(limit_line(NOW + 100)))
    w.clock.t = NOW + 120
    rot.tick()
    assert cr.current_email(two) == "b@x"
    assert w.sent == COMPACT
    w.sent.clear()
    append_transcript(two, json.loads(limit_line(NOW + 125, uuid="u-limit-inflight")))
    w.clock.t = NOW + 150
    rot.tick()
    assert w.sent == []
    append_transcript(two, {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
                      {"type": "user", "isCompactSummary": True, "message": {"content": "This session is being continued"}})
    w.clock.t = NOW + 200
    rot.tick()
    assert w.sent == CONTINUE
    w.sent.clear()
    w.clock.t = NOW + 300
    rot.tick()
    assert w.sent == []


def test_a_limit_from_after_the_last_switch_is_not_compacted_until_resolved(w, two):
    rot = stopped_session(w, two)
    cr.switch(w.ctx, two, "b@x")
    append_transcript(two, json.loads(limit_line(NOW + cr.INFLIGHT_GRACE + 10)))
    w.clock.t = NOW + cr.INFLIGHT_GRACE + 20
    assert rot.wake(two) == []
    assert w.sent == []


def test_a_limit_of_the_same_account_is_compacted_once_it_resets(w, two):
    st = cr.load_state(w.ctx)
    st["limited"]["b@x"] = RESET + 3600
    cr.save_state(w.ctx, st)
    rot = stopped_session(w, two, json.dumps({"type": "user", "message": {"content": "hi"}}))
    append_transcript(two, json.loads(limit_line(NOW + 100)))
    w.clock.t = NOW + 120
    rot.tick()
    assert cr.current_email(two) == "a@x"
    w.clock.t = RESET - 1
    rot.tick()
    assert w.sent == []
    w.clock.t = RESET
    rot.tick()
    assert w.sent == COMPACT
