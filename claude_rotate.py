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
DEAD_RE = re.compile(r"expired|could not be refreshed|not logged in|please run /login|invalid.{0,20}(token|grant)|unauthorized|authentication", re.I)
AUTH_RE = re.compile(r"please run /login|oauth token has expired|invalid authentication credentials|authentication_error|not logged in|invalid api key", re.I)
WAKE_TEXT = "Account rotated (claude-rotate): continue where you stopped."
RC_TEXT = "/remote-control"
RC_RETRY = 30
RC_BROKEN = ("requires a claude.ai subscription", "Unknown command: /remote-control")
RESTART_COOLDOWN = 1800
EXIT_WAIT = 30
SHELLS = ("bash", "zsh", "sh", "fish", "dash")
IDLE_STOPS = ("end_turn", "stop_sequence")
LIMIT_RE = re.compile(r"hit your (session |usage |weekly )?limit|usage limit reached|rate_limit.{0,200}429|429.{0,200}rate_limit", re.I | re.S)
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)(?:\s*\((?P<tz>[^)]+)\))?",
    re.I,
)


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


def real_run_claude(config_dir: str) -> tuple:
    """Run a one-word prompt with claude in an isolated config dir; return (returncode, stdout). Raises on timeout."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    r = subprocess.run(["claude", "-p", "Reply with the single word ok", "--output-format", "json"], env=env,
                       capture_output=True, text=True, timeout=RENEW_TIMEOUT, cwd=config_dir)
    return r.returncode, r.stdout + ("\n" + r.stderr if r.returncode else "")


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
                 tmux_send: Callable = real_tmux_send, out=None, run_claude: Callable = real_run_claude,
                 token_email: Callable = real_token_email):
        self.root = Path(root)
        self.now = now
        self.sleep = sleep
        self.list_procs = list_procs
        self.tmux_send = tmux_send
        self.run_claude = run_claude
        self.token_email = token_email
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
    """Snapshot the account logged in cd into the vault; return its email."""
    with ctx.lock():
        acc, oauth = live_account(cd), live_oauth(cd)
        if not acc or not acc.get("emailAddress") or not oauth:
            raise SystemExit(f"no logged-in account found in {cd}")
        email = acc["emailAddress"]
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


def parse_reset(text: str, now: float) -> Optional[float]:
    """Parse 'resets 9pm (Europe/Madrid)' into an epoch after now (local time when no zone is given), or None."""
    from zoneinfo import ZoneInfo
    m = RESET_RE.search(text)
    if not m:
        return None
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else None
    except Exception:
        return None
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    base = dt.datetime.fromtimestamp(now, tz)
    if m.group("mon"):
        try:
            month = dt.datetime.strptime(m.group("mon")[:3].title(), "%b").month
            cand = base.replace(month=month, day=int(m.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if cand.timestamp() <= now:
            cand = cand.replace(year=cand.year + 1)
        return cand.timestamp()
    cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += dt.timedelta(days=1)
    return cand.timestamp()


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
            if DEAD_RE.search(text):
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
    text: str
    ts: Optional[float]
    uuid: Optional[str]


def entry_ts(e: dict) -> Optional[float]:
    """Epoch of a transcript entry's ISO timestamp, or None."""
    try:
        return dt.datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None


