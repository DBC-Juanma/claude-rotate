#!/usr/bin/env python3
"""claude-rotate: rotate the Claude Code login across several accounts when one hits its usage limit."""
import argparse
import contextlib
import datetime as dt
import fcntl
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Optional

DEFAULT_CONFIG_DIR = str(Path.home() / ".claude")
DEFAULT_ROOT = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-rotate"
FALLBACK_HOURS = 5
GUARD_SECONDS = 120
GUARD_STEP = 5
INFLIGHT_GRACE = 90
TAIL_BYTES = 262144
RENEW_EVERY = 6 * 3600
RENEW_TIMEOUT = 180
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
LIMIT_ERROR = "usage_limit_reached"
AUTH_ERROR = "authentication_failed"
COMPACT_TEXT = ("/compact Account rotated (claude-rotate). Keep what this session was doing, what is done, what is pending "
                "and the next step.")
CONTINUE_TEXT = "Account rotated (claude-rotate) and the conversation compacted. Continue where you stopped."
ACTIVE_WINDOW = 30 * 60
COMPACT_WAIT = 10 * 60
COMPACT_MARK = b'"compact_boundary"'
NAMED = ("user", "auto")
POLL = 0.5
TYPE_POLLS = 6
ENTER_TRIES = 4
CLEAR_KEYS = ["C-u", "BSpace"] * (ENTER_TRIES + 2) + ["C-u"]
TITLE_MAX = 80
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
RC_TEXT = "/remote-control"
RC_RETRY = 30
PROMPT_FREE = ("idle", "shell")
RC_ANYTIME = PROMPT_FREE + ("busy",)
SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
DIM_RE = re.compile(r"\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)")
RC_BROKEN = ("requires a claude.ai subscription", "Unknown command: /remote-control")
RC_DIALOG = (("This session is available in the Claude mobile app", "Remote Control isn't connected"), "Esc to continue")
RC_SETTLE = 20
RC_DISCONNECT = "Disconnect this session"
RC_GRACE = 60
RC_BACKOFF_MAX = 1800
OWNER_RECHECK = 300
HAND_DISCONNECT = b"<local-command-stdout>Remote Control disconnected.</local-command-stdout>"
MENU_STEPS = 4
RESTART_COOLDOWN = 1800
EXIT_WAIT = 30
SHELLS = ("bash", "zsh", "sh", "fish", "dash")
IDLE_STOPS = ("end_turn", "stop_sequence")


class Proc(NamedTuple):
    pid: int
    config_dir: str
    pane: Optional[str]
    tmux_socket: Optional[str]


def real_list_procs() -> list:
    """Return every running `claude` process as Proc(pid, config_dir, pane, tmux_socket)."""
    out = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True).stdout.split()
    procs = []
    for pid in out:
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        env = dict(p.split("=", 1) for p in raw.decode(errors="replace").split("\0") if "=" in p)
        cd = env.get("CLAUDE_CONFIG_DIR") or DEFAULT_CONFIG_DIR
        tmux = env.get("TMUX", "")
        procs.append(Proc(int(pid), os.path.normpath(cd), env.get("TMUX_PANE"), tmux.split(",")[0] or None))
    return procs


