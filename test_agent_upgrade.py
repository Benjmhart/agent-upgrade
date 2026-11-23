# python3 test_agent_upgrade.py: the logic without herdr (memory, lost, resume commands, flag cleaning)
import os, sys, tempfile
os.environ["HERDR_PLUGIN_STATE_DIR"] = tempfile.mkdtemp()
import agent_upgrade as au

assert au.clean("claude", ["--resume", "abc", "--permission-mode", "auto", "-c"]) == ["--permission-mode", "auto"]
assert au.clean("claude", ["--resume=abc", "--model", "opus"]) == ["--model", "opus"]
assert au.clean("pi", ["--session", "x", "-n", "work"]) == ["-n", "work"]
assert au.resume_cmd({"agent": "claude", "session": "s1", "flags": ["--permission-mode", "auto"]}) == "claude --resume s1 --permission-mode auto"
assert au.resume_cmd({"agent": "pi", "session": "s2"}) == "pi --session s2"
assert au.resume_cmd({"agent": "gemini", "session": "s3"}) is None and au.resume_cmd({"agent": "claude"}) is None

mem = {"t1": {"agent": "claude", "session": "a", "pane": "w1:p1"},   # still running
       "t2": {"agent": "claude", "session": "b", "pane": "w2:p1"},   # exited, terminal still there: lost
       "t3": {"agent": "pi", "session": "c", "pane": "w3:p1"}}      # pane closed: forgotten
panes = [{"pane_id": "w1:p1", "terminal_id": "t1"}, {"pane_id": "w2:p9", "terminal_id": "t2"}]
agents = [{"pane_id": "w1:p1", "terminal_id": "t1"}]
out = au.lost(mem, panes, agents)
assert [(t, p) for t, _, p in out] == [("t2", "w2:p9")], out   # found by terminal, at its pane id now
assert "t3" not in mem and "t1" in mem
au.save(mem); assert au.load() == mem
# --- upgrade (the claude-restart port): the pure parts
import json, time
ben = {"bindings": [{"context": "Chat", "bindings": {"enter": "chat:newline", "alt+enter": "chat:submit", "shift+enter": "chat:submit", "ctrl+enter": "chat:submit"}},
                    {"context": "Autocomplete", "bindings": {"alt+enter": "autocomplete:accept"}}]}
assert au.keybindings(ben) == ("alt+enter", "ctrl+enter")
assert au.keybindings({}) == ("tab", "enter") and au.keybindings([]) == ("tab", "enter")
assert au.keybindings({"bindings": [{"context": "Chat", "bindings": {"ctrl+enter": "chat:newline", "shift+enter": "chat:submit"}}]}) == ("tab", "shift+enter")
# the first Chat block that binds a key decides it: a later block's chat:submit does not count
assert au.keybindings({"bindings": [{"context": "Chat", "bindings": {"ctrl+enter": "chat:newline"}}, {"context": "Chat", "bindings": {"ctrl+enter": "chat:submit"}}]})[1] == "enter"

RULE = "\x1b[0m\x1b[38;2;189;147;249m" + "─" * 40 + " tax-prep 🐺 ─\x1b[0m\r"
RULE2 = "\x1b[0m\x1b[38;2;189;147;249m" + "─" * 60 + "\x1b[0m\r"
STATUS = "  \x1b[0m\x1b[1m\x1b[38;5;3mINSERT\x1b[0m · ctx 68%\r"
scr = lambda *box: "\n".join(["old " + "─" * 3, RULE2, "\x1b[0m✻ Baked for 9s\x1b[0m\r", RULE, *box, RULE2, STATUS])
assert au.box_text(scr("❯ \r")) == []                                   # empty: prompt mark + NBSP
assert au.box_text(scr("❯ \x1b[2mpush it\x1b[0m\r")) == []             # dim suggestion is not a draft
assert au.box_text(scr("❯ fix the \x1b[1mbug\x1b[0m\r", "  and this\r")) == ["fix the bug", "and this"]
assert au.box_text(scr("❯ /restart\r", "/restart\r")) == ["/restart", "/restart"]
assert au.box_text("no rules here") == [] and au.box_text(RULE2 + "\nabove only") == []
assert au.box_text("draft\n" + RULE2) == ["draft"]                         # one rule: everything above it (as the chore)

