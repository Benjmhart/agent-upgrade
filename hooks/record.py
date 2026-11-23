"""Claude Code hook: record this session for agent-upgrade, so it needs no screen reading to know a session's state.

SessionStart, UserPromptSubmit and Stop each write <state>/sessions/<claude pid>.json: the session id, cwd, pane
(herdr's HERDR_PANE_ID or tmux's TMUX_PANE), state (idle after Stop, working after a prompt) and the last /loop prompt.
SessionStart also points <state>/current at this plugin, a stable path for schedulers (the plugin cache path is
versioned). Never fails the session: any error is swallowed.
"""
import json, os, sys, time

STATE = os.environ.get("AGENT_UPGRADE_STATE") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agent-upgrade")


def claude_pid():
    """The nearest ancestor that is claude (the hook runs under a shell it started)."""
    p = os.getppid()
    while p > 1:
        try:
            if open(f"/proc/{p}/comm").read().strip() == "claude" or "/claude/versions/" in os.readlink(f"/proc/{p}/exe"):
                return p
            p = int(open(f"/proc/{p}/stat").read().rsplit(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    return None


def update(rec, event, data):
    """The record after this event (pure: tested)."""
    rec = dict(rec)
    rec.update({"session": data.get("session_id") or rec.get("session"), "cwd": data.get("cwd") or rec.get("cwd"),
                "pane": os.environ.get("HERDR_PANE_ID") or os.environ.get("TMUX_PANE") or rec.get("pane"),
                "at": time.time()})
    if event == "start":
        rec["state"] = "idle"
    elif event == "prompt":
        rec["state"] = "working"
        p = (data.get("prompt") or "").strip()
        if p.startswith("/loop"):
            rec["loop"] = None if p.split()[1:2] == ["stop"] else p
    elif event == "stop":
        rec["state"] = "idle"
    return rec


def main(event):
    data = json.load(sys.stdin)
    pid = claude_pid()
    if not pid:
        return
    d = os.path.join(STATE, "sessions")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, f"{pid}.json")
    try:
        rec = json.load(open(f))
    except (OSError, ValueError):
        rec = {}
    tmp = f"{f}.{os.getpid()}.tmp"
    open(tmp, "w").write(json.dumps(update(rec, event, data)))
    os.replace(tmp, f)
    if event == "start":
        for old in os.listdir(d):   # sessions whose claude is gone
            if old.split(".")[0].isdigit() and not os.path.exists(f"/proc/{old.split('.')[0]}"):
                try:
                    os.remove(os.path.join(d, old))
                except OSError:
                    pass
        root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        link = os.path.join(STATE, "current")
        if os.path.realpath(link) != os.path.realpath(root):
            os.symlink(root, link + ".tmp")
            os.replace(link + ".tmp", link)


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except Exception:   # a hook must never get in the session's way
        pass
