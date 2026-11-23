---
name: upgrade-agents
description: Restart every running Claude Code session (in herdr or tmux) onto the installed Claude Code version now, gracefully - busy or looping sessions are asked to wind down first, Remote Control is turned off and back on, and loops are typed back. Use when the user says the agents need an upgrade or restart, "upgrade the agents", "restart the sessions", or after a Claude Code update. Args - "dry" to only report.
---

# upgrade-agents

The work is done by `agent_upgrade.py` from this plugin. Never type into other agents' panes yourself: the script checks
for drafts, the user's keybindings and the slash-menu highlight before it types anything.

The script is at `~/.local/state/agent-upgrade/current/agent_upgrade.py` (or under `$AGENT_UPGRADE_STATE/current/`),
a link the plugin's SessionStart hook keeps pointing at the installed version.

## Steps

1. `dry` in the args: run `python3 <script> once --dry-run` and report what it would do. Stop.
2. Run `python3 <script> once`. It never restarts this session (the one that started it), so this turn is not cut off.
   A pass that finds another pass running says "skipped": wait a minute and run it again.
3. A session asked to wind down restarts when it reaches a safe point and answers `RESTART-READY`, which can take as
   long as its running work. If the user wants to wait, follow with a background wait (bounded, not repeated sleeps)
   for `RESTART-READY` in that session's transcript, then run step 2 again. Otherwise report it as pending: a scheduled
   pass (`agent_upgrade.py setup ...`) finishes the job.
4. Check: `python3 <script> once --dry-run` reports 0 to restart, and `<state>/relaunch-*` is empty.

## Report

One row per session: folder, old -> new version, what happened (restarted, asked to wind down, loop relaunched), and
anything left (busy, draft in its box, waiting for RESTART-READY, failed: with the script's line). Say that this session
restarts on a later pass, and how many sessions ran outside herdr and tmux (those can't be reached).
