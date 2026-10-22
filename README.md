# claude-rotate

Rotates the Claude Code login across several of your Claude accounts. When a session hits a usage limit, the current account is switched out and the stopped sessions pick up where they left off.

It is a single Python file with no dependencies. It runs as a background watcher on Linux, and it can wake sessions that run inside tmux.

> **Read your plan's terms first.** This tool only switches between accounts that you own and have logged into yourself. Whether using several subscriptions this way is allowed is up to Anthropic's terms for your plan. You are responsible for following them.

## What it does

- **Watches transcripts.** It tails `<config-dir>/projects/**/*.jsonl` and looks for the synthetic error message that Claude Code writes when a usage limit is hit. It parses the reset time from that message (for example `resets 9pm (Europe/Madrid)`). If none can be read, it marks the account limited for 5 hours.
- **Switches accounts.** It moves to the next enrolled account that is not limited and not dead, in round-robin order. Before switching, it saves the outgoing account's current tokens back to the vault.
- **Wakes stopped sessions.** In each tmux pane whose last turn ended in the limit or an auth error, it sends `Account rotated (claude-rotate): continue where you stopped.`
- **Keeps tokens in the right slot.** Refresh tokens rotate, and a session that still holds the old account can write that account's refreshed tokens into the live credentials. claude-rotate looks up who owns each changed token through the OAuth profile endpoint and stores it in that account's slot. It never guesses the owner. If the token belongs to another account, the current account's tokens are put back.
- **Renews idle accounts.** Every 6 hours, each enrolled account that is not in use gets a one-word `claude -p` call. The call runs in a throwaway config dir and refreshes that account's tokens, so a long-idle account doesn't expire. This uses a tiny amount of each account's quota.
- **Optional: Remote Control.** If a config dir sets `"remoteControlAtStartup": true` in its `settings.json`, claude-rotate runs `/remote-control` in idle interactive tmux sessions that lost their bridge. If the bridge still doesn't come up, it restarts the session with `claude --resume`, which reloads the login.

## Install

```bash
uv tool install git+https://github.com/DBC-Juanma/claude-rotate
# or: pipx install git+https://github.com/DBC-Juanma/claude-rotate
```

Requires Python 3.10+, Linux (it reads `/proc`), `pgrep`, and `tmux` for waking sessions.

## Enroll your accounts

Enroll each account by logging in **from a fresh, temporary config dir** and snapshotting it:

```bash
tmp=$(mktemp -d)
CLAUDE_CONFIG_DIR=$tmp claude          # run /login with the account, then quit
claude-rotate enroll --config-dir "$tmp"
rm -rf "$tmp"
```

Repeat this for every account. The account you use day to day can be enrolled straight from your normal dir with `claude-rotate enroll`.

**Never enroll two slots from config dirs that share a login.** Two copies of the same refresh-token family revoke each other the first time either one refreshes, and both end up logged out.

## Run it

```bash
claude-rotate status          # account per config dir, state of every account (never prints tokens)
claude-rotate check           # renew every idle account now; exit 1 if any is dead or failing
claude-rotate use <email>     # switch a config dir to an account by hand
claude-rotate watch           # the watcher loop (default interval 15s)
```

As a systemd user service:

```bash
cp contrib/claude-rotate.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-rotate
journalctl --user -u claude-rotate -f
```

Adjust `ExecStart` in the unit to point at wherever `claude-rotate` is installed (`uv tool dir --bin` shows the location).

## Configuration and state

Everything lives in `$XDG_CONFIG_HOME/claude-rotate` (default `~/.config/claude-rotate`). The directory is mode 0700 and every file is 0600.

| File | Contents |
|---|---|
| `config.json` | `config_dirs`: the Claude Code config dirs to watch (default `["~/.claude"]`); `order`: the rotation order of enrolled emails |
| `accounts/<email>.json` | the vault: `claudeAiOauth` tokens and `oauthAccount` for each account |
| `state.json` | limited-until times, dead accounts, last renewal, short post-switch guards |

To watch more than one config dir (for example a second `CLAUDE_CONFIG_DIR` you run in parallel), add it to `config_dirs`.

An account is marked **dead** when its tokens are rejected. It is never picked again until you log in with it again and re-run `enroll`.

## Logs

Each event is one line on stdout: `switch`, `limit_hit`, `woke`, `renewed`, `vault_refreshed`, `guard_restored`, `account_dead`, `all_limited`, `rc_reconnected` and others.

## Development

```bash
uv run pytest
```

## License

MIT
