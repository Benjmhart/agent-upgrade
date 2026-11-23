"""agent-upgrade: restart coding-agent sessions onto the installed version, gracefully.

When Claude Code updates itself, every running session keeps the old version until it restarts. This finds those
sessions, waits until each is idle with an empty input box (a busy or looping one is asked to wind down first), turns
Remote Control off (/restart refuses under it), runs /restart, turns Remote Control back on and types the session's
/loop back. It drives the terminal the session runs in: herdr or tmux.

  agent_upgrade.py once [--dry-run]   one pass (safe to overlap: one pass at a time, by flock)
  agent_upgrade.py loop               a pass every 10 min, forever (herdr's startup entry)
  agent_upgrade.py setup herdr|systemd|launchd   run it on a schedule
  agent_upgrade.py lost               sessions whose agent exited and was not resumed (exit 1 when any)
  agent_upgrade.py open               the lost-agents popup (herdr)
  agent_upgrade.py forget PANE        drop one from the lost list
  agent_upgrade.py remember           record herdr's running agents (herdr runs it on agent events)

Environment: AGENT_UPGRADE_STATE (state folder, default ~/.local/state/agent-upgrade), AGENT_UPGRADE_TELL (the folder
of an agent to tell after a pass that restarted anything, so it can bring back loops of its own). `setup` saves them in
~/.config/agent-upgrade/env, which every run reads (the environment wins).
Standard library only. The upgrade pass reads /proc: Linux.
"""
import glob, json, os, re, shlex, shutil, socket, subprocess, sys, time
from datetime import datetime

# Settings saved by `setup`, for schedulers that start this with an environment of their own (herdr's startup entry
# runs with the herdr server's). The environment wins over the file.
SETTINGS = ("AGENT_UPGRADE_STATE", "AGENT_UPGRADE_TELL", "HERDR_SOCKET_PATH")
CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "agent-upgrade", "env")
try:
    for k, _, v in (l.partition("=") for l in open(CONF).read().splitlines()):
        if k in SETTINGS and v:
            os.environ.setdefault(k, v)
except OSError:
    pass