def _tmux(proc: Proc, *args) -> str:
    cmd = ["tmux"] + (["-S", proc.tmux_socket] if proc.tmux_socket else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def real_tmux_send(proc: Proc, keys: list, literal: bool = False) -> None:
    """Send keys (or literal text) to the proc's tmux pane."""
    _tmux(proc, "send-keys", "-t", proc.pane, *(["-l"] if literal else []), *keys)


def real_tmux_capture(proc: Proc) -> str:
    """Return the visible screen of the proc's tmux pane, with its SGR escape codes."""
    return _tmux(proc, "capture-pane", "-p", "-e", "-t", proc.pane)


def claude_env(config_dir: str) -> dict:
    """Return os.environ pointed at config_dir, without auth variables that would override its OAuth login."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def real_run_claude(config_dir: str) -> tuple:
    """Run a one-word prompt with claude in an isolated config dir; return (returncode, stdout). Raises on timeout."""
    r = subprocess.run(["claude", "-p", "Reply with the single word ok", "--output-format", "json"], env=claude_env(config_dir),
                       capture_output=True, text=True, timeout=RENEW_TIMEOUT, cwd=config_dir)
    return r.returncode, r.stdout + ("\n" + r.stderr if r.returncode else "")


def real_claude_login(config_dir: str, email: Optional[str]) -> None:
    """Run the interactive `claude auth login` against config_dir. Raises if it fails."""
    subprocess.run(["claude", "auth", "login"] + (["--email", email] if email else []), env=claude_env(config_dir), check=True)


def real_token_email(access_token: str) -> Optional[str]:
    """Return the email of the account owning access_token via the OAuth profile endpoint, or None if it cannot be resolved."""
    req = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return (json.load(r).get("account") or {}).get("email")
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Ctx:
    """Runtime context: state locations and injectable side effects."""

    def __init__(self, root: Path = DEFAULT_ROOT, now: Callable = time.time, sleep: Callable = time.sleep,
                 list_procs: Callable = real_list_procs,
                 tmux_send: Callable = real_tmux_send, tmux_capture: Callable = real_tmux_capture, out=None, run_claude: Callable = real_run_claude,
                 token_email: Callable = real_token_email, claude_login: Callable = real_claude_login):
        self.root = Path(root)
        self.now = now
        self.sleep = sleep
        self.list_procs = list_procs
        self.tmux_send = tmux_send
        self.tmux_capture = tmux_capture
        self.run_claude = run_claude
        self.token_email = token_email
        self.claude_login = claude_login
        self.out = out or sys.stdout
        self._depth = 0
        self._fh = None

    @property
    def vault(self) -> Path:
        return self.root / "accounts"

    def log(self, event: str, **kv) -> None:
        ts = dt.datetime.fromtimestamp(self.now()).strftime("%Y-%m-%d %H:%M:%S")
        extra = " ".join(f"{k}={v}" for k, v in kv.items() if v is not None)
        print(f"{ts} {event} {extra}".rstrip(), file=self.out, flush=True)

    @contextlib.contextmanager
    def lock(self):
        """Exclusive, re-entrant inter-process lock."""
        if self._depth == 0:
            self.root.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.root / "lock", "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None


def atomic_write_json(path: Path, obj, mode: int = 0o600) -> None:
    """Write obj as JSON to path atomically with the given mode."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default=None):
    """Read JSON from path; return default if missing."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def ensure_private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)


def load_config(ctx: Ctx) -> dict:
    """Load config, creating it with defaults when missing."""
    path = ctx.root / "config.json"
    cfg = read_json(path)
    if cfg is None:
        ensure_private_dir(ctx.root)
        cfg = {"config_dirs": [DEFAULT_CONFIG_DIR], "order": []}
        atomic_write_json(path, cfg)
    return cfg


def save_config(ctx: Ctx, cfg: dict) -> None:
    ensure_private_dir(ctx.root)
    atomic_write_json(ctx.root / "config.json", cfg)


def load_state(ctx: Ctx) -> dict:
    st = read_json(ctx.root / "state.json", {})
    st.setdefault("limited", {})
    st.setdefault("guards", {})
    st.setdefault("switched_at", {})
    st.setdefault("dead", {})
    st.setdefault("renewed", {})
    return st


def save_state(ctx: Ctx, st: dict) -> None:
    ensure_private_dir(ctx.root)
    atomic_write_json(ctx.root / "state.json", st)


def slot_path(ctx: Ctx, email: str) -> Path:
    return ctx.vault / f"{email}.json"


def load_slot(ctx: Ctx, email: str) -> Optional[dict]:
    return read_json(slot_path(ctx, email))


def save_slot(ctx: Ctx, slot: dict) -> None:
    ensure_private_dir(ctx.vault)
    slot["updated_at"] = ctx.now()
    atomic_write_json(slot_path(ctx, slot["email"]), slot)


def list_slots(ctx: Ctx) -> list:
    if not ctx.vault.is_dir():
        return []
    return [read_json(p) for p in sorted(ctx.vault.glob("*.json"))]


def creds_path(cd: str) -> Path:
    return Path(cd) / ".credentials.json"


def live_oauth(cd: str) -> Optional[dict]:
    return (read_json(creds_path(cd), {}) or {}).get("claudeAiOauth")


def write_live_oauth(cd: str, oauth: dict) -> None:
    """Replace only claudeAiOauth in the credentials file, keeping every other key."""
    data = read_json(creds_path(cd), {}) or {}
    data["claudeAiOauth"] = oauth
    atomic_write_json(creds_path(cd), data)


def account_path(cd: str) -> Path:
    """The file holding oauthAccount: ~/.claude.json for the default ~/.claude dir, else <cd>/.claude.json."""
    return Path.home() / ".claude.json" if Path(cd) == Path(DEFAULT_CONFIG_DIR) else Path(cd) / ".claude.json"


def live_account(cd: str) -> Optional[dict]:
    return (read_json(account_path(cd), {}) or {}).get("oauthAccount")


def write_live_account(cd: str, account: dict) -> None:
    """Replace only oauthAccount in .claude.json, atomically."""
    path = account_path(cd)
    data = read_json(path, {}) or {}
    data["oauthAccount"] = account
    atomic_write_json(path, data)


def current_email(cd: str) -> Optional[str]:
    acc = live_account(cd)
    return acc.get("emailAddress") if acc else None


def enroll(ctx: Ctx, cd: str) -> str:
    """Snapshot the account logged in cd into the vault after checking the token's owner; return its email."""
    with ctx.lock():
        acc, oauth = live_account(cd), live_oauth(cd)
        if not acc or not acc.get("emailAddress") or not oauth:
            raise SystemExit(f"no logged-in account found in {cd}")
        email = acc["emailAddress"]
        owner = ctx.token_email(oauth["accessToken"])
        if owner != email:
            raise SystemExit(f"the token in {cd} belongs to {owner or 'an account that could not be resolved'}, not {email}")
        save_slot(ctx, {"email": email, "claudeAiOauth": oauth, "oauthAccount": acc})
        cfg = load_config(ctx)
        if email not in cfg["order"]:
            cfg["order"].append(email)
            save_config(ctx, cfg)
        st = load_state(ctx)
        if st["dead"].pop(email, None):
            save_state(ctx, st)
        ctx.log("enroll", email=email, config_dir=cd)
        return email


def login(ctx: Ctx, email: Optional[str] = None) -> str:
    """Log in to a new account in a throwaway config dir, enroll it and delete the dir; return its email."""
    with tempfile.TemporaryDirectory(prefix="claude-rotate-login-") as tmp:
        ctx.claude_login(tmp, email)
        got = enroll(ctx, tmp)
    print(f"enrolled {got}", file=ctx.out)
    return got


def switch(ctx: Ctx, cd: str, target: str, reason: str = "use") -> None:
    """Switch cd to the target account, saving the outgoing account's live tokens first."""
    with ctx.lock():
        slot = load_slot(ctx, target)
        if slot is None:
            raise SystemExit(f"account {target} is not enrolled")
        if not usable_oauth(slot.get("claudeAiOauth")):
            _mark_dead(ctx, target, "vault slot has no usable tokens")
            raise SystemExit(f"account {target} has no usable tokens; log in again and run enroll")
        dead = load_state(ctx)["dead"].get(target)
        if dead:
            raise SystemExit(f"account {target} is dead ({dead['reason']}); log in again and run enroll")
        prev = current_email(cd)
        live = live_oauth(cd)
        if prev and prev != target and live and load_slot(ctx, prev):
            ps = load_slot(ctx, prev)
            ps["claudeAiOauth"] = live
            save_slot(ctx, ps)
        write_live_oauth(cd, slot["claudeAiOauth"])
        write_live_account(cd, slot["oauthAccount"])
        st = load_state(ctx)
        st["guards"][cd] = {"target": target, "prev": prev, "until": ctx.now() + GUARD_SECONDS}
        st["switched_at"][cd] = ctx.now()
        save_state(ctx, st)
        ctx.log("switch", reason=reason, config_dir=cd, **{"from": prev, "to": target})


def is_limited(st: dict, email: str, now: float) -> bool:
    return st["limited"].get(email, 0) > now


def is_dead(st: dict, email: str) -> bool:
    return email in st["dead"]


def next_available(ctx: Ctx, cur: Optional[str]) -> Optional[str]:
    """Round-robin: the next enrolled account after cur that is not limited."""
    cfg, st, now = load_config(ctx), load_state(ctx), ctx.now()
    order = cfg["order"]
    if not order:
        return None
    start = order.index(cur) + 1 if cur in order else 0
    for i in range(len(order)):
        cand = order[(start + i) % len(order)]
        slot = load_slot(ctx, cand)
        if cand != cur and slot and usable_oauth(slot.get("claudeAiOauth")) and not is_limited(st, cand, now) and not is_dead(st, cand):
            return cand
    return None


def live_emails(ctx: Ctx) -> set:
    """Emails currently logged in on any configured config dir."""
    return {e for e in (current_email(cd) for cd in load_config(ctx)["config_dirs"] if Path(cd).is_dir()) if e}


def usable_oauth(o) -> bool:
    return isinstance(o, dict) and bool(o.get("accessToken")) and bool(o.get("refreshToken"))


def renew(ctx: Ctx, email: str) -> str:
    """Renew an idle account's tokens through claude in an isolated dir. Returns 'ok', 'dead' or 'error'.

    Holds the lock throughout so no switch can hand out the token being rotated. The vault slot is rewritten
    only with a complete, changed claudeAiOauth; any failure leaves it untouched.
    """
    with ctx.lock():
        slot = load_slot(ctx, email)
        if slot is None or not usable_oauth(slot.get("claudeAiOauth")):
            return _mark_dead(ctx, email, "vault slot has no usable tokens")
        with tempfile.TemporaryDirectory(prefix="claude-rotate-renew-") as t:
            os.chmod(t, 0o700)
            atomic_write_json(Path(t) / ".credentials.json", {"claudeAiOauth": slot["claudeAiOauth"]})
            atomic_write_json(Path(t) / ".claude.json", {"oauthAccount": slot.get("oauthAccount"), "hasCompletedOnboarding": True})
            try:
                rc, out = ctx.run_claude(t)
            except (subprocess.TimeoutExpired, OSError) as e:
                ctx.log("renew_error", email=email, reason=type(e).__name__)
                return "error"
            errors = api_errors(t)
            new = (read_json(Path(t) / ".credentials.json", {}) or {}).get("claudeAiOauth")
        if usable_oauth(new) and new != slot["claudeAiOauth"]:
            slot["claudeAiOauth"] = new
            save_slot(ctx, slot)
        try:
            res = json.loads(out.strip().splitlines()[0] if rc == 0 else out)
        except (ValueError, IndexError):
            res = None
        failed = rc != 0 or not isinstance(res, dict) or res.get("is_error")
        if failed:
            text = str(res.get("result") if isinstance(res, dict) else out).strip().replace("\n", " ")[:160]
            if AUTH_ERROR in errors:
                return _mark_dead(ctx, email, text)
            ctx.log("renew_error", email=email, reason=text or f"exit {rc}")
            return "error"
        if not usable_oauth(new):
            ctx.log("renew_error", email=email, reason="claude left no usable credentials; slot untouched")
            return "error"
        st = load_state(ctx)
        st["renewed"][email] = ctx.now()
        st["dead"].pop(email, None)
        save_state(ctx, st)
        ctx.log("renewed", email=email, expires=fmt_ts(new["expiresAt"] / 1000) if new.get("expiresAt") else None)
        return "ok"


def _mark_dead(ctx: Ctx, email: str, reason: str) -> str:
    st = load_state(ctx)
    st["dead"][email] = {"since": (st["dead"].get(email) or {}).get("since", ctx.now()), "reason": reason, "checked": ctx.now()}
    save_state(ctx, st)
    ctx.log("account_dead", email=email, reason=reason)
    return "dead"


def renew_due(ctx: Ctx, force: bool = False) -> dict:
    """Renew every idle (non-live) enrolled account that is due (all of them when force); return {email: result}."""
    with ctx.lock():
        cfg, st, live = load_config(ctx), load_state(ctx), live_emails(ctx)
        due = [e for e in cfg["order"] if e not in live and load_slot(ctx, e) is not None and
               (force or (not is_dead(st, e) and not is_limited(st, e, ctx.now()) and
                          ctx.now() - st["renewed"].get(e, 0) >= RENEW_EVERY))]
    return {e: renew(ctx, e) for e in due}


def check(ctx: Ctx) -> int:
    """Renew all idle accounts now and print one line each; exit code 1 if any is dead or errored."""
    results = renew_due(ctx, force=True)
    live = live_emails(ctx)
    for e in load_config(ctx)["order"]:
        print(f"  {e}: " + (results.get(e) or ("live (in use, not renewed)" if e in live else "skipped")), file=ctx.out)
    return 0 if all(r == "ok" for r in results.values()) else 1


class Hit(NamedTuple):
    kind: str
    text: str
    ts: Optional[float]
    uuid: Optional[str]
    resets: Optional[float] = None


def entry_ts(e: dict) -> Optional[float]:
    """Epoch of a transcript entry's ISO timestamp, or None."""
    try:
        return dt.datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None


def entry_error(line: str) -> Optional[Hit]:
    """Return a Hit when this transcript line is Claude Code's own usage-limit ('limit') or login ('auth') error, else None.

    Read from the structured fields Claude Code writes on the entry (apiError, error, quotaLimits), never from its text.
    """
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("type") != "assistant" or not e.get("isApiErrorMessage"):
        return None
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    if msg.get("model") != "<synthetic>":
        return None
    kind = "limit" if e.get("apiError") == LIMIT_ERROR else "auth" if e.get("error") == AUTH_ERROR else None
    if kind is None:
        return None
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    resets = (e.get("quotaLimits") or {}).get("resetsAt")
    return Hit(kind, str(content or ""), entry_ts(e), e.get("uuid"), float(resets) if resets else None)


def api_errors(config_dir: str) -> set:
    """The error kinds (the entries' error field) Claude Code recorded in the transcripts under config_dir."""
    kinds = set()
    for f in glob.glob(os.path.join(config_dir, "projects", "**", "*.jsonl"), recursive=True):
        for line in Path(f).read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("isApiErrorMessage") and e.get("error"):
                kinds.add(e["error"])
    return kinds


def last_turn_entry(path: str) -> Optional[str]:
    """The last user or assistant line of a transcript, read from its tail."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            lines = fh.read().split(b"\n")
    except OSError:
        return None
    for raw in reversed(lines):
        line = raw.decode(errors="replace")
        try:
            t = json.loads(line).get("type")
        except (ValueError, AttributeError):
            continue
        if t in ("user", "assistant"):
            return line
    return None


def session_info(cd: str, pid: int) -> dict:
    """The live session record Claude Code keeps at <cd>/sessions/<pid>.json, or {} when absent."""
    return read_json(Path(cd) / "sessions" / f"{pid}.json", {}) or {}


def parent_is_shell(pid: int) -> bool:
    """Whether pid's parent is an interactive shell, so the pane survives the process exiting."""
    try:
        ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        return Path(f"/proc/{ppid}/comm").read_text().strip() in SHELLS
    except (OSError, ValueError, IndexError):
        return False


def rc_wanted(cd: str) -> bool:
    """Whether cd is configured to run Remote Control in every session (remoteControlAtStartup)."""
    return bool((read_json(Path(cd) / "settings.json", {}) or {}).get("remoteControlAtStartup"))


def plain(screen: str) -> str:
    """screen without SGR escape codes."""
    return SGR_RE.sub("", screen)


def prompt_empty(screen: str) -> bool:
    """Whether the input box on a captured Claude Code screen (its last ❯ line) holds no typed text; dim suggestions don't count."""
    prompts = [plain(DIM_RE.sub("", ln)).replace("\xa0", " ").strip() for ln in screen.splitlines() if plain(ln).strip().startswith("❯")]
    return bool(prompts) and prompts[-1] == "❯"


def rc_dialog_open(screen: str) -> bool:
    """Whether a captured screen ends in a Remote Control dialog waiting on "Esc to continue"."""
    lines = [ln.strip() for ln in plain(screen).splitlines() if ln.strip()]
    return bool(lines) and lines[-1].endswith(RC_DIALOG[1]) and any(m in ln for ln in lines[-10:] for m in RC_DIALOG[0])


def rc_dialog_connected(screen: str) -> bool:
    """Whether a captured screen ends in the Remote Control dialog of a connected session ("This session is available")."""
    lines = [ln.strip() for ln in plain(screen).splitlines() if ln.strip()]
    return rc_dialog_open(screen) and any(RC_DIALOG[0][0] in ln for ln in lines[-10:])


def input_holds(screen: str, text: str) -> bool:
    """Whether the input box on a captured Claude Code screen (from its last ❯ line down) still holds the start of text."""
    lines = plain(screen).replace("\xa0", " ").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().startswith("❯")]
    if not starts:
        return False
    box = " ".join(ln.strip() for ln in lines[starts[-1]:]).lstrip("❯ ")
    return text[1:30] in box


