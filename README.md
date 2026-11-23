# agent-upgrade

Claude Code updates itself, but every session already running keeps the old version until it restarts. With a dozen
agents in terminal panes, some looping and some on Remote Control, restarting them by hand is a chore, and doing it
blindly loses work. agent-upgrade restarts them gracefully:

- It waits until a session is idle with an empty input box. It never types over a draft or into Claude's agent view.
- A busy or looping session is asked to wind down at its next safe point: finish or stop its shells, subagents and
  monitors, stop its loop, and answer `RESTART-READY` with the `/loop` line to bring back. It is never interrupted
  (no Esc, no ctrl+d).
- Remote Control is turned off first (`/restart` refuses under it) and back on after.
- `/restart` restarts the session in place, on the same session, with the same flags. The session's `/loop` is typed
  back.
- A session that can't be restarted cleanly is reported and left running.

It drives the terminal the session runs in: [herdr](https://herdr.dev) or tmux.

## Install

In Claude Code:

```
/plugin marketplace add Benjmhart/agent-upgrade
/plugin install agent-upgrade@agent-upgrade
```

Restart your sessions once so they load the plugin's hooks. The hooks record each session's state (idle or working),
pane and `/loop` command in `~/.local/state/agent-upgrade/sessions/`, so the upgrader doesn't have to guess from the
screen. The hooks also keep `~/.local/state/agent-upgrade/current` pointing at the installed plugin.

`/upgrade-agents` (or `/upgrade-agents dry`) then upgrades every session now, from inside Claude. The session you run
it from restarts on a later pass, so its turn isn't cut off.

## Run it on a schedule

A Claude plugin can't restart the process it runs in, so the unattended part runs outside Claude. Pick one:

```sh
S=~/.local/state/agent-upgrade/current/agent_upgrade.py
python3 $S setup systemd   # Linux: a user timer, every 10 minutes
python3 $S setup herdr     # herdr: the plugin's startup entry runs the loop while the server runs
python3 $S setup launchd   # macOS: a launch agent, every 10 minutes
```

Setup writes the unit with the stable `current` path, so plugin updates take effect without running setup again.

Anything else works too: cron, another multiplexer's hook, or by hand. Run `python3 $S once` every few minutes. Passes
are safe to overlap, because only one runs at a time (flock).

Options, from the environment at setup time (setup saves them in `~/.config/agent-upgrade/env`, which every run
reads, so herdr's startup entry gets them too; the environment wins):
- `AGENT_UPGRADE_STATE`: the state folder.
- `AGENT_UPGRADE_TELL`: the folder of an agent to tell after a pass that restarted something, for an agent that keeps
  other agents' loops.

## Lost agents (herdr)

herdr forgets an agent the moment it exits, so a crash or a failed restart leaves a plain shell that looks like every
other pane. With `setup herdr`, the plugin remembers which agent each terminal ran (session id and flags) and lists
the terminals that no longer run it. Enter types the resume command into that pane.

```toml
[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "benjmhart.agent-upgrade.lost"
description = "lost agents"
```

`python3 $S lost` prints them (exit 1 when there are any). Resume forms: `claude --resume ID`, `pi --session ID`,
`codex resume ID`.

## Limits

- **Linux.** The pass reads `/proc`. The launchd setup is there, but the pass does nothing useful on macOS yet.
- **Claude Code's native install** (`~/.local/share/claude/versions/`). An npm install isn't detected.
- **tmux**
  - A session needs the plugin's hooks for the upgrader to know whether it's idle; without them it is left alone.
  - The submit key must reach Claude through tmux. Enter and alt+enter do. ctrl+enter didn't in testing,
    even with tmux's extended keys on.
- **Other agents.** pi, codex and others are on the list, but only lost-agents resume knows them so far.

Standard library only. MIT.
