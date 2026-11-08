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
    assert w.sent == [("%1", ("Escape",), False), ("%1", (cr.WAKE_TEXT,), True), ("%1", ("Enter",), False)]



def test_wake_asks_for_a_status_summary_before_continuing():
    assert "status summary" in cr.WAKE_TEXT
    assert cr.WAKE_TEXT.index("summary") < cr.WAKE_TEXT.index("continue where you stopped")


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
    w.clock.t += cr.RC_SETTLE
    assert rot.reconnect(two) == ["%1"]
    assert w.sent == [("%1", ("Escape",), False), ("%1", ("/remote-control yed18-s",), True), ("%1", ("Enter",), False),
                      ("%1", ("Escape",), False)]
    w.sent.clear()
    name_session(two, bridgeSessionId="session_new")
    assert rot.wake(two) == ["%1"]
    assert w.sent == [("%1", ("Escape",), False), ("%1", (cr.WAKE_TEXT,), True), ("%1", ("Enter",), False)]
    w.sent.clear()
    assert rot.summarize(two) == []

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


@pytest.mark.parametrize("status,keys", [("idle", RC_FREE), ("shell", RC_FREE), ("busy", RC_BUSY), ("waiting", [])])
def test_reconnect_runs_remote_control_in_every_session_without_bridge(w, two, status, keys):
    rc_session(w, two, status)
    assert cr.Rotator(w.ctx).reconnect(two) == (["%1"] if keys else [])
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
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
    assert w.sent == [RC_FREE[0], ("%1", ("/remote-control docs-research",), True)] + RC_FREE[2:]


def test_reconnect_uses_the_generated_title_not_the_name_derived_from_the_directory(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="calm-your-socks-16", nameSource="derived")
    write_transcript(two, {"type": "ai-title", "aiTitle": "Propuesta calma your sock", "sessionId": "s1"},
                     {"type": "user", "message": {"content": "hi"}},
                     {"type": "ai-title", "aiTitle": "Técnica flip en el QG", "sessionId": "s1"})
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
    assert w.sent == [RC_FREE[0], ("%1", ("/remote-control Técnica flip en el QG",), True)] + RC_FREE[2:]


def test_reconnect_without_a_title_leaves_the_remote_session_unnamed(w, two):
    rc_session(w, two, "idle")
    name_session(two, name="vokoban-de", nameSource="derived")
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
    assert w.sent == RC_FREE


SUMMARY = [("%1", ("Escape",), False), ("%1", (cr.SUMMARY_TEXT,), True), ("%1", ("Enter",), False)]


def rotated_session(w, cd: str, status: str, bridge) -> cr.Rotator:
    """A session of cd with the given status, a Rotator that just switched cd to b@x while the session was on bridge
    session_old, and the session now on bridge."""
    rc_session(w, cd, status)
    name_session(cd, bridgeSessionId="session_old")
    write_transcript(cd, {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}})
    rot = cr.Rotator(w.ctx)
    rot.rotate(cd)
    assert cr.current_email(cd) == "b@x"
    assert w.sent == []
    name_session(cd, bridgeSessionId=bridge)
    return rot


def test_the_summary_is_not_sent_on_the_bridge_that_dies_with_the_switch(w, two):
    rot = rotated_session(w, two, "idle", "session_old")
    assert rot.summarize(two) == []
    assert w.sent == []


def test_after_a_rotation_an_idle_session_writes_the_summary_once(w, two):
    rot = rotated_session(w, two, "idle", "session_new")
    assert rot.summarize(two) == ["%1"]
    assert w.sent == SUMMARY
    w.sent.clear()
    assert rot.summarize(two) == []
    assert w.sent == []


def test_the_summary_waits_for_the_new_remote_control_bridge(w, two):
    rot = rotated_session(w, two, "idle", None)
    assert rot.summarize(two) == []
    name_session(two, bridgeSessionId="session_new")
    assert rot.summarize(two) == ["%1"]
    assert w.sent == SUMMARY


def test_the_summary_waits_until_a_busy_session_is_idle(w, two):
    rot = rotated_session(w, two, "busy", "session_new")
    assert rot.summarize(two) == []
    assert w.sent == []
    name_session(two, status="idle")
    assert rot.summarize(two) == ["%1"]
    assert w.sent == SUMMARY


