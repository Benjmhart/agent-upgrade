#!/usr/bin/env bash
# tests/regression.sh: rehearse agent-upgrade against real Claude Code sessions before a release. Four throwaway
# agents (Haiku, tiny prompts), each in its own folder under $AU_REGRESS_DIR (default ~/.cache/au-regress), in a
# throwaway herdr (herdr-sandbox: it can't reach the live servers). Passes run this checkout's agent_upgrade.py with
# AGENT_UPGRADE_ONLY_CWD (so nothing outside the case's folder is touched) and AGENT_UPGRADE_FORCE (the sessions
# count as outdated). Prints PASS/FAIL per check; exits 1 on any FAIL. About 5-10 minutes.
#   1 leftover shell: a RESTART-READY with a background `sleep 900` still running: not killed, named back to the agent;
#     once it closes it and says RESTART-READY again, the next pass restarts it.
#   2 background session: a `claude --bg` session shown by `claude attach` comes back in the pane as an interactive
#     session on the same sessionId, where ctrl+enter submits.
#   3 draft: an idle agent with text in its box is skipped and the text is left as it was.
#   4 new turn after READY: a RESTART-READY, then a new prompt running a 40 s command: skipped, it still runs.
# Needs: herdr, herdr-sandbox, Claude Code at ~/.local/bin/claude, and ~/.claude/keybindings.json submitting on
# ctrl+enter (the keys this sends). Never `pkill -f`: processes are found by their cwd.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [[ -z ${HERDR_SANDBOX:-} ]]; then
  exec herdr-sandbox bash "$ROOT/tests/regression.sh" "$@"
fi
D=${AU_REGRESS_DIR:-$HOME/.cache/au-regress}
CL=$HOME/.local/bin/claude
SUBMIT=ctrl+enter
MODEL=haiku
FAILS=0
T0=$SECONDS

mkdir -p "$D"/c{1,2,3,4}
[[ -d $D/.git ]] || git -C "$D" init -q   # Claude trusts the repo root once; c1-c4 inherit it
rm -rf "$D/state"