def is_idle(line: Optional[str]) -> bool:
    """True when a session's last turn entry is a finished assistant reply or Claude Code's own error."""
    try:
        e = json.loads(line) if line else None
    except ValueError:
        return False
    if not isinstance(e, dict) or e.get("type") != "assistant":
        return False
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    return bool(e.get("isApiErrorMessage")) or msg.get("stop_reason") in IDLE_STOPS


def is_active(info: dict, now: float) -> bool:
    """Whether a session is busy, or its status last changed (its last turn ended) within ACTIVE_WINDOW seconds."""
    return info.get("status") == "busy" or (info.get("statusUpdatedAt") or 0) / 1000 >= now - ACTIVE_WINDOW


def transcript_path(cd: str, sid: Optional[str]) -> Optional[str]:
    """The transcript of session sid under cd, or None."""
    found = glob.glob(os.path.join(cd, "projects", "*", f"{sid}.jsonl")) if sid else []
    return found[0] if found else None


def clean_title(title) -> Optional[str]:
    """title as a single line safe to type into a terminal: control characters and ESC removed, whitespace collapsed,
    at most TITLE_MAX characters; None when nothing is left or title is not a string."""
    if not isinstance(title, str):
        return None
    return " ".join(CONTROL_RE.sub(" ", title).split())[:TITLE_MAX].strip() or None