PLUGIN = "benjmhart.agent-upgrade"
WIN = os.name == "nt"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
STATE_ROOT = os.environ["LOCALAPPDATA"] if WIN else (os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(STATE_ROOT, "herdr/plugins", PLUGIN)
MEMORY = os.path.join(STATE_DIR, "agents.json")

# How each agent resumes a session by id. An agent not listed is still reported, with its session id; nothing is typed.
# ponytail: codex's form is from its docs, not tried here (no codex on the test machines); gemini's --resume takes an
# index, not an id, so it is left out.
RESUME = {"claude": "claude --resume {id}", "pi": "pi --session {id}", "codex": "codex resume {id}"}
# Flags not carried into the resume command: the old session choice.
DROP = {"claude": {"-c", "--continue", "-r", "--resume", "--session-id"}, "pi": {"-c", "--continue", "-r", "--resume", "--session", "--session-id", "--fork"}}
TAKES_VALUE = {"-r", "--resume", "--session-id", "--session", "--fork"}


def run(*args, timeout=5):
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"herdr {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def herdr(*args, timeout=5):
    return json.loads(run(*args, timeout=timeout))["result"]


def load():
    try:
        with open(MEMORY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(mem):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = MEMORY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mem, f, indent=1, sort_keys=True)
    os.replace(tmp, MEMORY)


def flags(agent, pane):
    """The agent's own flags (permission mode, model ...) from its command line, minus its old session choice. Linux
    only (/proc); elsewhere the resume runs with none."""
    for e in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not e.isdigit():
            continue
        try:
            env = open(f"/proc/{e}/environ", "rb").read().split(b"\0")
            if f"HERDR_PANE_ID={pane}".encode() not in env:
                continue
            argv = [a.decode() for a in open(f"/proc/{e}/cmdline", "rb").read().split(b"\0") if a]
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == agent:
            return clean(agent, argv[1:])
    return []


def clean(agent, args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.split("=")[0] in DROP.get(agent, ()):
            skip = a in TAKES_VALUE
        else:
            out.append(a)
    return out


def resume_cmd(rec):
    t = RESUME.get(rec.get("agent"))
    if not t or not rec.get("session"):
        return None
    return " ".join([t.format(id=rec["session"]), *map(shlex.quote, rec.get("flags") or [])])


def remember():
    """Record every running agent by its terminal (pane ids change across a herdr restart; terminal ids do not)."""
    mem = load()
    for a in herdr("agent", "list")["agents"]:
        t, s = a.get("terminal_id"), (a.get("agent_session") or {}).get("value")
        # Only agents with a session: herdr-queues' heading entries are session-less stand-in agents that herdr drops
        # while their panes stay. ponytail: an agent that dies before herdr learns its session is not remembered.
        if not t or not s:
            continue
        old = mem.get(t, {})
        rec = {"agent": a.get("agent"), "session": s, "pane": a["pane_id"], "cwd": a.get("cwd"),
               "workspace": a.get("workspace_id"), "seen": time.time()}
        rec["flags"] = flags(rec["agent"], a["pane_id"]) if old.get("session") != rec["session"] or "flags" not in old \
            else old["flags"]
        mem[t] = rec
    save(mem)
    return mem


def lost(mem=None, panes=None, agents=None):
    """[(terminal id, record, pane id now)] for terminals that ran an agent and still exist without it. Terminals that
    are gone (pane closed) drop out of the memory."""
    mem = load() if mem is None else mem
    panes = herdr("pane", "list")["panes"] if panes is None else panes
    agents = herdr("agent", "list")["agents"] if agents is None else agents
    alive = {a.get("terminal_id") for a in agents}
    by_term = {p.get("terminal_id"): p for p in panes}
    out = [(t, r, by_term[t]["pane_id"]) for t, r in mem.items() if t in by_term and t not in alive]
    for t in [t for t in mem if t not in by_term]:
        del mem[t]
    return out


def workspaces():
    try:
        return {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in herdr("workspace", "list")["workspaces"]}
    except (RuntimeError, OSError, ValueError, KeyError):
        return {}


def line(rec, pane, ws):
    cmd = resume_cmd(rec)
    return f"{pane}  {ws.get(rec.get('workspace'), rec.get('workspace') or '?')}  {rec.get('agent')}  " + (
        cmd or f"session {rec.get('session') or '?'} (no resume command known for {rec.get('agent')})")


def resume(rec, pane):
    """Type the resume command into the pane's shell (in its old folder) and focus it."""
    cmd = resume_cmd(rec)
    if not cmd:
        return f"no resume command known for {rec.get('agent')}"
    if rec.get("cwd"):
        cmd = f"cd {shlex.quote(rec['cwd'])} && {cmd}"
    run("pane", "send-text", pane, cmd)
    run("pane", "send-keys", pane, "enter")
    subprocess.run([HERDR, "pane", "focus", pane], capture_output=True, timeout=5)  # best effort
    return ""


# --- the popup ---------------------------------------------------------------------------------------------------
def ui(scr):
    import curses
    curses.curs_set(0)
    curses.use_default_colors()
    sel, msg = 0, ""
    while True:
        mem = load()
        rows = lost(mem)
        save(mem)
        ws = workspaces()
        scr.erase()
        h, w = scr.getmaxyx()
        scr.addnstr(0, 0, f"Lost agents: {len(rows)}  ·  enter: resume in its pane  ·  x: forget  ·  r: refresh  ·  q: quit", w - 1)
        if not rows:
            scr.addnstr(2, 2, "None: every terminal that ran an agent still runs it.", w - 3)
        sel = max(0, min(sel, len(rows) - 1))
        for i, (t, rec, pane) in enumerate(rows[: h - 3]):
            scr.addnstr(2 + i, 1, line(rec, pane, ws), w - 2, curses.A_REVERSE if i == sel else 0)
        if msg:
            scr.addnstr(h - 1, 0, msg, w - 1)
        scr.refresh()
        c = scr.getch()
        msg = ""
        if c in (ord("q"), 27):
            return
        if c in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif c in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif c in (10, 13, curses.KEY_ENTER) and rows:
            t, rec, pane = rows[sel]
            msg = resume(rec, pane)
            if not msg:
                return
        elif c == ord("x") and rows:
            mem.pop(rows[sel][0], None)
            save(mem)


# --- upgrade: Claude Code ----------------------------------------------------------------------------------------
# Each pass starts fresh and keeps what it must remember in the state folder (CR_STATE): asks sent, resumes and loop
# relaunches still to do.
# Every odd-looking step below is a fix for a real incident; the comments name it. Only a pane's own session (a claude whose parent is a shell) is touched, only when herdr says idle, done or
# working; a working one (or one with shells, subagents, a loop) is asked to wind down first; blocked ones are left.
# ponytail: Linux only (/proc for processes, `ps --ppid` for a session's shells); other platforms need a process API.
SHELLS = {"zsh", "bash", "sh", "fish"}
CLAUDE = os.path.expanduser("~/.claude")
VDIR = os.path.expanduser("~/.local/share/claude/versions")
CR_STATE = os.environ.get("AGENT_UPGRADE_STATE") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agent-upgrade")
RECORDS = os.path.join(CR_STATE, "sessions")   # hooks/record.py: one file per claude pid
# Tests only: act on nothing but processes whose cwd is this folder. A sandbox herdr's pane ids (w1:p1) can equal a live
# session's HERDR_PANE_ID, and every scan here reads all of /proc.
ONLY_CWD = os.environ.get("AGENT_UPGRADE_ONLY_CWD")
ASK = ("[agent-upgrade] Claude Code %s is installed; this session still runs %s. At your next safe point "
       "(between loop iterations, if you loop): gracefully finish or stop every background shell, subagent and monitor "
       "of yours (TaskStop what can't finish soon), stop your loop if you have one (ScheduleWakeup with stop: true; "
       "CronDelete your recurring cron jobs), then end that reply with one line exactly: RESTART-READY, followed by the "
       "/loop command that restarts your loop as it was if you had one (e.g. RESTART-READY /loop /next-story:bmad-pony). "
       "agent-upgrade then disconnects Remote Control, restarts this session on the new version, reconnects it and types "
       "that /loop line back. If work is running now, carry on and do this when it is done; you will not be asked again.")
U = {"dry": False, "cur": "", "accept": "tab", "submit": "enter", "rcwas": False}
NBSP = " "


def echo(*a):
    print(*a, flush=True)


def h(*args, timeout=15):
    """herdr, errors swallowed: (ok, stdout)."""
    try:
        r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def tmux(pane):
    return pane.startswith("%")


SOCK = {}   # tmux pane -> its server's socket, from the session's own TMUX variable (pane ids repeat across servers)
# ponytail: filled by pane_of(); a pending resume in a pane whose claude is gone uses the default server


def t(pane, *args, timeout=15):
    try:
        r = subprocess.run(["tmux", *(["-S", SOCK[pane]] if SOCK.get(pane) else []), args[0], "-t", pane, *args[1:]],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def tmux_key(k):
    """herdr's key names (enter, c-c, ctrl+enter, up) as tmux's (Enter, C-c, C-Enter, Up)."""
    if k.startswith("c-"):
        return "C-" + k[2:]
    mods, _, base = k.replace("ctrl+", "C-").replace("shift+", "S-").replace("alt+", "M-").rpartition("-")
    base = {"enter": "Enter", "tab": "Tab", "up": "Up", "down": "Down", "escape": "Escape"}.get(base, base)
    return f"{mods}-{base}" if mods else base


def key(pane, k):
    return t(pane, "send-keys", tmux_key(k))[0] if tmux(pane) else h("pane", "send-keys", pane, k)[0]


def send_text(pane, text):
    return t(pane, "send-keys", "-l", "--", text)[0] if tmux(pane) else h("pane", "send-text", pane, text)[0]


def screen(pane, ansi=False):
    if tmux(pane):
        return t(pane, "capture-pane", "-p", *(["-e"] if ansi else []))[1]
    return h("agent", "read", pane, *(["--ansi"] if ansi else []))[1]


def pane_exists(pane):
    return t(pane, "display-message", "-p", "#{pane_id}")[0] if tmux(pane) else h("pane", "get", pane)[0]


def keybindings(kb):
    """(accept, submit) from keybindings.json's content. Submit: the first of ctrl/shift/alt+enter, enter whose first
    Chat binding is chat:submit (enter may be chat:newline), else enter. Accept: the first Autocomplete key bound to
    autocomplete:accept, else tab (the slash menu ignores submit until its entry is accepted, 2026-11-08)."""
    blocks = [b for b in (kb.get("bindings") or []) if isinstance(b, dict)] if isinstance(kb, dict) else []
    chat = [b["bindings"] for b in blocks if b.get("context") == "Chat" and isinstance(b.get("bindings"), dict)]
    submit = next((k for k in ("ctrl+enter", "shift+enter", "alt+enter", "enter")
                   if next((c[k] for c in chat if c.get(k) is not None), "") == "chat:submit"), "enter")
    accept = next((k for b in blocks if b.get("context") == "Autocomplete"
                   for k, v in (b["bindings"] if isinstance(b.get("bindings"), dict) else {}).items()
                   if v == "autocomplete:accept"), "tab")
    return accept, submit


def rules(lines):
    return [i for i, l in enumerate(lines) if l.startswith("─")]


def box_text(ansi):
    """The input box's text from an ANSI screen: lines between the last two rules (─), dim SGR-2 text dropped (Claude
    Code's greyed suggestion, not a draft), prompt mark "❯" + NBSP and blanks stripped (2026-11-08: a check for
    "❯ /restart" never matched, so every pass typed another /restart)."""
    lines = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", re.sub(r"\x1b\[2m[^\x1b\n]*", "", l)) for l in ansi.split("\n")]
    r = rules(lines)
    lo, hi = (r[-2] + 1, r[-1]) if len(r) > 1 else (0, r[-1]) if r else (0, 0)
    out = []
    for l in lines[lo:hi]:
        l = l.replace("\r", "").removeprefix("❯").replace(NBSP, " ").strip(" ")
        if l:
            out.append(l)
    return out


def box(pane):
    return box_text(screen(pane, ansi=True))


def agent_view(text):
    """Claude's agent view (`claude agents`: the Needs input / Completed list, its own "describe a task" box): no
    /restart there ("isn't available in agent view"), and a prompt typed there starts a new session. One session
    sat in it 2026-11-20 17:33 to 11-21: ~100 passes typed /restart, four were left stacked in its box. Its footer,
    seen in herdr-sandbox on 2.1.330 (2026-11-21)."""
    return "ctrl+x to remove all" in text or "describe a task for a new session" in text


def untypable(pane):
    """Why nothing may be typed into this pane now, or "". Checked by every path that types, right before it types:
    a draft was once lost under the wind-down ask."""
    a = screen(pane, ansi=True)
    if agent_view(a):
        return "it shows Claude's agent view; open the session there to upgrade it"
    b = box_text(a)
    return f"draft in box: {' '.join(b)[:40]!r}" if b else ""


def menu_sel(text):
    """The slash menu's highlighted entry ("/restart"), from the 19 lines above the box's top rule, or "". It must BE
    the typed command before it is accepted: accepting the nearest match once ran /deep-research."""
    lines = text.replace("\r", "").replace(NBSP, " ").split("\n")
    r = rules(lines)
    if len(r) < 2:
        return ""
    for i in range(r[-2] - 1, max(-1, r[-2] - 20), -1):
        if re.match(r" +❯ /", lines[i]):
            return lines[i].split()[1]
    return ""


def rc_menu_sel(text):
    """/remote-control's check: the last highlighted slash entry anywhere on screen."""
    m = [l for l in text.replace("\r", "").replace(NBSP, " ").split("\n") if re.match(r" +❯ /", l)]
    return m[-1].split()[1] if m else ""


def pids():
    """Process ids in lexicographic order, only ONLY_CWD's when that is set."""
    for p in sorted(e for e in os.listdir("/proc") if e.isdigit()):
        if ONLY_CWD:
            try:
                if os.readlink(f"/proc/{p}/cwd") != ONLY_CWD:
                    continue
            except OSError:
                continue
        yield p


def exe(pid):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def comm(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return ""


def pane_of(pid):
    """The herdr pane (HERDR_PANE_ID) or tmux pane (TMUX_PANE) the process was started in, or ""."""
    try:
        env = dict(e.split(b"=", 1) for e in open(f"/proc/{pid}/environ", "rb").read().split(b"\0") if b"=" in e)
    except OSError:
        return ""
    pane = (env.get(b"HERDR_PANE_ID") or env.get(b"TMUX_PANE") or b"").decode(errors="replace")
    if tmux(pane) and env.get(b"TMUX"):
        SOCK[pane] = env[b"TMUX"].decode(errors="replace").split(",")[0]
    return pane


def sess(pid, field):
    try:
        v = json.load(open(f"{CLAUDE}/sessions/{pid}.json")).get(field)
    except (OSError, ValueError, AttributeError):
        return ""
    return v if isinstance(v, str) else ""


def bridge(pid):   # Remote Control's id
    return sess(pid, "bridgeSessionId")


def alive(pid):
    return bool(pid) and os.path.exists(f"/proc/{pid}")


def touch(name):
    open(os.path.join(CR_STATE, name), "w").close()


def claude_pid(pane):
    return next((p for p in pids() if pane_of(p) == pane and exe(p).startswith(VDIR + "/")), "")


def shell_fg(pane):
    """The pane's shell owns its terminal (nothing else in the foreground to type into)."""
    for p in pids():
        if pane_of(p) != pane or comm(p) not in SHELLS:
            continue
        try:
            st = open(f"/proc/{p}/stat").read().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if st[2] == st[5]:   # pgrp == tpgid
            return True
    return False


def record(pid):
    """What the session's hooks wrote (hooks/record.py), or {}."""
    try:
        r = json.load(open(os.path.join(RECORDS, f"{pid}.json")))
    except (OSError, ValueError):
        return {}
    return r if isinstance(r, dict) else {}


def status(pane):
    """idle / working from the session's own hooks; herdr's status (it reads the screen) for a session without them."""
    p = claude_pid(pane)
    r = record(p) if p else {}
    if r.get("state") in ("idle", "working"):
        return r["state"]
    if tmux(pane):
        return ""
    ok, out = h("agent", "list")
    try:
        return "\n".join(a.get("agent_status") or "" for a in json.loads(out)["result"]["agents"] if a.get("pane_id") == pane)
    except (ValueError, KeyError, TypeError):
        return ""


def transcript(sid):
    f = sorted(glob.glob(f"{CLAUDE}/projects/*/{glob.escape(sid)}.jsonl")) if sid else []
    return f[0] if f else ""


def loop_state_lines(lines):
    """"none", "active" (a loop is scheduled), "active /loop ..." or "ready <the /loop line to relaunch>"."""
    state, line = "none", ""
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        m = d.get("message") or {} if isinstance(d, dict) else {}
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "ScheduleWakeup":
                i = b.get("input", {})
                state = "none" if i.get("stop") else "active"
                if not i.get("stop"):
                    line = "/loop" if "autonomous-loop" in (i.get("prompt") or "") else (i.get("prompt") or "")
            elif b.get("type") == "tool_use" and b.get("name") == "CronCreate" and not b.get("input", {}).get("durable") \
                    and b.get("input", {}).get("recurring", True):
                state, line = "active", ""  # a cron loop's /loop line is not in the call: only the agent can say it
            elif b.get("type") == "tool_use" and b.get("name") == "CronDelete" and state == "active":
                state, line = "none", ""    # ponytail: one delete ends it; several recurring crons would need ids
            elif b.get("type") == "text" and m.get("role") == "assistant":
                r = re.findall(r"^RESTART-READY(?:[ \t]+(/loop\b.*))?[ \t]*$", b.get("text") or "", re.M)
                if r:
                    state, line = "ready", (r[-1] or "").strip().strip("`")
    return state + (" " + line if state != "none" and line.startswith("/loop") else "")


def loop_state(sid):
    f = transcript(sid)
    if not f:
        return "none"
    with open(f, encoding="utf-8", errors="replace") as fh:
        return loop_state_lines(fh)


def shell_why(ps_out):
    """A shell of the session's own (Bash tool: shell-snapshots in its args) running over 2 min."""
    for l in ps_out.splitlines():
        f = l.split()
        if f and "shell-snapshots" in l and f[0].isdigit() and int(f[0]) > 120:
            return f"a shell running {int(f[0]) // 60} min"
    return ""


def subagent_why(d, now):
    """A subagent transcript written in the last 2 min whose first timestamp (its start: the .meta.json is rewritten
    when it finishes) is over 2 min old."""
    for f in sorted(glob.glob(os.path.join(d, "agent-*.jsonl"))):
        try:
            if now - os.stat(f).st_mtime >= 120:
                continue
            m = re.search(rb'"timestamp":"([^"]*)"', open(f, "rb").read(65536))
            t = datetime.fromisoformat(m.group(1).decode()).timestamp()
        except (OSError, AttributeError, ValueError):
            continue
        if now - t > 120:
            return f"a subagent working {int(now - t) // 60} min"
    return ""


def heavy(pid, sid):
    """Why stopping this claude now would lose substantial work, or ""."""
    try:
        ps = subprocess.run(["ps", "--ppid", pid, "-o", "etimes=,args="], capture_output=True, text=True).stdout
    except OSError:
        ps = ""
    now = int(time.time())
    return shell_why(ps) or subagent_why(os.path.join(os.path.dirname(transcript(sid)), sid, "subagents"), now)


def say(pane, text):
    """Type a prompt into an idle claude with an empty box and submit it: (ok, why not)."""
    why = untypable(pane)
    if why:
        return False, why
    if not send_text(pane, text):
        return False, ""
    time.sleep(1)
    if text.startswith("/"):
        sel = menu_sel(screen(pane))
        if sel:
            if sel != text.split(" ")[0]:
                key(pane, "c-c")
                return False, f"menu highlighted {sel}, not {text.split(' ')[0]}"
            key(pane, U["accept"])
            time.sleep(1)
    if not key(pane, U["submit"]):
        return False, ""
    s = ""
    for _ in range(10):
        time.sleep(1)
        s = status(pane)
        if s in ("working", "blocked") and not box(pane):   # 00:30 2026-11-19: "working" for a moment, /loop left unsent
            return True, ""
    return False, f"typed but the agent did not start (still {s or '?'})"


def type_resume(pane, sid, cmd):
    """Type the resume command into the pane's shell; True once the installed claude is up on that session."""
    try:
        os.remove(os.path.join(CR_STATE, f"resume-{sid}"))
    except OSError:
        pass
    if not (send_text(pane, cmd) and key(pane, "enter")):
        echo(f"FAIL {pane}: claude exited but could not type '{cmd}' into the pane; up-arrow there or run it by hand")
        return False
    for _ in range(30):
        time.sleep(1)
        np = claude_pid(pane)
        if np and exe(np) == U["cur"] and sess(np, "sessionId") == sid:
            return True
    echo(f"FAIL {pane}: typed '{cmd}' but no claude {os.path.basename(U['cur'])} on session {sid} came up in 30 s; look at the pane")
    return False


def pending():
    """A resume whose claude exited after exit_resume stopped waiting: typed once its pane has no claude left."""
    for f in sorted(glob.glob(os.path.join(CR_STATE, "resume-*"))):
        sid = os.path.basename(f)[len("resume-"):]
        try:
            pane, _, cmd = open(f).readline().rstrip("\n").partition("\t")
        except OSError:
            continue
        if not pane_exists(pane):
            echo(f"note: pane {pane} is gone; not resumed: {cmd}")
            os.remove(f)
            continue
        p = claude_pid(pane)
        if p:   # still exiting: next pass. Restarted by someone (another session): nothing to do.
            if not (sess(p, "sessionId") == sid and exe(p) != U["cur"]):
                os.remove(f)
            continue
        if not shell_fg(pane):
            echo(f"note {pane}: its shell is not at a prompt; not typing: {cmd}")
            continue
        if U["dry"]:
            echo(f"would type in {pane}: {cmd}")
            continue
        if type_resume(pane, sid, cmd):
            echo(f"resumed {pane} late (its claude exited after the wait): {cmd}")


def relaunch(only=None):
    """For each session with a pending /loop line (relaunch-<sid>) whose claude is on the installed version, idle, with
    an empty box: type the line. Every pass, so a relaunch that failed is retried."""
    for f in sorted(glob.glob(os.path.join(CR_STATE, "relaunch-*"))):
        sid = os.path.basename(f)[len("relaunch-"):]
        line = open(f).read().rstrip("\n")
        np = ""
        for s in sorted(glob.glob(f"{CLAUDE}/sessions/*.json")):
            i = os.path.basename(s)[:-5]
            if sess(i, "sessionId") == sid and alive(i) and exe(i) == U["cur"] and (not ONLY_CWD or i in pids()):
                np = i
        if not np:
            continue
        pane = pane_of(np)
        if not pane or (only and only != pane):
            continue
        for _ in range(20):
            if status(pane) in ("idle", "done") and not box(pane):
                break
            time.sleep(2)
        if U["dry"]:
            echo(f"would relaunch in {pane}: {line}")
            continue
        r = untypable(pane)
        if r:   # an earlier version typed it anyway after 40 s
            echo(f"skip {pane}: relaunch of '{line}' waits: {r}")
            continue
        ok, r = say(pane, line)
        if ok:
            os.remove(f)
            echo(f"relaunched in {pane}: {line}")
        else:
            echo(f"FAIL {pane}: relaunch of '{line}' ({r}); retried next pass")


def rc_slash(pane):
    """Type /remote-control and run it, only if the box is empty and the menu highlights exactly that entry (its
    ctrl+c on a mismatch would wipe a draft)."""
    if untypable(pane) or not send_text(pane, "/remote-control"):
        return False
    time.sleep(1)
    if rc_menu_sel(screen(pane)) != "/remote-control":
        key(pane, "c-c")
        return False
    key(pane, U["accept"])
    time.sleep(1)
    return key(pane, U["submit"])


def rc_off(pane, pid):
    """Remote Control's dialog (Disconnect this session / Show QR code / Continue, Continue selected): up to "Disconnect
    this session", Enter; True once the session file has no bridgeSessionId (rehearsed 2.1.327, 2026-11-18)."""
    if not rc_slash(pane):
        echo(f"note {pane}: no /remote-control in its menu")
        return False
    time.sleep(3)
    on = lambda: "❯ Disconnect this session" in screen(pane).replace(NBSP, " ")
    for _ in range(4):
        if on():
            break
        key(pane, "up")
        time.sleep(0.5)
    if not on():
        key(pane, "escape")
        echo(f"note {pane}: Remote Control's dialog had no Disconnect")
        return False
    key(pane, "enter")
    for _ in range(20):
        time.sleep(0.5)
        if not bridge(pid):
            time.sleep(1)
            return True
    echo(f"note {pane}: Remote Control did not disconnect")
    return False


def rc_on(pane):
    """The restarted claude reconnects by itself (a NEW Remote Control session); if not in 20 s, /remote-control."""
    np = ""
    for _ in range(20):
        time.sleep(1)
        np = claude_pid(pane)
        if np and bridge(np):
            return True
    if rc_slash(pane):
        for _ in range(20):
            time.sleep(1)
            if np and bridge(np):
                echo(f"note {pane}: Remote Control reconnected by /remote-control")
                return True
    echo(f"FAIL {pane}: restarted, but Remote Control is off; run /remote-control there")
    return False


def rc_restart(pane):
    """restart, and a session it disconnected from Remote Control gets it back whatever happened next."""
    U["rcwas"] = False
    r = restart(pane)
    if U["rcwas"]:
        rc_on(pane)
    return r


def fail(pane, why_not):
    """No ctrl+d, ever: an exit + resume once left a session half-exited for 9 hours with background work running. A
    session /restart can't reach is reported, not exited."""
    echo(f"fail {pane}: {why_not}; not restarted (no exit): look at the pane")
    return 1


def restart(pane):
    """0 restarted (/restart), 2 skipped (draft, busy, a loop asked to pause), 1 failed (reported, never exited)."""
    st = lambda n: os.path.join(CR_STATE, n)
    cannot = lambda why_not: fail(pane, why_not)

    if agent_view(screen(pane)):
        echo(f"skip {pane}: it shows Claude's agent view (no /restart there); nothing typed: open the session there")
        return 2
    pid = claude_pid(pane)
    sid = sess(pid, "sessionId") if pid else ""
    ls = loop_state(sid) if sid else "none"
    why = heavy(pid, sid) if pid and sid else ""
    # No Esc, no ctrl+d. A session that is working, has shells/subagents/monitors in flight, refused
    # /restart for background work, or loops is asked once to wind down at a safe point and say RESTART-READY (a
    # READY counts only after this pid was asked: an old one in the transcript is not an answer).
    ready = ls.startswith("ready") and pid and os.path.exists(st(f"ask-{pid}"))
    bg = pid and os.path.exists(st(f"bg-{pid}"))
    if not ready and (status(pane) == "working" or why or bg or ls.startswith("active")):
        if os.path.exists(st(f"ask-{pid}")):
            age = time.time() - os.path.getmtime(st(f"ask-{pid}"))
            if age > STUCK_ASK:   # one waited 1h48m on a `pgrep -f` loop that matched itself
                return cannot(f"asked to wind down {age / 3600:.1f} h ago and still not RESTART-READY; "
                              f"held by: {holders(pid) or 'nothing visible (a subagent or monitor?)'}")
            echo(f"skip {pane}: asked to wind down; waiting for its RESTART-READY")
            return 2
        b = box(pane)
        if b:
            echo(f"skip {pane}: the prompt holds a draft: {' '.join(b)[:40]!r}")
            return 2
        ok, r = say(pane, ASK % (os.path.basename(U["cur"]), os.path.basename(exe(pid))))
        if ok:
            touch(f"ask-{pid}")
            echo(f"asked {pane} to wind down (shells, subagents, loop) at a safe point")
            return 2
        echo(f"fail {pane}: could not ask it to wind down ({r})")
        return 1
    b = box(pane)
    if b and all(l == "/restart" for l in b):   # left by an early version, which typed one per pass
        key(pane, "c-c")
        time.sleep(1)
        b = box(pane)
    if b:
        echo(f"skip {pane}: the prompt holds a draft: {' '.join(b)[:40]!r}")
        return 2
    line = ls.removeprefix("ready ") if ls != "ready" else (record(pid).get("loop") or "" if pid else "")
    if ready and line:   # its loop comes back after the restart: its RESTART-READY line, else the /loop it was started with
        open(st(f"relaunch-{sid}"), "w").write(line + "\n")
    # Remote Control connected: disconnect it, /restart, and it comes back. One that will not
    # disconnect is reported: /restart refuses under it, and there is no exit fallback.
    if pid and bridge(pid):
        if rc_off(pane, pid):
            U["rcwas"] = True
    if pid and bridge(pid):
        return cannot("Remote Control would not disconnect, and /restart refuses while it is connected")
    cmd = resume_cmd({"agent": "claude", "session": sid, "flags": clean("claude", argv(pid)[1:])}) if pid and sid else None
    for attempt in (1, 2):
        b = untypable(pane)
        if b:
            echo(f"skip {pane}: {b}")
            return 2
        if not send_text(pane, "/restart"):
            return 1
        time.sleep(1)
        sel = menu_sel(screen(pane))
        if sel == "/restart":
            break
        # a claude without /restart highlights the nearest match (/deep-research, 2026-11-08); one
        # 2026-11-19 highlighted nothing just after Remote Control's dialog closed: one more try 3 s later
        key(pane, "c-c")
        if attempt == 2:
            return cannot(f"no /restart in its menu (highlighted: {sel or 'nothing'})")
        time.sleep(3)
    if not key(pane, U["accept"]):
        return 1
    time.sleep(1)
    if not key(pane, U["submit"]):
        return 1
    for _ in range(6):
        time.sleep(2)
        if exe(claude_pid(pane)) == U["cur"]:
            return settled(pane, sid, cmd)
        why = refused(screen(pane))
        if why:
            if "Remote Control" in why:
                return cannot("/restart refused: Remote Control is connected")
            if pid:
                touch(f"bg-{pid}")   # e.g. "work is running in the background": retried in an hour
            echo(f"refused {pane}: {why.split('restart while ', 1)[-1]}")
            return 2
    echo(f"note: {pane}: /restart sent ({U['accept']}, {U['submit']}), but the pane still runs an old claude")
    return 1


STUCK_ASK = 3600   # an ask unanswered this long is reported, with what holds it
TELL = os.path.expanduser(os.environ.get("AGENT_UPGRADE_TELL", ""))   # an agent that brings back loops after a pass


def argv(pid):
    try:
        return [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        return []


def refused(scr):
    """The refusal under the last `❯ /restart`, or "". Claude redraws a repeated refusal in place on the same line, so
    counting refusal lines missed one (2026-11-19)."""
    ls = [l for l in scr.split("\n") if l.strip()]
    at = [i for i, l in enumerate(ls) if l.lstrip().startswith("❯ /restart")]
    return next((l.strip() for l in ls[at[-1] + 1:at[-1] + 4] if "Can't restart while" in l), "") if at else ""


def holders(pid):
    """What a claude's own shells are running: "pid N (age) command head; ..." (its Bash tool's background shells)."""
    try:
        out = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid=,etimes=,args="], capture_output=True, text=True).stdout
    except OSError:
        return ""
    rows = []
    for ln in out.splitlines():
        p, secs, cmd = (ln.split(None, 2) + ["", ""])[:3]
        head = re.sub(r".*?(?:&& )?eval '", "", cmd)[:90] if "eval '" in cmd else cmd[:90]
        rows.append(f"pid {p} ({int(secs) // 60} min) {head}")
    return "; ".join(rows)


def settled(pane, sid, cmd):
    """/restart started the new claude; count it only if it is still there 15 s later. One exited
    within seconds (17:21 2026-11-19) and the pane sat at its shell for 17 min: one that dropped to its shell is
    resumed there (a relaunch, not an exit)."""
    time.sleep(15)
    if claude_pid(pane):
        return 0
    if not cmd:
        return fail(pane, "the new claude exited after /restart and its session is unknown; resume it by hand")
    if not shell_fg(pane):
        open(os.path.join(CR_STATE, f"resume-{sid}"), "w").write(f"{pane}\t{cmd}\n")   # pending() types it later
        return fail(pane, f"the new claude exited after /restart; its shell is busy, the next pass resumes it: {cmd}")
    echo(f"note {pane}: the new claude exited within 15 s of /restart; resumed: {cmd}")
    return 0 if type_resume(pane, sid, cmd) else 1


def loops_due():
    """After a pass that restarted anything, tell the agent in AGENT_UPGRADE_TELL's folder (one that keeps other
    agents' loops running), once it is idle with an empty box; a flag file carries it to the next pass until then."""
    flag = os.path.join(CR_STATE, "loops-due")
    if not os.path.exists(flag):
        return
    if not TELL or not os.path.isdir(TELL):
        os.remove(flag)
        return
    p = next((p for p in pids() if exe(p).startswith(VDIR + "/") and pane_of(p) and cwd(p) == TELL), "")
    pane = pane_of(p) if p else ""
    if not pane or status(pane) not in ("idle", "done") or box(pane):
        return
    ok, why = say(pane, open(flag).read().strip())
    if ok:
        os.remove(flag)
        echo(f"told {pane} ({os.path.basename(TELL)}) about the restarts")
    else:
        echo(f"note {pane}: could not tell it about the restarts ({why}); next pass")


def cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def ancestors():
    """This process's ancestors' pids (Linux)."""
    out, p = set(), os.getpid()
    while p > 1:
        try:
            p = int(open(f"/proc/{p}/stat").read().rsplit(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
        out.add(p)
    return out


LOOP_EVERY = 600
ALIVE = os.path.join(CR_STATE, "loop.alive")   # touched every pass: a scheduler can stand aside while it is fresh


def loop():
    """herdr starts this with the server: an upgrade pass every 10 minutes, one loop per user (flock). Each pass is a
    fresh process, so a plugin update takes effect at the next pass. It dies with the server and comes back when the
    server (or the machine) starts again; pending resumes and loop relaunches are state files, carried over."""
    if WIN:
        return   # ponytail: the upgrade reads /proc; Windows and macOS keep their own way of upgrading for now
    import fcntl
    os.makedirs(CR_STATE, exist_ok=True)
    me = open(os.path.join(CR_STATE, "loop.lock"), "w")
    try:
        fcntl.flock(me, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return   # one runs already
    log = os.path.join(CR_STATE, "loop.log")
    time.sleep(120)   # the server's agents first (the timer waited 2 min after boot too)
    while True:
        open(ALIVE, "w").close()
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "once"], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        try:
            old = open(log).read().splitlines()[-400:]
        except OSError:
            old = []
        with open(log, "w") as f:
            f.write("\n".join(old + [time.strftime("%F %T ") + l for l in out.splitlines()]) + "\n")
        time.sleep(LOOP_EVERY - time.time() % LOOP_EVERY)   # on the 10-minute marks, as the timer was
        open(ALIVE, "w").close()


def upgrade(dry):
    """One pass. Returns the exit code: 0 when nothing failed."""
    try:
        U["cur"] = os.path.realpath(os.path.expanduser("~/.local/bin/claude"), strict=True)
    except OSError:
        echo("no claude installed")
        return 0
    U["dry"] = dry
    # One pass at a time (flock): two would type into the same panes.
    lock = None
    if not dry and not WIN:   # ponytail: no lock on Windows (no fcntl); the upgrade itself is Linux-only for now
        import fcntl
        os.makedirs(CR_STATE, exist_ok=True)
        lock = open(os.path.join(CR_STATE, "upgrade.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            echo("another upgrade pass is running: skipped")
            return 0
    mine = ancestors()
    stale, outside = {}, 0   # pane -> version
    for p in pids():
        x = exe(p)
        if not x.startswith(VDIR + "/") or x == U["cur"]:
            continue
        try:
            pp = open(f"/proc/{p}/stat").read().rsplit(") ", 1)[1].split()[1]
        except (OSError, IndexError):
            continue
        if comm(pp) not in SHELLS:   # the daemon's background sessions (parent: a bg-pty-host) are left alone
            continue
        pane = pane_of(p)
        if pane and int(p) in mine:   # the session that started this run (/claude-upgrade): restarting it cuts its turn
            echo(f"skip {pane}: it started this run; it restarts on a later pass")
        elif pane:
            stale[pane] = os.path.basename(x)
        else:
            outside += 1
    os.makedirs(CR_STATE, exist_ok=True)
    try:
        U["accept"], U["submit"] = keybindings(json.load(open(f"{CLAUDE}/keybindings.json")))
    except (OSError, ValueError):
        U["accept"], U["submit"] = "tab", "enter"
    for f in os.listdir(CR_STATE):
        if f.split("-")[0] in ("rc", "bg", "nr", "ask") and not alive(f.rsplit("-", 1)[-1]):
            os.remove(os.path.join(CR_STATE, f))
    have = bool(shutil.which(HERDR) or shutil.which("tmux"))
    done = busy = failed = 0
    if stale and have:
        for pane, ver in stale.items():
            s = status(pane)
            if s not in ("idle", "done", "working"):
                busy += 1
                continue
            if dry:
                p = claude_pid(pane)
                i = sess(p, "sessionId") if p else ""
                echo(f"would restart {pane} ({ver}, {s}, loop: {loop_state(i)}, in flight: {heavy(p, i) if p else ''})")
                done += 1
                continue
            r = rc_restart(pane)
            if r == 0:
                echo(f"restarted {pane} (was {ver})")
                done += 1
            elif r == 1:
                failed += 1
    if done and not dry:
        open(os.path.join(CR_STATE, "loops-due"), "w").write(
            f"The Claude upgrade pass at {time.strftime('%H:%M')} restarted {done} session(s) onto "
            f"{os.path.basename(U['cur'])} ({os.path.join(CR_STATE, 'loop.log')}). Bring back the loops that should be "
            "running that agent-upgrade did not relaunch, and say what you did.\n")
    if have:
        pending()
        relaunch()
        if not dry:
            loops_due()
    echo(f"claude {os.path.basename(U['cur'])}: {done} {'to restart' if dry else 'restarted'}, "
         f"{busy} busy (next pass), {failed} failed, {outside} outside herdr and tmux")
    return 1 if failed else 0

UNIT = "agent-upgrade"
LABEL = "io.github.benjmhart.agent-upgrade"


def script_path():
    """The stable path when the session hook keeps one (CR_STATE/current links to the installed plugin, whose cache
    path changes with every version), else this file's own path: re-run setup after an update then."""
    cur = os.path.join(CR_STATE, "current", "agent_upgrade.py")
    return cur if os.path.exists(cur) else os.path.abspath(__file__)


def setup(kind, write=True):
    """Run a pass every 10 minutes: herdr's startup entry, a systemd user timer, or a launchd agent. Returns
    {path: content} of what it wrote."""
    me, env = script_path(), {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update({k: os.environ[k] for k in SETTINGS if os.environ.get(k)})
    files, cmds = {CONF: "".join(f"{k}={v}\n" for k, v in env.items() if k != "PATH")}, []
    if kind == "herdr":   # herdr-plugin.toml's startup entry runs `loop` while the server runs
        cmds = [[HERDR, "plugin", "link", os.path.dirname(me)]]
    elif kind == "systemd":
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systemd/user")
        files[os.path.join(d, UNIT + ".service")] = (
            "[Unit]\nDescription=agent-upgrade: restart agent sessions onto the installed version\n\n[Service]\n"
            "Type=oneshot\n" + "".join(f"Environment={k}={shlex.quote(v)}\n" for k, v in env.items())
            + f"ExecStart={sys.executable} {me} once\n")
        files[os.path.join(d, UNIT + ".timer")] = (
            "[Unit]\nDescription=agent-upgrade every 10 minutes\n\n[Timer]\nOnBootSec=2min\nOnCalendar=*:0/10\n\n"
            "[Install]\nWantedBy=timers.target\n")
        cmds = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT + ".timer"]]
    elif kind == "launchd":
        p = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        esc = lambda v: v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        log = os.path.join(CR_STATE, "loop.log")
        files[p] = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
                    f"<key>Label</key><string>{LABEL}</string>\n<key>ProgramArguments</key><array>"
                    f"<string>{esc(sys.executable)}</string><string>{esc(me)}</string><string>once</string></array>\n"
                    "<key>EnvironmentVariables</key><dict>"
                    + "".join(f"<key>{k}</key><string>{esc(v)}</string>" for k, v in env.items())
                    + f"</dict>\n<key>StartInterval</key><integer>600</integer>\n<key>RunAtLoad</key><true/>\n"
                    f"<key>StandardOutPath</key><string>{esc(log)}</string>\n<key>StandardErrorPath</key>"
                    f"<string>{esc(log)}</string>\n</dict></plist>\n")
        cmds = [["launchctl", "unload", p], ["launchctl", "load", "-w", p]]
    else:
        raise RuntimeError("setup takes herdr, systemd or launchd")
    if write:
        os.makedirs(CR_STATE, exist_ok=True)
        for p, c in files.items():
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(c)
            echo(f"wrote {p}")
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True)
            echo(f"{' '.join(c)}: {'ok' if r.returncode == 0 else (r.stderr or r.stdout).strip()}")
        echo(f"runs {me}" + ("" if me.startswith(os.path.join(CR_STATE, "current")) else
                             " (this version's own path: re-run setup after updating the plugin)"))
    return files


def rpc(method, params):
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr/herdr.sock")
    line_ = (json.dumps({"id": "au", "method": method, "params": params}) + "\n").encode()
    if WIN:
        p = path if path.startswith("\\\\.\\pipe\\") else "\\\\.\\pipe\\" + path
        with open(p, "r+b", buffering=0) as f:
            f.write(line_)
            return json.loads(f.readline())
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3)
        s.connect(path)
        s.sendall(line_)
        return json.loads(s.makefile().readline())


def main(cmd, args):
    if cmd == "remember":
        remember()
    elif cmd == "lost":
        mem = remember()
        rows = lost(mem)
        save(mem)
        ws = workspaces()
        for t, rec, pane in rows:
            print(line(rec, pane, ws))
        sys.exit(1 if rows else 0)
    elif cmd == "forget" and args:
        mem = load()
        for t in [t for t, r in mem.items() if r.get("pane") == args[0]]:
            del mem[t]
        save(mem)
    elif cmd == "ui":
        import curses
        curses.wrapper(ui)
    elif cmd == "open":
        resp = rpc("plugin.pane.open", {"plugin_id": os.environ.get("HERDR_PLUGIN_ID", PLUGIN), "entrypoint": "ui",
                                        "focus": True})
        if "error" in resp:
            sys.exit(f"agent-upgrade: plugin.pane.open: {resp['error']}")
    elif cmd == "loop":
        loop()
    elif cmd == "setup" and len(args) == 1:
        setup(args[0])
    elif cmd in ("once", "upgrade") and args in ([], ["--dry-run"]):   # any other argument refuses: never live by accident
        sys.exit(upgrade(args[:1] == ["--dry-run"]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2:])
    except RuntimeError as e:
        sys.exit(f"agent-upgrade: {e}")