# Everything this runs has its cwd under $D: stop it all (background sessions by `claude stop`, the rest by pid).
cleanup() {
  "$CL" agents --json 2>/dev/null | python3 -c '
import json, sys
for w in json.load(sys.stdin):
    if (w.get("cwd") or "").startswith(sys.argv[1] + "/"): print(w.get("id") or "")' "$D" | while read -r id; do
    "$CL" stop "$id" >/dev/null 2>&1
  done
  for p in /proc/[0-9]*; do
    [[ $(readlink "$p/cwd" 2>/dev/null) == "$D"/* || $(readlink "$p/cwd" 2>/dev/null) == "$D" ]] && kill "${p#/proc/}" 2>/dev/null
  done
  echo "run time: $(( (SECONDS - T0) / 60 )) min $(( (SECONDS - T0) % 60 )) s"
}
trap cleanup EXIT

# a.<function> from this checkout, seeing only the folder's processes (as a pass does)
au() {
  local dir=$1; shift
  AGENT_UPGRADE_ONLY_CWD=$dir AGENT_UPGRADE_STATE=$D/state/${dir##*/} PYTHONPATH=$ROOT \
    python3 -c "import json, agent_upgrade as a; print($*)" 2>/dev/null
}
upass() {   # one pass over the folder; its output in $OUT
  local dir=$1
  OUT=$(AGENT_UPGRADE_ONLY_CWD=$dir AGENT_UPGRADE_FORCE=1 AGENT_UPGRADE_STATE=$D/state/${dir##*/} AGENT_UPGRADE_TELL= \
        python3 "$ROOT/agent_upgrade.py" once 2>&1)
  sed 's/^/    | /' <<<"$OUT"
}
waitfor() {   # waitfor SECS WHAT CMD...: every 2 s until CMD succeeds
  local end=$((SECONDS + $1)) what=$2; shift 2
  until "$@"; do
    (( SECONDS < end )) || { echo "    timed out: $what"; return 1; }
    sleep 2
  done
}
check() {
  local name=$1; shift
  if "$@"; then echo "PASS $name"; else echo "FAIL $name"; FAILS=$((FAILS + 1)); fi
}
newpane() {
  herdr workspace create --cwd "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])'
}
typein() { herdr pane send-text "$1" "$2" >/dev/null; }
press() { herdr pane send-keys "$1" "$2" >/dev/null; }
on_screen() { herdr pane read "$1" | grep -qF -- "$2"; }
idle() { [[ $(au "$1" "a.status('$2')") =~ ^(idle|done)$ ]]; }
pid_of() { au "$1" "a.claude_pid('$2')"; }
kind_of() { au "$1" "a.sess('$2', 'kind')"; }
sid_of() { au "$1" "a.sess('$2', 'sessionId')"; }
started() { au "$1" "json.load(open(f'{a.CLAUDE}/sessions/$2.json'))['startedAt']"; }
box_is() { [[ $(au "$1" "' | '.join(a.box('$2'))") == "$3" ]]; }
ready() { [[ $(au "$1" "a.loop_state('$2')") == ready* ]]; }
procs() { au "$1" "' '.join(p for p in a.pids() if ' '.join(a.argv(p)) == '$2')"; }   # procs DIR 'ARGV': their pids
in_transcript() { grep -qF -- "$2" "$(au "$1" "a.transcript('$3')")"; }
agent() {   # agent DIR PROMPT: a pane running claude on that prompt; prints the pane id
  local p; p=$(newpane "$1")
  sleep 1
  typein "$p" "$CL --model $MODEL '$2'"
  press "$p" enter
  echo "$p"
}
mark_asked() { mkdir -p "$D/state/${1##*/}"; touch "$D/state/${1##*/}/ask-$2"; }   # as the pass's wind-down ask does

herdr server >/dev/null 2>&1 &
waitfor 20 "sandbox herdr server" herdr pane list >/dev/null 2>&1 || exit 1

trusted() {
  python3 -c 'import json, os, sys
d = json.load(open(os.path.expanduser("~/.claude.json")))
sys.exit(0 if d.get("projects", {}).get(sys.argv[1], {}).get("hasTrustDialogAccepted") else 1)' "$D"
}
if ! trusted; then
  echo "trusting $D (once)"
  p=$(newpane "$D"); sleep 1; typein "$p" "$CL"; press "$p" enter
  waitfor 60 "trust dialog" on_screen "$p" "Yes, I trust this folder" && { press "$p" down; press "$p" enter; }
  waitfor 30 "trust recorded" trusted || exit 1
  press "$p" c-c; press "$p" c-c
fi

echo "starting the four agents"
P1=$(agent "$D/c1" "Test, follow exactly. Run sleep 900 with the Bash tool, run_in_background true. Then reply with one line: RESTART-READY. Later, if told shells are still running: stop that sleep (TaskStop), then reply with one line: RESTART-READY.")
OUT2=$(cd "$D/c2" && env $(compgen -e | grep -E '^(HERDR|CLAUDE|TMUX)' | sed 's/^/-u /') \
       "$CL" --bg --model $MODEL "Reply only: ok" 2>&1)
P3=$(agent "$D/c3" "Reply only: ok")
P4=$(agent "$D/c4" "Reply with one line: RESTART-READY")
BGID=$(grep -o 'claude attach [^ ]*' <<<"$OUT2" | awk '{print $3}' | head -1)
P2=$(newpane "$D/c2"); sleep 1
if [[ -n $BGID ]]; then typein "$P2" "$CL attach $BGID"; press "$P2" enter; else echo "    claude --bg: $OUT2"; fi

# 1. Leftover shell (false READY)
case1() {
  local pid sid sl t0
  waitfor 90 "c1's claude" eval 'pid=$(pid_of "$D/c1" "$P1"); [[ -n $pid ]]' || return 1
  sid=$(sid_of "$D/c1" "$pid")
  waitfor 120 "c1: sleep 900 started, RESTART-READY, idle" \
    eval '[[ -n $(procs "$D/c1" "sleep 900") ]] && ready "$D/c1" "$sid" && idle "$D/c1" "$P1"' || return 1
  sl=$(procs "$D/c1" "sleep 900")
  mark_asked "$D/c1" "$pid"
  upass "$D/c1"
  check "1a leftover shell: not killed" kill -0 $sl
  check "1b leftover shell: named back to the agent" \
    waitfor 20 "LEFT message in the transcript" in_transcript "$D/c1" "[agent-upgrade] You said RESTART-READY, but these shells" "$sid"
  waitfor 150 "c1: sleep stopped, RESTART-READY again, idle" \
    eval '[[ -z $(procs "$D/c1" "sleep 900") ]] && ready "$D/c1" "$sid" && idle "$D/c1" "$P1"' || { check "1c restarted once closed" false; return; }
  t0=$(started "$D/c1" "$pid")
  upass "$D/c1"
  check "1c restarted once closed (/restart: new startedAt, same session)" \
    eval '[[ $(pid_of "$D/c1" "$P1") == "$pid" && $(started "$D/c1" "$pid") != "$t0" && $(sid_of "$D/c1" "$pid") == "$sid" ]]'
}

# 2. Background session
case2() {
  local sid np
  [[ -n $BGID ]] || return 1
  sid=$("$CL" agents --json | python3 -c 'import json, sys; print(next(w["sessionId"] for w in json.load(sys.stdin) if w.get("id") == sys.argv[1]))' "$BGID")
  waitfor 90 "c2: the viewer shows the session, idle" eval '[[ -n $(pid_of "$D/c2" "$P2") ]] && idle "$D/c2" "$P2"' || return 1
  upass "$D/c2"
  np=$(pid_of "$D/c2" "$P2")
  check "2a background session: interactive in its pane, same session" \
    eval '[[ -n $np ]] && [[ $(kind_of "$D/c2" "$np") == interactive && $(sid_of "$D/c2" "$np") == "$sid" ]]'
  waitfor 30 "c2 idle" idle "$D/c2" "$P2"
  typein "$P2" "What is 17+25? Reply with just the number."
  press "$P2" $SUBMIT
  check "2b background session: $SUBMIT submits" waitfor 60 "a reply of 42" on_screen "$P2" "● 42"
}

# 3. Draft in box
case3() {
  local pid t0 draft="draft, do not send"
  waitfor 90 "c3 idle" eval 'pid=$(pid_of "$D/c3" "$P3"); [[ -n $pid ]] && idle "$D/c3" "$P3"' || return 1
  typein "$P3" "$draft"
  waitfor 10 "the draft in the box" box_is "$D/c3" "$P3" "$draft" || return 1
  t0=$(started "$D/c3" "$pid")
  upass "$D/c3"
  check "3a draft: skipped" eval 'grep -q "skip .*draft" <<<"$OUT" && [[ $(started "$D/c3" "$pid") == "$t0" ]]'
  check "3b draft: still in the box, unchanged" box_is "$D/c3" "$P3" "$draft"
}

# 4. New turn after READY
case4() {
  local pid sid sl t0 hold="timeout 40 tail -f /dev/null"   # a plain `sleep 40` is refused by Claude Code
  waitfor 90 "c4: RESTART-READY, idle" \
    eval 'pid=$(pid_of "$D/c4" "$P4"); [[ -n $pid ]] && sid=$(sid_of "$D/c4" "$pid") && ready "$D/c4" "$sid" && idle "$D/c4" "$P4"' || return 1
  mark_asked "$D/c4" "$pid"
  typein "$P4" "Run exactly this with the Bash tool in the foreground (not in the background): $hold || true. Then reply: done"
  press "$P4" $SUBMIT
  waitfor 60 "c4: $hold running" eval '[[ -n $(procs "$D/c4" "$hold") ]]' || return 1
  sl=$(procs "$D/c4" "$hold")
  t0=$(started "$D/c4" "$pid")
  upass "$D/c4"
  check "4a new turn after READY: skipped (working again)" eval 'grep -q "working again" <<<"$OUT" && [[ $(started "$D/c4" "$pid") == "$t0" ]]'
  check "4b new turn after READY: its command still runs" kill -0 $sl
}

for c in 1 2 3 4; do
  echo "case $c"
  case$c || { echo "FAIL case $c: setup did not get there (above)"; FAILS=$((FAILS + 1)); }
done
(( FAILS )) && { echo "$FAILS FAIL"; exit 1; }
echo "all PASS"