def test_the_summary_waits_for_a_draft_in_the_input_box(w, two):
    rot = rotated_session(w, two, "idle", "session_new")
    w.screen = "● done\n───\n❯ lanza el benchmark ya\n───\n"
    assert rot.summarize(two) == []
    w.screen = "● done\n───\n❯ \n───\n"
    assert rot.summarize(two) == ["%1"]


def test_a_session_stopped_by_the_limit_gets_only_the_wake_text(w, two):
    rc_session(w, two, "idle")
    name_session(two, bridgeSessionId="session_old")
    write_transcript(two, json.loads(limit_line(NOW - 100)))
    rot = cr.Rotator(w.ctx)
    rot.rotate(two)
    assert w.sent == []
    name_session(two, bridgeSessionId="session_new")
    assert rot.wake(two) == ["%1"]
    assert w.sent == SUMMARY[:1] + [("%1", (cr.WAKE_TEXT,), True), ("%1", ("Enter",), False)]
    w.sent.clear()
    assert rot.summarize(two) == []
    assert w.sent == []


def test_the_summary_text_does_not_ask_to_continue():
    assert "status summary" in cr.SUMMARY_TEXT
    assert "continue" not in cr.SUMMARY_TEXT.lower()

@pytest.mark.parametrize("status", ["idle", "busy"])
def test_reconnect_leaves_a_draft_in_the_input_box_alone(w, two, status):
    rc_session(w, two, status)
    w.screen = "● done\n───\n❯ lanza el benchmark ya\n───\n  status line\n"
    assert cr.Rotator(w.ctx).reconnect(two) == []
    assert w.sent == []


def test_reconnect_types_over_a_dim_suggestion(w, two):
    rc_session(w, two, "idle")
    w.screen = "───\n\x1b[39m❯\xa0\x1b[2mlanza el benchmark ya\x1b[0m\n───\n"
    assert cr.Rotator(w.ctx).reconnect(two) == ["%1"]
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


def test_typing_after_escape_waits_so_the_first_character_is_not_read_as_alt(w, two):
    rc_session(w, two, "idle")
    events = []
    w.ctx.sleep = lambda secs: events.append(("sleep", secs))
    w.ctx.tmux_send = lambda proc, keys, literal=False: events.append(tuple(keys))
    cr.Rotator(w.ctx).type_line(w.procs[0], "Account rotated")
    assert events == [("Escape",), ("sleep", cr.ESCAPE_GAP), ("Account rotated",), ("Enter",), ("sleep", cr.ESCAPE_GAP)]


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
    screens = iter([STUCK_SUMMARY_SCREEN, "● done\n───\n❯ \n───\n"])
    w.ctx.tmux_capture = lambda p: next(screens)
    assert cr.Rotator(w.ctx).type_line(w.procs[0], cr.SUMMARY_TEXT)
    assert w.sent == SUMMARY + [("%1", ("Enter",), False)]


def test_a_line_that_never_submits_is_cleared_not_left_as_a_draft(w, two):
    rc_session(w, two, "idle")
    w.screen = STUCK_SUMMARY_SCREEN
    assert not cr.Rotator(w.ctx).type_line(w.procs[0], cr.SUMMARY_TEXT)
    assert w.sent == SUMMARY + [("%1", ("Enter",), False), ("%1", ("C-u",), False)]
    assert "type_failed" in w.ctx.out.getvalue()


def test_a_submitted_line_echoed_above_the_input_box_does_not_count_as_stuck():
    screen = "❯ Account rotated (claude-rotate) and Remote Control\n● Session status\n───\n❯ \n───\n"
    assert not cr.input_holds(screen, cr.SUMMARY_TEXT)


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
    screens = iter(["● done\n───\n❯ \n───\n", "● done\n───\n❯ \n───\n", RC_MENU_ON["Continue"],
                    RC_MENU_ON["Show QR code"], RC_MENU_ON["Disconnect this session"]])
    w.ctx.tmux_capture = lambda p: next(screens)
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
    screens = iter(["● done\n───\n❯ \n───\n"] * 2 + ["● done\n───\n❯ \n───\n"] * cr.MENU_STEPS)
    w.ctx.tmux_capture = lambda p: next(screens)
    assert cr.Rotator(w.ctx).disconnect_stale(two) == []
    assert w.sent == RC_FREE[:3] + [("%1", ("Escape",), False)]
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