def entry_limit(line: str, pattern=None) -> Optional[Hit]:
    """Return a Hit if this transcript line is Claude Code's own error matching pattern (default: usage limit), else None."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("type") != "assistant" or not e.get("isApiErrorMessage"):
        return None
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    if msg.get("model") != "<synthetic>":
        return None
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = str(content or "")
    return Hit(text, entry_ts(e), e.get("uuid")) if (pattern or LIMIT_RE).search(text) else None


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


def session_transcript(cd: str, pid: int) -> Optional[str]:
    """The transcript of the session a claude process is running, from <cd>/sessions/<pid>.json."""
    sid = session_info(cd, pid).get("sessionId")
    if not sid:
        return None
    found = glob.glob(os.path.join(cd, "projects", "*", f"{sid}.jsonl"))
    return found[0] if found else None


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
                line = raw.decode(errors="replace")
                hit = entry_limit(line)
                if hit:
                    hits.append(hit)
                auth = entry_limit(line, AUTH_RE)
                if auth:
                    self.auth_hits.setdefault(cd, []).append(auth)
        return hits

    def wake(self, cd: str) -> list:
        """Nudge each tmux session of cd whose last turn is a limit error from before the last switch."""
        ctx, woken = self.ctx, []
        st = load_state(ctx)
        cur = current_email(cd)
        if not cur or is_limited(st, cur, ctx.now()):
            return woken
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in woken:
                continue
            path = session_transcript(cd, p.pid)
            line = last_turn_entry(path) if path else None
            hit = (entry_limit(line) or entry_limit(line, AUTH_RE)) if line else None
            if not hit or (hit.uuid or path) in self.woken:
                continue
            ctx.tmux_send(p, ["Escape"])
            ctx.tmux_send(p, [WAKE_TEXT], literal=True)
            ctx.tmux_send(p, ["Enter"])
            self.woken.add(hit.uuid or path)
            woken.append(p.pane)
        if woken:
            ctx.log("woke", config_dir=cd, panes=",".join(woken))
        return woken

    def reconnect(self, cd: str) -> list:
        """Run /remote-control in each idle interactive tmux session of cd that has no Remote Control bridge."""
        ctx, sent = self.ctx, []
        if not rc_wanted(cd):
            return sent
        for p in ctx.list_procs():
            if os.path.normpath(p.config_dir) != os.path.normpath(cd) or not p.pane or p.pane in sent:
                continue
            info = session_info(cd, p.pid)
            if info.get("kind") != "interactive" or info.get("bridgeSessionId") or info.get("status") != "idle":
                continue
            last = self.rc_sent.get(p.pid)
            if last and ctx.now() - last < RC_RETRY:
                continue
            path = session_transcript(cd, p.pid)
            line = last_turn_entry(path) if path else None
            if line and entry_limit(line):
                continue
            if last and ctx.now() - self.rc_restarted.get(p.pane, 0) >= RESTART_COOLDOWN and parent_is_shell(p.pid) \
                    and any(m in _tmux(p, "capture-pane", "-p", "-t", p.pane) for m in RC_BROKEN):
                self.restart(cd, p, info)
                continue
            ctx.tmux_send(p, ["Escape"])
            ctx.tmux_send(p, [RC_TEXT], literal=True)
            ctx.tmux_send(p, ["Enter"])
            self.rc_sent[p.pid] = ctx.now()
            sent.append(p.pane)
        if sent:
            ctx.sleep(5)
            for p in ctx.list_procs():
                if p.pane in sent:
                    ctx.tmux_send(p, ["Escape"])
            ctx.log("rc_reconnected", config_dir=cd, panes=",".join(sent))
        return sent

    def restart(self, cd: str, p: Proc, info: dict) -> None:
        """Exit an idle session whose /remote-control did not come up and resume it in the same pane, so it reloads its login."""
        ctx = self.ctx
        self.rc_restarted[p.pane] = ctx.now()
        ctx.tmux_send(p, ["Escape"])
        ctx.tmux_send(p, ["/exit"], literal=True)
        ctx.tmux_send(p, ["Enter"])
        for _ in range(EXIT_WAIT):
            if not Path(f"/proc/{p.pid}").exists():
                break
            ctx.sleep(1)
        else:
            ctx.log("rc_restart_failed", config_dir=cd, pane=p.pane, reason="session did not exit")
            return
        env = "env -u CLAUDE_CONFIG_DIR" if Path(cd) == Path(DEFAULT_CONFIG_DIR) else f"env CLAUDE_CONFIG_DIR={shlex.quote(cd)}"
        cmd = f"{env} claude --resume {shlex.quote(info['sessionId'])}" + (f" --name {shlex.quote(info['name'])}" if info.get("name") else "")
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
        texts = [h.text for h in fresh]
        cur = current_email(cd)
        until = None
        for t in texts:
            until = parse_reset(t, ctx.now())
            if until:
                break
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
            self.reconnect(cd)
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
    if a.cmd == "enroll":
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
