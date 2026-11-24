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
- A session that runs in Claude's background (agent view, `claude --bg`) and is shown in a pane gets the same
  wind-down, then `claude stop` and `claude --resume` in that pane, with its options and name. It then runs in the
  pane's own terminal on the new version, and takes `/restart` next time. Claude's own daemon moves only idle
  background sessions to a new version. A `claude attach` viewer passes keys through the daemon's terminal, where
  ctrl+enter doesn't arrive.
- Nothing is ever killed or forced. A busy or looping session is asked to save its work and stop what it started
  as gracefully as it can. If it says `RESTART-READY` and still has shells running, or `/restart` is refused for
  background work, it is told what is left and asked to close it gracefully.
- Each upgrade round ends with one report: every session that was outdated, upgraded or not and why. It is written
  to `<state>/rounds/<version>.txt`, logged, and shown as a herdr notification. It is sent when none is left waiting,
  or when the wait limit has passed; the sessions still waiting are flagged then.
- A session that hasn't said `RESTART-READY` within the wait limit (default 60 min) is reported once and flagged
  for you (herdr-queues' 🔖 revisit flag, when that plugin is installed). It keeps running on its old version.

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
- `AGENT_UPGRADE_MAX_WAIT`: minutes to wait for a session's `RESTART-READY` before it is reported (default 60).
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

## Regression

Every release passes `tests/regression.sh` first. It starts four throwaway Claude sessions (Haiku, one-line prompts)
in a throwaway herdr (`herdr-sandbox`, which can't reach a live herdr server), each in its own folder under
`~/.cache/au-regress` (`AU_REGRESS_DIR`), and runs this checkout's pass on each with `AGENT_UPGRADE_ONLY_CWD` and
`AGENT_UPGRADE_FORCE`:

1. a `RESTART-READY` with a background shell left running: the shell is not killed, it is named back to the agent,
   and once the agent closes it and says `RESTART-READY` again, the next pass restarts it;
2. a `claude --bg` session shown with `claude attach`: it comes back in the pane as an interactive session on the
   same session id, and ctrl+enter submits there;
3. an idle session with a draft in its box: skipped, the draft untouched;
4. a `RESTART-READY`, then a new turn running a command: skipped ("working again"), the command still running.

It prints PASS/FAIL per check and exits 1 on any FAIL, in a minute or two. On the first run Claude asks to trust the
folder; the script accepts. It needs herdr, `herdr-sandbox`, Claude Code's native install and a ctrl+enter submit
binding.

## Limits

- **Linux.** The pass reads `/proc`. The launchd setup is there, but the pass does nothing useful on macOS yet.
- **Claude Code's native install** (`~/.local/share/claude/versions/`). An npm install isn't detected.
- **tmux**
  - A session needs the plugin's hooks for the upgrader to know whether it's idle; without them it is left alone.
  - The submit key must reach Claude through tmux. Enter and alt+enter do. ctrl+enter didn't in testing,
    even with tmux's extended keys on.
- **Other agents.** pi, codex and others are on the list, but only lost-agents resume knows them so far.

Standard library only. MIT.