def session_title(cd: str, info: dict) -> Optional[str]:
    """The name a session shows: the one its user gave it, else the latest title Claude Code generated for it, else None.

    The name Claude Code derives from the directory (nameSource "derived", e.g. yedran-81) is never returned.
    """
    if info.get("nameSource") in NAMED and clean_title(info.get("name")):
        return clean_title(info["name"])
    path = transcript_path(cd, info.get("sessionId"))
    if not path:
        return None
    title = None
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"ai-title"' in raw:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("type") == "ai-title":
                    title = clean_title(e.get("aiTitle")) or title
    return title


def rc_command(cd: str, info: dict) -> str:
    """The /remote-control command for a session, carrying its title so the remote session keeps it."""
    title = session_title(cd, info)
    return f"{RC_TEXT} {title}" if title else RC_TEXT


def rc_missing(cd: str, info: dict) -> bool:
    """Whether an interactive session of cd should run Remote Control and has no bridge."""
    return rc_wanted(cd) and info.get("kind") == "interactive" and not info.get("bridgeSessionId")


def bridge_key(bridge_id: Optional[str]) -> Optional[str]:
    """The id part shared by a bridge's session_<id> (sessions/<pid>.json) and cse_<id> (transcript) names."""
    return bridge_id.split("_", 1)[-1] if bridge_id else None


def bridge_owner(cd: str, info: dict) -> Optional[str]:
    """The account uuid that registered the session's current Remote Control bridge, from the transcript's
    bridge-session entries; None when the session has no bridge or no entry names it."""
    key, path = bridge_key(info.get("bridgeSessionId")), transcript_path(cd, info.get("sessionId"))
    if not key or not path:
        return None
    owner = None
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"bridge-session"' in raw:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("type") == "bridge-session" and bridge_key(e.get("bridgeSessionId")) == key:
                    owner = e.get("ownerAccountUuid") or owner
    return owner


def menu_cursor(screen: str) -> Optional[str]:
    """The option under the cursor (the last ❯ line) of a menu on a captured screen, or None."""
    lines = [ln.strip() for ln in plain(screen).replace("\xa0", " ").splitlines() if ln.strip().startswith("❯")]
    return lines[-1][1:].strip() if lines else None


VIEW_SEPARATOR_RE = re.compile(r"^─+ (.+?) ─$")


def viewed_session(screen: str) -> Optional[str]:
    """The name of the session an agent view shows on a captured screen: the title on the separator line right above
    its input box (its last ❯ line), or None."""
    lines = [ln.strip() for ln in plain(screen).replace("\xa0", " ").splitlines()]
    starts = [i for i, ln in enumerate(lines) if ln.startswith("❯")]
    if not starts or starts[-1] == 0:
        return None
    m = VIEW_SEPARATOR_RE.match(lines[starts[-1] - 1])
    return m.group(1) if m else None


def pid_alive(pid) -> bool:
    """Whether a process with this pid exists."""
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def bg_jobs(cd: str, pids: set) -> list:
    """The daemon job states (<cd>/jobs/<jobId>/state.json) of the live background sessions of cd: those whose process
    is in pids or still exists (the daemon runs them under its own binary name, so pgrep -x claude misses them)."""
    jobs = []
    for f in glob.glob(os.path.join(cd, "sessions", "*.json")):
        info = read_json(Path(f), {}) or {}
        if info.get("kind") == "bg" and (info.get("pid") in pids or pid_alive(info.get("pid"))) and info.get("jobId"):
            state = read_json(Path(cd) / "jobs" / info["jobId"] / "state.json", None)
            if state:
                jobs.append({**state, "jobId": info["jobId"]})
    return jobs


def session_transcript(cd: str, pid: int) -> Optional[str]:
    """The transcript of the session a claude process is running, from <cd>/sessions/<pid>.json."""
    return transcript_path(cd, session_info(cd, pid).get("sessionId"))