menu = "\n".join(["  ⎿ hi\r", "   /review   Review a PR\r", "  ❯ /restart   Restart Claude Code\r", "    /resume\r", "─" * 9 + "\r", "❯ /restart\r", "─" * 9 + "\r", "  INSERT"])
assert au.menu_sel(menu) == "/restart"
assert au.menu_sel(menu.replace("❯ /restart   R", "❯ /deep-research R")) == "/deep-research"
assert au.menu_sel("  ❯ /restart\n" + "─" * 9) == ""                         # under two rules: nothing (as the chore)
assert au.menu_sel("\n".join(["  ❯ /restart"] + ["x"] * 20 + ["─", "❯", "─"])) == ""   # only the 19 lines above the box
assert au.rc_menu_sel("  ❯ /remote-control  Connect\n─\n❯ /remote-control\n─") == "/remote-control"

j = lambda role, *c: json.dumps({"message": {"role": role, "content": list(c)}})
wake = lambda **i: {"type": "tool_use", "name": "ScheduleWakeup", "input": i}
text = lambda t: {"type": "text", "text": t}
ls = au.loop_state_lines
assert ls([]) == "none" and ls(["not json", j("user", "plain")]) == "none"
assert ls([j("assistant", wake(prompt="/loop /next-story:bmad-pony", delaySeconds=60))]) == "active /loop /next-story:bmad-pony"
assert ls([j("assistant", wake(prompt="<<autonomous-loop>> go"))]) == "active /loop"
assert ls([j("assistant", wake(prompt="/loop x")), j("assistant", wake(stop=True))]) == "none"
assert ls([j("assistant", {"type": "tool_use", "name": "CronCreate", "input": {"cron": "*/5 * * * *"}})]) == "active"
assert ls([j("assistant", {"type": "tool_use", "name": "CronCreate", "input": {"durable": True}})]) == "none"
assert ls([j("assistant", wake(prompt="/loop a")), j("assistant", text("done.\nRESTART-READY /loop /next-story:bmad-pony lane-2`"))]) == "ready /loop /next-story:bmad-pony lane-2"
assert ls([j("user", text("RESTART-READY /loop x"))]) == "none"               # only the agent's own reply counts
assert ls([j("assistant", text("RESTART-READY /loopy"))]) == "none"
# 2026-11-19: a one-shot reminder is no loop; CronDelete ends one; a bare RESTART-READY (no loop) is ready
assert ls([j("assistant", {"type": "tool_use", "name": "CronCreate", "input": {"recurring": False}})]) == "none"
cron = j("assistant", {"type": "tool_use", "name": "CronCreate", "input": {}})
assert ls([cron]) == "active"
assert ls([cron, j("assistant", {"type": "tool_use", "name": "CronDelete", "input": {"id": "x"}})]) == "none"
assert ls([j("assistant", text("done.\nRESTART-READY"))]) == "ready"

assert au.shell_why("   30 /bin/zsh -c source shell-snapshots/x\n  400 /bin/zsh -c source shell-snapshots/y\n") == "a shell running 6 min"
assert au.shell_why("  400 node mcp-server\n   99 zsh shell-snapshots/a\n") == ""
d = tempfile.mkdtemp(); now = time.time()
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
def agent(name, start, mtime):
    p = os.path.join(d, name); open(p, "w").write(json.dumps({"timestamp": iso(start)}, separators=(",", ":")) + "\n"); os.utime(p, (mtime, mtime))  # compact, as Claude Code writes it
agent("agent-a.jsonl", now - 600, now - 300)    # finished writing 5 min ago: not in flight
agent("agent-b.jsonl", now - 60, now)           # started 1 min ago: light
assert au.subagent_why(d, now) == ""
agent("agent-c.jsonl", now - 400, now - 10)     # started ~6 min ago, still writing: substantial
assert au.subagent_why(d, now) == "a subagent working 6 min", au.subagent_why(d, now)
assert au.subagent_why(os.path.join(d, "missing"), now) == ""

# resume flags: the session's own, minus its old session choice; quoted for the shell
argv = ["--resume", "abc", "--permission-mode", "auto", "-c", "--name", "a b", "--session-id=x"]
assert au.resume_cmd({"agent": "claude", "session": "s9", "flags": au.clean("claude", argv)}) == "claude --resume s9 --permission-mode auto --name 'a b'"
print("ok")

