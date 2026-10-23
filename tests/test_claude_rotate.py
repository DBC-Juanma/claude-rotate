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


def limit_line(ts: float, uuid: str = "u-limit", text: str = LIMIT_TEXT) -> str:
    return json.dumps({
        "type": "assistant", "isApiErrorMessage": True, "uuid": uuid,
        "timestamp": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
    })


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
        self.screen = "● done\n───\n❯ \n───\n  status line\n"
        self.ctx = cr.Ctx(root=tmp_path / "root", now=self.clock, sleep=lambda s: None,
                          list_procs=lambda: self.procs, tmux_send=self._send, tmux_capture=lambda p: self.screen,
                          out=io.StringIO(),
                          run_claude=self._run_claude, token_email=lambda at: self.owners.get(at))

    def _send(self, proc, keys, literal=False):
        self.sent.append((proc.pane, tuple(keys), literal))

    def _run_claude(self, config_dir):
        self.claude_runs.append(json.loads((Path(config_dir) / ".claude.json").read_text())["oauthAccount"]["emailAddress"])
        if self.claude_writes:
            (Path(config_dir) / ".credentials.json").write_text(json.dumps({"claudeAiOauth": self.claude_writes}))
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


def test_parse_reset_later_today_in_named_zone():
    assert cr.parse_reset(LIMIT_TEXT, NOW) == dt.datetime(2026, 10, 22, 21, 0, tzinfo=MADRID).timestamp()


def test_parse_reset_rolls_to_tomorrow_when_hour_has_passed():
    late = dt.datetime(2026, 10, 22, 22, 30, tzinfo=MADRID).timestamp()
    assert cr.parse_reset(LIMIT_TEXT, late) == dt.datetime(2026, 10, 23, 21, 0, tzinfo=MADRID).timestamp()


def test_parse_reset_with_date_and_minutes():
    text = "You've hit your weekly limit · resets Oct 25, 3:30am (UTC)"
    assert cr.parse_reset(text, NOW) == dt.datetime(2026, 10, 25, 3, 30, tzinfo=dt.timezone.utc).timestamp()


def test_parse_reset_without_reset_time_is_none():
    assert cr.parse_reset("Usage limit reached", NOW) is None


def test_entry_limit_matches_synthetic_limit_error():
    hit = cr.entry_limit(limit_line(NOW, uuid="u7"))
    assert hit == cr.Hit(LIMIT_TEXT, NOW, "u7")


def test_entry_limit_ignores_model_text_quoting_the_limit():
    e = json.loads(limit_line(NOW))
    e["message"]["model"] = "claude-opus-5-5"
    assert cr.entry_limit(json.dumps(e)) is None


def test_entry_limit_with_auth_pattern():
    line = limit_line(NOW, text="OAuth token has expired. Please run /login")
    assert cr.entry_limit(line) is None
    assert cr.entry_limit(line, cr.AUTH_RE).text == "OAuth token has expired. Please run /login"


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
    w.claude_reply = (1, "OAuth token has expired. Please run /login")
    assert cr.renew(w.ctx, "b@x") == "dead"
    dead = cr.load_state(w.ctx)["dead"]["b@x"]
    assert dead["reason"] == "OAuth token has expired. Please run /login"
    assert w.slot_tokens("b@x") == oauth("b1")


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
    assert w.sent == [("%1", ("Escape",), False), ("%1", (cr.WAKE_TEXT,), True), ("%1", ("Enter",), False)]


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


RC_FREE = [("%1", ("Escape",), False), ("%1", (cr.RC_TEXT,), True), ("%1", ("Enter",), False), ("%1", ("Escape",), False)]
RC_BUSY = [("%1", (cr.RC_TEXT,), True), ("%1", ("Enter",), False)]


def rc_session(w, cd: str, status: str) -> None:
    (Path(cd) / "settings.json").write_text(json.dumps({"remoteControlAtStartup": True}))
    (Path(cd) / "sessions").mkdir()
    (Path(cd) / "sessions" / "4242.json").write_text(json.dumps(
        {"sessionId": "s1", "kind": "interactive", "status": status, "bridgeSessionId": None}))
    w.procs = [cr.Proc(4242, cd, "%1", None)]


@pytest.mark.parametrize("status,keys", [("idle", RC_FREE), ("shell", RC_FREE), ("busy", RC_BUSY)])
def test_reconnect_runs_remote_control_in_every_session_without_bridge(w, two, status, keys):
    rc_session(w, two, status)
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
    assert w.sent == keys


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_reconnect_leaves_a_draft_in_the_input_box_alone(w, two, status):
    rc_session(w, two, status)
    w.screen = "● done\n───\n❯ lanza el benchmark ya\n───\n  status line\n"
    assert cr.Rotator(w.ctx).reconnect(two) == []
    assert w.sent == []


def test_reconnect_types_over_a_dim_suggestion(w, two):
    rc_session(w, two, "busy")
    w.screen = "───\n\x1b[39m❯\xa0\x1b[2mlanza el benchmark ya\x1b[0m\n───\n"
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
    assert w.sent == RC_BUSY


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
    rc_session(w, two, "busy")
    w.screen = "❯ earlier prompt\n   Remote Control\n   ❯ Continue\n   Enter to select · Esc to continue\n"
    assert cr.Rotator(w.ctx).reconnect(two) == []