class Rotator:
    """The watch loop and its per-config-dir state."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.offsets: dict = {}
        self.baselined: set = set()
        self.waiting: dict = {}
        self.woken: set = set()
        self.rc_sent: dict = {}
        self.rc_restarted: dict = {}
        self.auth_hits: dict = {}
        self.rc_dialogs: set = set()
        self.compact_due: dict = {}
        self.continue_due: dict = {}
        self.old_bridge: dict = {}
        self.owners: dict = {}
        self.owner_checked: dict = {}
        self.rc_gone_since: dict = {}
        self.rc_connected: set = set()
        self.rc_tries: dict = {}
        self.stale_tried: set = set()
        self.hand_off: set = set()
        self.own_disconnects: set = set()

    def type_line(self, p: Proc, text: str) -> bool:
        """Type text into p's empty input box and submit it; returns whether it was submitted.

        No Escape is sent first: it doesn't clear the box, the terminal can read it with the first letter as one Alt
        key, and it would interrupt a busy session. Enter is pressed only once the text shows in the box, since a
        session slow to draw it can drop an Enter that arrives before the text is in. Enter is pressed again, up to
        ENTER_TRIES times, while the text stays in the box (a session sometimes takes an Enter as a new line instead).
        If the text never shows, or never leaves, the box is emptied line by line (Ctrl+U, Backspace) so it is not
        left there as a draft for the user to send.
        """
        ctx = self.ctx
        ctx.tmux_send(p, [text], literal=True)
        if self.wait_box(p, text, True):
            for _ in range(ENTER_TRIES):
                ctx.tmux_send(p, ["Enter"])
                if self.wait_box(p, text, False):
                    return True
        ctx.tmux_send(p, CLEAR_KEYS)
        ctx.log("type_failed", pane=p.pane, text=text[:40])
        return False

    def wait_box(self, p: Proc, text: str, holds: bool) -> bool:
        """Poll p's screen for up to TYPE_POLLS * POLL seconds until its input box holds text (holds) or no longer does."""
        for _ in range(TYPE_POLLS):
            self.ctx.sleep(POLL)
            if input_holds(self.ctx.tmux_capture(p), text) == holds:
                return True
        return False

    def rc_ready(self, cd: str, pid: int, info: dict) -> bool:
        """Whether a session can be asked to write something for its remote viewer: Remote Control is off for cd, or its
        bridge is up and is not the one it had when cd last switched, which dies a few seconds after the switch."""
        return not rc_wanted(cd) or bool(info.get("bridgeSessionId")) and info["bridgeSessionId"] != self.old_bridge.get(pid)

    def baseline(self, cd: str) -> None:
        """Skip everything already in the transcripts of cd."""
        for f in glob.glob(os.path.join(cd, "projects", "**", "*.jsonl"), recursive=True):
            try:
                self.offsets[f] = os.path.getsize(f)
            except OSError:
                pass
        self.baselined.add(cd)

    def scan_limits(self, cd: str) -> list:
        """Return limit Hits from lines appended since the last scan."""
        if cd not in self.baselined:
            self.baseline(cd)
            return []
        hits = []
        for f in glob.glob(os.path.join(cd, "projects", "**", "*.jsonl"), recursive=True):
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            off = self.offsets.get(f, 0)
            if size < off:
                off = 0
            if size == off:
                self.offsets[f] = off
                continue
            with open(f, "rb") as fh:
                fh.seek(off)
                chunk = fh.read(size - off)
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self.offsets[f] = off + end + 1
            for raw in chunk[:end].split(b"\n"):
                if HAND_DISCONNECT in raw and b'"local_command"' in raw:
                    self.note_disconnect(f)
                    continue
                line = raw.decode(errors="replace")
                hit = entry_error(line)
                if hit and hit.kind == "limit":
                    hits.append(hit)
                elif hit:
                    self.auth_hits.setdefault(cd, []).append(hit)
        return hits

    def note_disconnect(self, path: str) -> None:
        """Record a "Remote Control disconnected." command entry appended to the transcript at path: the session's
        user disconnected it by hand, unless disconnect_stale did."""
        if path in self.own_disconnects:
            self.own_disconnects.discard(path)
        else:
            self.hand_off.add(path)
            self.ctx.log("rc_disconnected_by_hand", transcript=path)

    def wake(self, cd: str) -> list:
        """Compact each tmux session of cd whose last turn is a usage-limit error that is resolved, while cd is on a
        healthy account; resume tells it to continue once the compaction is in its transcript.

        A limit error is resolved when cd switched after it (on_limit's in-flight grace included) or when the limit it
        reports has reset. Any other error, such as a failed login, gets nothing. A session already waiting for resume
        is not compacted again. A session that should run Remote Control waits for its new bridge (reconnect brings it
        up), so the compaction is the first thing the new remote session shows.
        """
        ctx, woken = self.ctx, []
        st = load_state(ctx)
        cur = current_email(cd)
        if not cur or is_limited(st, cur, ctx.now()) or is_dead(st, cur):
            return woken
        switched, pending = st["switched_at"].get(cd, 0), self.continue_due.get(cd, {})
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in woken or p.pid in pending:
                continue
            path = session_transcript(cd, p.pid)
            line = last_turn_entry(path) if path else None
            hit = entry_error(line) if line else None
            if not hit or hit.kind != "limit" or (hit.uuid or path) in self.woken:
                continue
            if not (hit.ts is not None and hit.ts <= switched + INFLIGHT_GRACE or hit.resets and hit.resets <= ctx.now()):
                continue
            if not self.rc_ready(cd, p.pid, session_info(cd, p.pid)) or not prompt_empty(ctx.tmux_capture(p)):
                continue
            offset = os.path.getsize(path)
            self.type_line(p, COMPACT_TEXT)
            self.woken.add(hit.uuid or path)
            self.compact_due.get(cd, set()).discard(p.pid)
            self.continue_due.setdefault(cd, {})[p.pid] = (path, offset, ctx.now())
            woken.append(p.pane)
        if woken:
            ctx.log("woke", config_dir=cd, panes=",".join(woken))
        return woken

    def reconnect(self, cd: str) -> list:
        """Run /remote-control in each interactive tmux session of cd that has no Remote Control bridge and an empty input
        box, whether it is free or busy.

        A busy session connects at once without a dialog and keeps working; it gets no Escape before or after the
        command, since Escape would interrupt its turn. A session waiting on a permission prompt is left alone.
        Nothing is sent in the first RC_SETTLE seconds after cd switched:
        the old bridge is still switching off then, and /remote-control only opens a "Remote Control isn't connected" dialog.

        Claude Code leaves bridgeSessionId out of sessions/<pid>.json while it brings a bridge up or reattaches it (at
        startup, on its own reconnects), so a session is typed into only once its bridge has been missing for RC_GRACE
        seconds in a row. A session whose /remote-control answered with the dialog of a connected session is not typed
        into again until its bridge shows up in the registry. A session whose user disconnected Remote Control by hand
        is left alone until it has a bridge again. Each further send to a session whose bridge still does not show up
        waits twice as long as the one before (RC_RETRY, then doubling up to RC_BACKOFF_MAX).
        """
        ctx, sent, escape = self.ctx, [], set()
        if not rc_wanted(cd):
            return sent
        settling = ctx.now() - load_state(ctx)["switched_at"].get(cd, 0) < RC_SETTLE
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in sent:
                continue
            info = session_info(cd, p.pid)
            status = info.get("status")
            if info.get("kind") != "interactive":
                continue
            if info.get("bridgeSessionId"):
                self.rc_gone_since.pop(p.pid, None)
                self.rc_tries.pop(p.pid, None)
                self.rc_connected.discard(p.pid)
                if self.hand_off:
                    self.hand_off.discard(transcript_path(cd, info.get("sessionId")))
                continue
            gone = self.rc_gone_since.setdefault(p.pid, ctx.now())
            if settling or status not in RC_ANYTIME or ctx.now() - gone < RC_GRACE or p.pid in self.rc_connected:
                continue
            if self.hand_off and transcript_path(cd, info.get("sessionId")) in self.hand_off:
                continue
            last, tries = self.rc_sent.get(p.pid), self.rc_tries.get(p.pid, 0)
            if last and ctx.now() - last < min(RC_RETRY * 2 ** max(tries - 1, 0), RC_BACKOFF_MAX):
                continue
            screen = ctx.tmux_capture(p)
            if not prompt_empty(screen):
                continue
            if last and status in PROMPT_FREE and ctx.now() - self.rc_restarted.get(p.pane, 0) >= RESTART_COOLDOWN and parent_is_shell(p.pid) \
                    and any(m in plain(screen) for m in RC_BROKEN):
                self.restart(cd, p, info)
                continue
            if status in PROMPT_FREE:
                escape.add(p.pane)
            self.type_line(p, rc_command(cd, info))
            self.rc_sent[p.pid] = ctx.now()
            self.rc_tries[p.pid] = tries + 1
            sent.append(p.pane)
        if sent:
            ctx.sleep(5)
            for p in ctx.list_procs():
                if p.pane not in sent:
                    continue
                screen = ctx.tmux_capture(p)
                if not rc_dialog_open(screen):
                    continue
                connected = rc_dialog_connected(screen)
                if connected:
                    self.rc_connected.add(p.pid)
                ctx.log("rc_already_on", config_dir=cd, pane=p.pane, dialog="connected" if connected else "not-connected")
                if p.pane in escape:
                    ctx.tmux_send(p, ["Escape"])
            ctx.log("rc_reconnected", config_dir=cd, panes=",".join(sent))
        return sent

    def reconnect_bg(self, cd: str) -> list:
        """Run /remote-control for each background session of cd whose bridge is missing or belongs to another account
        than the one cd is on, typing it into the agent view (`claude agents`) that has the session open.

        A background session has no pane of its own; the daemon keeps its bridge in its job state. The command
        registers a new bridge under the current account at once, busy or not, so no Escape is sent. A session not
        open in any agent view can't be reached. Returns the panes typed into.
        """
        ctx, sent = self.ctx, []
        account = (live_account(cd) or {}).get("accountUuid")
        if not rc_wanted(cd) or not account or ctx.now() - load_state(ctx)["switched_at"].get(cd, 0) < RC_SETTLE:
            return sent
        procs = [p for p in ctx.list_procs() if os.path.normpath(p.config_dir) == os.path.normpath(cd)]
        due = {j["name"]: j for j in bg_jobs(cd, {p.pid for p in procs})
               if j.get("name") and (not j.get("bridgeSessionId") or j.get("bridgeOwnerAccountUuid") != account)}
        for p in procs:
            if not due or not p.pane or p.pane in sent or session_info(cd, p.pid):
                continue
            screen = ctx.tmux_capture(p)
            job = due.get(viewed_session(screen))
            if not job or not prompt_empty(screen):
                continue
            last = self.rc_sent.get(job["jobId"])
            if last and ctx.now() - last < RC_RETRY:
                continue
            title = clean_title(job["name"])
            self.type_line(p, f"{RC_TEXT} {title}" if title else RC_TEXT)
            self.rc_sent[job["jobId"]] = ctx.now()
            sent.append(p.pane)
            del due[job["name"]]
        if sent:
            ctx.log("rc_reconnected_bg", config_dir=cd, panes=",".join(sent))
        return sent

    def stale_bridge(self, cd: str, pid: int, info: dict) -> bool:
        """Whether the session's Remote Control bridge was registered by another account than the one cd is on now.

        Older Claude Code versions keep their bridge across an account switch, so the session stays listed under the
        old account. The owner of a bridge never changes, so a known owner is read once per bridge. An owner no
        transcript entry names is read again every OWNER_RECHECK seconds; until then a bridge the session already had
        when this daemon switched cd counts as stale, since it was registered before the switch, for one disconnect try.
        """
        key = bridge_key(info.get("bridgeSessionId"))
        account = (live_account(cd) or {}).get("accountUuid")
        if not key or not account:
            return False
        if key not in self.owners and self.ctx.now() - self.owner_checked.get(key, -OWNER_RECHECK) >= OWNER_RECHECK:
            self.owner_checked[key] = self.ctx.now()
            owner = bridge_owner(cd, info)
            if owner:
                self.owners[key] = owner
        if key in self.owners:
            return self.owners[key] != account
        return info["bridgeSessionId"] == self.old_bridge.get(pid) and (pid, key) not in self.stale_tried

    def disconnect_stale(self, cd: str) -> list:
        """Disconnect the Remote Control bridge of each free session of cd whose bridge belongs to another account, so
        reconnect registers it again under the current one.

        /remote-control on a connected session opens a menu with the cursor on Continue; the cursor is moved up to
        "Disconnect this session" and that is chosen. When the menu does not show that option it is closed with
        Escape. Returns the panes disconnected.
        """
        ctx, done = self.ctx, []
        if not rc_wanted(cd) or ctx.now() - load_state(ctx)["switched_at"].get(cd, 0) < RC_SETTLE:
            return done
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in done:
                continue
            info = session_info(cd, p.pid)
            if info.get("kind") != "interactive" or info.get("status") not in PROMPT_FREE or not self.stale_bridge(cd, p.pid, info):
                continue
            last = self.rc_sent.get(p.pid)
            if last and ctx.now() - last < RC_RETRY or not prompt_empty(ctx.tmux_capture(p)):
                continue
            self.rc_sent[p.pid] = ctx.now()
            self.stale_tried.add((p.pid, bridge_key(info["bridgeSessionId"])))
            self.type_line(p, RC_TEXT)
            for _ in range(MENU_STEPS):
                ctx.sleep(POLL)
                screen = ctx.tmux_capture(p)
                if RC_DISCONNECT not in plain(screen):
                    continue
                if menu_cursor(screen) == RC_DISCONNECT:
                    self.own_disconnects.add(transcript_path(cd, info.get("sessionId")))
                    ctx.tmux_send(p, ["Enter"])
                    done.append(p.pane)
                    break
                ctx.tmux_send(p, ["Up"])
            else:
                ctx.tmux_send(p, ["Escape"])
                ctx.log("rc_disconnect_failed", config_dir=cd, pane=p.pane)
        if done:
            ctx.log("rc_stale_disconnected", config_dir=cd, panes=",".join(done))
        return done

    def compact(self, cd: str) -> list:
        """Run /compact in each session of cd that was active at the last switch, once its prompt is free.

        A session waits while it is busy, while its new Remote Control bridge is not up yet (so the compaction is the
        first thing the remote session shows), and while its input box holds a draft or a dialog. Returns the panes.
        """
        ctx, asked = self.ctx, []
        due = self.compact_due.get(cd)
        if not due:
            return asked
        alive = {p.pid: p for p in ctx.list_procs() if os.path.normpath(p.config_dir) == os.path.normpath(cd) and p.pane}
        due &= set(alive)
        for pid in sorted(due):
            p, info = alive[pid], session_info(cd, pid)
            if info.get("kind") != "interactive" or info.get("status") not in PROMPT_FREE or not self.rc_ready(cd, pid, info):
                continue
            path = session_transcript(cd, pid)
            line = last_turn_entry(path) if path else None
            hit = entry_error(line) if line else None
            if hit and hit.kind == "limit" or not prompt_empty(ctx.tmux_capture(p)):
                continue
            self.type_line(p, COMPACT_TEXT)
            due.discard(pid)
            asked.append(p.pane)
        if asked:
            ctx.log("compact_asked", config_dir=cd, panes=",".join(asked))
        return asked

    def resume(self, cd: str) -> list:
        """Tell each session wake compacted to continue, once its transcript holds the compaction and its prompt is free.

        A compaction that has not shown up COMPACT_WAIT seconds after /compact was typed is logged and the session is
        told to continue anyway, so a failed /compact does not leave it stopped. Returns the panes told.
        """
        ctx, told = self.ctx, []
        due = self.continue_due.get(cd)
        if not due:
            return told
        alive = {p.pid: p for p in ctx.list_procs() if os.path.normpath(p.config_dir) == os.path.normpath(cd) and p.pane}
        for pid in [pid for pid in due if pid not in alive]:
            del due[pid]
        for pid in sorted(due):
            p, (path, offset, sent_at) = alive[pid], due[pid]
            if session_info(cd, pid).get("status") not in PROMPT_FREE or not prompt_empty(ctx.tmux_capture(p)):
                continue
            with open(path, "rb") as fh:
                fh.seek(offset)
                compacted = COMPACT_MARK in fh.read()
            if not compacted:
                if ctx.now() - sent_at < COMPACT_WAIT:
                    continue
                ctx.log("compact_missing", config_dir=cd, pane=p.pane)
            self.type_line(p, CONTINUE_TEXT)
            del due[pid]
            told.append(p.pane)
        if told:
            ctx.log("continued", config_dir=cd, panes=",".join(told))
        return told

    def dismiss_rc_dialogs(self, cd: str) -> list:
        """Close a Remote Control dialog that is still open on the next tick: the one of a connected session, or the
        "Remote Control isn't connected" one /remote-control opens while the old bridge is still switching off.

        Either leaves the session waiting on it; Escape is its own "continue". The dialog of a connected session marks it
        so reconnect does not type into it again. Returns the panes closed.
        """
        ctx, closed, seen, kinds = self.ctx, [], set(), []
        if not rc_wanted(cd):
            return closed
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in seen:
                continue
            if session_info(cd, p.pid).get("kind") != "interactive":
                continue
            screen = ctx.tmux_capture(p)
            if not rc_dialog_open(screen):
                continue
            connected = rc_dialog_connected(screen)
            if connected:
                self.rc_connected.add(p.pid)
            seen.add(p.pane)
            if p.pane in self.rc_dialogs:
                ctx.tmux_send(p, ["Escape"])
                closed.append(p.pane)
                kinds.append("connected" if connected else "not-connected")
        self.rc_dialogs = seen - set(closed)
        if closed:
            ctx.log("rc_dialog_closed", config_dir=cd, panes=",".join(closed), dialogs=",".join(kinds))
        return closed

    def restart(self, cd: str, p: Proc, info: dict) -> None:
        """Exit an idle session whose /remote-control did not come up and resume it in the same pane, so it reloads its login."""
        ctx = self.ctx
        self.rc_restarted[p.pane] = ctx.now()
        self.type_line(p, "/exit")
        for _ in range(EXIT_WAIT):
            if not Path(f"/proc/{p.pid}").exists():
                break
            ctx.sleep(1)
        else:
            ctx.log("rc_restart_failed", config_dir=cd, pane=p.pane, reason="session did not exit")
            return
        env = "env -u CLAUDE_CONFIG_DIR" if Path(cd) == Path(DEFAULT_CONFIG_DIR) else f"env CLAUDE_CONFIG_DIR={shlex.quote(cd)}"
        cmd = f"{env} claude --resume {shlex.quote(info['sessionId'])}"
        ctx.tmux_send(p, [cmd], literal=True)
        ctx.tmux_send(p, ["Enter"])
        ctx.log("rc_restarted", config_dir=cd, pane=p.pane, session=info["sessionId"])

    def rotate(self, cd: str) -> bool:
        """Switch cd to the next available account and wake its sessions."""
        ctx = self.ctx
        with ctx.lock():
            cur = current_email(cd)
            nxt = next_available(ctx, cur)
            if nxt is None:
                st = load_state(ctx)
                until = [v for k, v in st["limited"].items() if v > ctx.now()]
                key = (cur, min(until) if until else None)
                if self.waiting.get(cd) != key:
                    self.waiting[cd] = key
                    reset = dt.datetime.fromtimestamp(min(until)).isoformat(" ", "seconds") if until else "unknown"
                    ctx.log("all_limited", config_dir=cd, earliest_reset=reset)
                return False
            self.waiting.pop(cd, None)
            switch(ctx, cd, nxt, reason="limit")
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane:
                continue
            info = session_info(cd, p.pid)
            self.old_bridge[p.pid] = info.get("bridgeSessionId")
            self.rc_connected.discard(p.pid)
            self.rc_tries.pop(p.pid, None)
            if info.get("kind") == "interactive" and is_active(info, ctx.now()):
                self.compact_due.setdefault(cd, set()).add(p.pid)
        self.baseline(cd)
        self.wake(cd)
        return True

    def on_limit(self, cd: str, hits: list) -> None:
        ctx = self.ctx
        switched = load_state(ctx)["switched_at"].get(cd, 0)
        fresh = [h for h in hits if h.ts is None or h.ts > switched + INFLIGHT_GRACE]
        if not fresh:
            ctx.log("limit_inflight_ignored", config_dir=cd, count=len(hits))
            return
        cur = current_email(cd)
        until = next((h.resets for h in fresh if h.resets), None)
        fallback = until is None
        until = until or ctx.now() + FALLBACK_HOURS * 3600
        with ctx.lock():
            if cur:
                st = load_state(ctx)
                st["limited"][cur] = until
                save_state(ctx, st)
        ctx.log("limit_hit", email=cur, config_dir=cd, until=dt.datetime.fromtimestamp(until).isoformat(" ", "seconds"),
                fallback="5h" if fallback else None)
        self.rotate(cd)

    def reconcile(self, cd: str) -> None:
        """Route changed live tokens to the vault slot of the account that really owns them, and keep cd on its account.

        Ownership is resolved through the OAuth profile endpoint, never guessed: a refresh of the current account is
        stored in its own slot; a stray write from a session still holding another account is stored in that account's
        slot and the current account's tokens are put back. Nothing is written when the owner cannot be resolved.
        """
        ctx = self.ctx
        with ctx.lock():
            st = load_state(ctx)
            g = st["guards"].get(cd)
            if g and ctx.now() > g["until"]:
                del st["guards"][cd]
                save_state(ctx, st)
                g = None
            expected = g["target"] if g else current_email(cd)
            slot = load_slot(ctx, expected) if expected else None
            if not slot:
                return
            if g and current_email(cd) != expected:
                write_live_account(cd, slot["oauthAccount"])
                ctx.log("guard_account_restored", config_dir=cd, target=expected)
            live = live_oauth(cd)
            if not usable_oauth(live) or live == slot["claudeAiOauth"]:
                return
            owner = ctx.token_email(live["accessToken"])
            if owner is None:
                ctx.log("owner_unresolved", config_dir=cd, expected=expected)
                return
            owner_slot = load_slot(ctx, owner)
            if owner_slot is None:
                ctx.log("owner_not_enrolled", config_dir=cd, owner=owner, expected=expected)
                return
            owner_slot["claudeAiOauth"] = live
            save_slot(ctx, owner_slot)
            st = load_state(ctx)
            if st["dead"].pop(owner, None):
                save_state(ctx, st)
            if owner == expected:
                ctx.log("vault_refreshed", email=owner, config_dir=cd)
                return
            write_live_oauth(cd, slot["claudeAiOauth"])
            ctx.log("guard_restored", config_dir=cd, stray_stored_for=owner, target=expected)

    def drop_dead_live(self, cd: str) -> bool:
        """Mark the live account dead and rotate off it when its login is unusable or a session reported an auth failure."""
        ctx = self.ctx
        with ctx.lock():
            cur = current_email(cd)
            auth = self.auth_hits.pop(cd, [])
            switched = load_state(ctx)["switched_at"].get(cd, 0)
            fresh = [h for h in auth if h.ts is None or h.ts > switched + INFLIGHT_GRACE]
            if not cur or (usable_oauth(live_oauth(cd)) and not fresh):
                return False
            if cd in load_state(ctx)["guards"] and usable_oauth(live_oauth(cd)):
                return False
            _mark_dead(ctx, cur, fresh[0].text[:160] if fresh else "live login has no usable tokens")
        return self.rotate(cd)

    def recover(self, cd: str) -> None:
        """Move a config dir off a limited account once another one is available."""
        ctx = self.ctx
        cur = current_email(cd)
        if cur and is_limited(load_state(ctx), cur, ctx.now()) and next_available(ctx, cur):
            self.rotate(cd)

    def tick(self) -> None:
        cfg = load_config(self.ctx)
        for cd in cfg["config_dirs"]:
            if not Path(cd).is_dir():
                continue
            self.reconcile(cd)
            hits = self.scan_limits(cd)
            if self.drop_dead_live(cd):
                continue
            if hits:
                self.on_limit(cd, hits)
            else:
                self.recover(cd)
            self.wake(cd)
            self.dismiss_rc_dialogs(cd)
            self.disconnect_stale(cd)
            self.reconnect(cd)
            self.reconnect_bg(cd)
            self.compact(cd)
            self.resume(cd)
        renew_due(self.ctx)

    def run(self, interval: float) -> None:
        self.ctx.log("watch_start", interval=interval)
        while True:
            self.tick()
            guarded = bool(load_state(self.ctx)["guards"])
            self.ctx.sleep(min(interval, GUARD_STEP) if guarded else interval)


def fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(" ", "seconds")


def status(ctx: Ctx) -> None:
    """Print current account per config dir and the state of every vault account. Never prints tokens."""
    cfg, st, now = load_config(ctx), load_state(ctx), ctx.now()
    print("config dirs:", file=ctx.out)
    for cd in cfg["config_dirs"]:
        acc = live_account(cd) or {}
        oauth = live_oauth(cd) or {}
        print(f"  {cd}: {acc.get('emailAddress', '(none)')} {oauth.get('subscriptionType') or ''}".rstrip(), file=ctx.out)
    print("accounts:", file=ctx.out)
    slots = {s["email"]: s for s in list_slots(ctx)}
    for email in list(dict.fromkeys(cfg["order"] + list(slots))):
        s = slots.get(email)
        dead = st["dead"].get(email)
        lim = f"DEAD since {fmt_ts(dead['since'])}: {dead['reason']}" if dead else f"limited until {fmt_ts(st['limited'][email])}" if is_limited(st, email, now) else "available"
        exp = (s or {}).get("claudeAiOauth", {}).get("expiresAt")
        exp_s = f"token expires {fmt_ts(exp / 1000)}" if exp else "token expiry unknown"
        print(f"  {email}: {lim}; {exp_s}" + ("" if s else "; NOT ENROLLED"), file=ctx.out)


def default_dir() -> str:
    return os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR") or DEFAULT_CONFIG_DIR)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="claude-rotate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login")
    p.add_argument("--email", default=None)
    p = sub.add_parser("enroll")
    p.add_argument("--config-dir", default=None)
    sub.add_parser("status")
    sub.add_parser("check")
    p = sub.add_parser("use")
    p.add_argument("email")
    p.add_argument("--config-dir", default=None)
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=float, default=15)
    a = ap.parse_args(argv)
    ctx = Ctx()
    if a.cmd == "login":
        login(ctx, a.email)
    elif a.cmd == "enroll":
        enroll(ctx, os.path.normpath(a.config_dir or default_dir()))
    elif a.cmd == "status":
        status(ctx)
    elif a.cmd == "check":
        sys.exit(check(ctx))
    elif a.cmd == "use":
        switch(ctx, os.path.normpath(a.config_dir or default_dir()), a.email)
    elif a.cmd == "watch":
        Rotator(ctx).run(a.interval)


if __name__ == "__main__":
    main()