# refused(): the refusal under the LAST `❯ /restart`, however many times Claude redrew it in place
scr = "● done\n❯ /restart\n  ⎿  Can't restart while work is running in the background\n\n❯ /restart\n  ⎿  Can't restart while work is running in the background\n─────\n❯ \n"
assert "work is running" in au.refused(scr)
assert au.refused("❯ /restart\n  ⎿  Can't restart while x\n❯ /restart\n  ⎿  Successfully updated from 2.1.328 to version 2.1.329\n") == ""
assert au.refused("no command here\n") == ""
# holders(): this process's own children, with age and the eval'd command's head
import subprocess as _sp
_c = _sp.Popen(["bash", "-c", "eval 'sleep 30'"])
try:
    import time as _t; _t.sleep(0.3)
    h_ = au.holders(os.getpid())
    assert f"pid {_c.pid} (0 min)" in h_ and "sleep 30" in h_, h_
finally:
    _c.kill()
print("ok")
# agent view (claude agents, 2.1.330): never typed into
bx = lambda *b: "\n".join([RULE, *b, RULE2, STATUS])
AV = "\n".join(["❯ Needs input", "  ✻ personal-comms 🐺   press Start", RULE2, "❯ describe a task for a new session\r", RULE2,
                "  enter to collapse · ctrl+x to remove all · ? for shortcuts"])
assert au.agent_view(AV) and not au.agent_view(bx("❯ \r"))
au.screen = lambda pane, ansi=False: AV
assert au.untypable("w1:p1").startswith("it shows Claude's agent view")
assert au.say("w1:p1", "/restart")[0] is False and au.rc_slash("w1:p1") is False
au.screen = lambda pane, ansi=False: bx("❯ accept the flagged choices\r")
assert au.untypable("w1:p1") == "draft in box: 'accept the flagged choices'"
assert au.say("w1:p1", "hello") == (False, "draft in box: 'accept the flagged choices'")
au.screen = lambda pane, ansi=False: bx("❯ \r")
assert au.untypable("w1:p1") == ""
print("ok")

# --- agent-upgrade 0.1: tmux keys, the session hook's record, setup's units, status from the record
assert [au.tmux_key(k) for k in ("enter", "c-c", "ctrl+enter", "up", "tab", "escape", "shift+enter")] == \
    ["Enter", "C-c", "C-Enter", "Up", "Tab", "Escape", "S-Enter"]
assert au.tmux("%3") and not au.tmux("w1:p2")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
import record
r = record.update({}, "start", {"session_id": "s1", "cwd": "/x"})
assert r["state"] == "idle" and r["session"] == "s1"
r = record.update(r, "prompt", {"prompt": "/loop 5m /check"})
assert r["state"] == "working" and r["loop"] == "/loop 5m /check"
r = record.update(r, "prompt", {"prompt": "hello"})
assert r["loop"] == "/loop 5m /check"                      # a later prompt keeps the loop
assert record.update(r, "stop", {})["state"] == "idle"
assert record.update(r, "prompt", {"prompt": "/loop stop"})["loop"] is None
_st = au.CR_STATE
try:
    au.CR_STATE = tempfile.mkdtemp()
    f = au.setup("systemd", write=False)
    svc = next(v for k, v in f.items() if k.endswith(".service"))
    assert "ExecStart=" in svc and svc.rstrip().endswith(" once") and "Environment=PATH=" in svc
    assert "OnCalendar=*:0/10" in next(v for k, v in f.items() if k.endswith(".timer"))
    plist = next(iter(au.setup("launchd", write=False).values()))
    assert "<integer>600</integer>" in plist and "<string>once</string>" in plist
    try:
        au.setup("cron", write=False); assert False
    except RuntimeError:
        pass
    au.RECORDS = os.path.join(au.CR_STATE, "sessions"); os.makedirs(au.RECORDS)
    open(os.path.join(au.RECORDS, "42.json"), "w").write('{"state": "working"}')
    _cp = au.claude_pid
    au.claude_pid = lambda pane: "42"
    assert au.status("%1") == "working"
    au.claude_pid = lambda pane: "43"
    assert au.status("%1") == ""                           # tmux, no record: unknown, so it waits
    au.claude_pid = _cp
finally:
    au.CR_STATE = _st
print("ok")
