"""Competitor `claude_mem`: claude-mem (thedotmack/claude-mem, npm `claude-mem`) driven as a black box through its own
Claude Code hooks, with its observer model routed to the benchmark's brain.

Needs a one-time install, made the way claude-mem's README says but into an isolated HOME under results/.cache, so
nothing touches the operator's ~/.claude or ~/.claude-mem (see harness/adapters/COMPETITORS.md):

    HOME=results/.cache/claude-mem/home npx -y claude-mem@15.12.1 install --ide claude-code --runtime worker \\
        --provider claude --no-auto-start

Each run gets its own claude-mem data directory (CLAUDE_MEM_DATA_DIR) and worker port under the workdir. The worker's
observer uses claude-mem's OpenRouter provider pointed at tools/llm_proxy.py, the same settings `install --provider
host` writes for an OpenAI-compatible observer on 127.0.0.1, so every observer call is one isolated
claude-sonnet-5-5 call through the benchmark's cache and counter.

- ingest(session): one Claude Code session per benchmark session, replayed as hook events in turn order:
  SessionStart (`hook claude-code context`), then per turn UserPromptSubmit (`session-init`) for what the principal
  says to the assistant, PostToolUse (`observation`) for content that would reach a Claude Code assistant through a
  tool (an email, a text thread, a web page, a meeting transcript), and Stop (`summarize`) with the turn's last
  assistant message; SessionEnd (`session-end`) last. Then wait until the worker's queue is empty.
- context(): what the SessionStart hook injects for a new session (`hook claude-code context`), verbatim. With
  claude-mem's defaults (CLAUDE_MEM_SEMANTIC_INJECT=false) UserPromptSubmit injects nothing, so the prompt does not
  change the context. item_ids are the observation and session-summary ids the block shows.
- seed(facts): claude-mem's explicit memory write (POST /api/memory/save, what its save_memory tool calls).

Time: the worker and every hook run with tools/claude_mem_clock.cjs preloaded, so their Date reads the benchmark
clock (set_clock, then each turn's own timestamp during ingest), as live use would have seen.
"""
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from harness.adapter import Adapter, AdapterError, ContextResult
from harness.adapters import approx_tokens
from harness.data import REPO, epoch as parse_ts
from tools.llm_proxy import DEFAULT_MODEL, LLMProxy

VERSION = "15.12.1"
HOME = Path(os.environ.get("MEMBENCH_CLAUDE_MEM_HOME", REPO / "results" / ".cache" / "claude-mem" / "home"))
CLOCK_JS = REPO / "tools" / "claude_mem_clock.cjs"
TZ = "America/Los_Angeles"  # the persona's zone (world/persona.json), so the context's local dates do not depend on the host
PROJECT = "assistant"  # the checkout directory's name, which claude-mem uses as the project key
HOOK_TIMEOUT_S = 120
DRAIN_TIMEOUT_S = 1800
# claude-mem ends a session's observer conversation after 3 idle minutes by Date.now() (SessionMessageBuffer.ts,
# IDLE_TIMEOUT_MS), checked once a second. After a gap that long between two turns, the adapter waits for that check,
# so the conversation restarts exactly where it would live, and a rerun sends the observer the same prompts.
OBSERVER_IDLE_S = 180
IDLE_CHECK_WAIT_S = 1.5

# Channels whose turns are a conversation with the assistant: everything the device hears or is shown is the prompt.
CONVERSATION = {"typed", "voice", "remote"}
# Channels whose non-assistant content reaches a Claude Code assistant through a tool. The principal's turns there
# are part of the thread they had with someone else, except on `web`, where they talk to the assistant.
TOOLS = {
    "inbound_email": ("mcp__mail__read_message", "message"),
    "inbound_text": ("mcp__messages__read_thread", "thread"),
    "meeting": ("mcp__meetings__get_transcript", "meeting"),
    "web": ("WebFetch", "url"),
}

# The settings claude-mem's `install --provider host` writes for an OpenAI-compatible observer, plus what keeps the
# run local and quiet. Everything else is claude-mem's default.
BASE_SETTINGS = {
    "CLAUDE_MEM_RUNTIME": "worker",
    "CLAUDE_MEM_PROVIDER": "openrouter",
    "CLAUDE_MEM_OPENROUTER_API_KEY": "host-observer-local",
    "CLAUDE_MEM_CLOUD_SYNC_HUB_URL": "",
    "CLAUDE_MEM_CLOUD_SYNC_TOKEN": "",
}


# A timeline row of claude-mem's agent-facing context block (src/services/context/formatters/AgentFormatter.ts).
OBS_ROW = re.compile(r"^(?:(S\d+) .*\(.*\)|(?:\*\*)?(\d+)(?:\*\*)? (?:\d{1,2}:\d{2}[ap]|\") .*)$", re.M)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def turn_events(session):
    """A session as Claude Code turns: [(kind, text, ts)], kind prompt | tool | reply, consecutive turns of one kind
    merged. tool events carry (tool_name, input_key) in place of nothing, see TOOLS."""
    channel = session["channel"]
    events = []
    for t in session["turns"]:
        sp = t["speaker"]
        name = t.get("speaker_name", sp)
        if sp == "assistant":
            kind, line = "reply", t["text"]
        elif channel in CONVERSATION or (channel == "web" and sp == "principal") or channel not in TOOLS:
            kind, line = "prompt", t["text"] if sp == "principal" else f"{name}: {t['text']}"
        else:
            kind, line = "tool", f"{name}: {t['text']}"
        if events and events[-1][0] == kind:
            k, text, when = events[-1]
            events[-1] = (k, text + ("\n\n" if kind == "reply" else "\n") + line, when)
        else:
            events.append((kind, line, t["ts"]))
    return events


class ClaudeMemAdapter(Adapter):
    name = "claude_mem"

    def __init__(self, llm=None, model=DEFAULT_MODEL, home=HOME):
        self.proxy = LLMProxy(llm=llm, model=model)
        self.model = model
        self.home = Path(home)
        self.launcher = self.home / ".local" / "bin" / "claude-mem"
        if not self.launcher.exists():
            raise AdapterError(f"claude-mem is not installed under {self.home}; see harness/adapters/claude_mem.py")
        self.wd = None
        self.now = None
        self.running = False
        self.hook_calls = 0

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def reset(self, workdir):
        self._stop()
        self.wd = Path(workdir).resolve()  # claude-mem runs in the project directory, so no relative paths
        self.data = self.wd / "claude-mem"
        self.project = self.wd / PROJECT
        self.data.mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        # claude-mem names the project after the enclosing git checkout; a repository of its own keeps the name
        # `assistant` wherever the workdir is (inside this repository it would be the checkout's folder name).
        if not (self.project / ".git").exists():
            subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.tmp = self.wd / "tmp"
        self.tmp.mkdir(exist_ok=True)
        self.clock_file = self.wd / "clock"
        self.port = _free_port()
        url = self.proxy.start()
        self.proxy.reset_stats()
        self.hook_calls = 0
        settings = {**BASE_SETTINGS, "CLAUDE_MEM_OPENROUTER_BASE_URL": url, "CLAUDE_MEM_OPENROUTER_MODEL": self.model,
                    "CLAUDE_MEM_WORKER_PORT": str(self.port), "CLAUDE_MEM_DATA_DIR": str(self.data)}
        (self.data / "settings.json").write_text(json.dumps(settings, indent=2))
        self._write_clock(self.now)
        self._start()

    def _env(self):
        bins = f"{self.home}/.local/bin:{self.home}/.bun/bin"
        preload = str(CLOCK_JS)
        return {
            "PATH": f"{bins}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home), "TMPDIR": str(self.tmp), "LANG": "en_US.UTF-8", "TZ": TZ,
            "CLAUDE_MEM_DATA_DIR": str(self.data), "CLAUDE_MEM_WORKER_PORT": str(self.port),
            "CLAUDE_MEM_TELEMETRY": "0", "DO_NOT_TRACK": "1", "CLAUDE_MEM_ONLINE_OPTIN": "false",
            "MEMBENCH_CLOCK_FILE": str(self.clock_file),
            "BUN_OPTIONS": f"--preload {preload}", "NODE_OPTIONS": f"--require {preload}",
        }

    def _run(self, args, stdin=None, timeout=HOOK_TIMEOUT_S, env=None):
        p = subprocess.run([str(self.launcher), *args], input=None if stdin is None else json.dumps(stdin),
                           capture_output=True, text=True, env={**self._env(), **(env or {})}, cwd=str(self.project),
                           timeout=timeout)
        if p.returncode != 0:
            raise AdapterError(f"claude-mem {' '.join(args)} exited {p.returncode}: {p.stderr[-2000:]}")
        return p.stdout

    def _start(self):
        out = self._run(["start"], timeout=300, env={"MEMBENCH_CLOCK_SMOOTH": "1"})  # the worker it spawns lives on
        if '"ready"' not in out:
            raise AdapterError(f"claude-mem worker did not start: {out[-2000:]}")
        self.running = True

    def _stop(self):
        if not self.running:
            return
        try:
            self._run(["stop"], timeout=120)
        finally:
            self.running = False
        # Wait for the port to close and for every claude-mem process (the worker, its Chroma child) to let go of the
        # data directory's databases, so the harness can copy or replace the directory.
        held = [str(p) for p in (self.data / "claude-mem.db", self.data / "chroma" / "chroma.sqlite3")]
        for _ in range(600):
            with socket.socket() as s:
                port_open = s.connect_ex(("127.0.0.1", self.port)) == 0
            holders = subprocess.run(["lsof", "-t", *[p for p in held if os.path.exists(p)]],
                                     capture_output=True, text=True).stdout.split() if not port_open else ["port"]
            if not holders:
                time.sleep(0.5)  # last log lines and markers
                return
            time.sleep(0.1)
        raise AdapterError(f"claude-mem processes still hold {self.data}: {holders}")

    def close(self):
        if self.running:
            self._drain()
        self._stop()

    def reopen(self):
        self._start()

    def set_clock(self, ts):
        self.now = ts
        if self.wd is not None:
            self._write_clock(ts)

    def _write_clock(self, ts):
        if ts is None:
            return
        tmp = self.clock_file.with_suffix(".tmp")
        tmp.write_text(f"{int(ts * 1000)} {int(time.time() * 1000)}")
        os.replace(tmp, self.clock_file)

    # --- worker HTTP ----------------------------------------------------------------------------------------------

    def _http(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _drain(self):
        """Wait until the worker has nothing queued or in flight (hook spool included)."""
        deadline = time.time() + DRAIN_TIMEOUT_S
        quiet = 0
        while time.time() < deadline:
            st = self._http("GET", "/api/processing-status")
            spool = self.data / "state" / "hook-spool"
            spooled = [p for p in spool.glob("*.json")] if spool.exists() else []
            busy = bool(st.get("isProcessing")) or int(st.get("queueDepth") or 0) > 0 or bool(spooled)
            quiet = 0 if busy else quiet + 1
            if quiet >= 3:
                return
            time.sleep(0.2)
        raise AdapterError(f"claude-mem worker still busy after {DRAIN_TIMEOUT_S}s: {st}")

    # --- writing --------------------------------------------------------------------------------------------------

    def _hook(self, event, payload):
        self.hook_calls += 1
        return self._run(["hook", "claude-code", event], stdin=payload)

    def seed(self, facts):
        for f in facts:
            self._http("POST", "/api/memory/save", {"text": f["text"], "cwd": str(self.project)})

    def ingest(self, session):
        sid = session["session_id"]
        base = {"session_id": sid, "cwd": str(self.project), "transcript_path": "", "permission_mode": "default"}
        # idle: when the observer last got work (bench time), and whether its idle check has had its chance since
        idle = {"since": None, "settled": False}

        def at(when):
            t = parse_ts(when)
            self._write_clock(t)
            if idle["since"] is not None and not idle["settled"] and t - idle["since"] >= OBSERVER_IDLE_S:
                time.sleep(IDLE_CHECK_WAIT_S)
                idle["settled"] = True
            return t

        def queued(t):
            self._drain()  # the user reads the reply (or the assistant the tool output) before going on
            idle.update(since=t, settled=False)

        def stop(t):  # Claude Code's Stop fires when the turn's last reply is done
            self._hook("summarize", {**base, "hook_event_name": "Stop", "stop_hook_active": False,
                                     "last_assistant_message": last_reply or ""})
            if last_reply:  # without an assistant message claude-mem skips the summary and queues nothing
                queued(t)

        now = at(session["start_ts"])
        self._hook("context", {**base, "hook_event_name": "SessionStart", "source": "startup"})
        turn_open, last_reply = False, None
        for i, (kind, text, when) in enumerate(turn_events(session)):
            if kind == "prompt" and turn_open:
                stop(now)
            now = at(when)
            if kind == "prompt":
                self._hook("session-init", {**base, "hook_event_name": "UserPromptSubmit", "prompt": text})
                last_reply = None
            elif kind == "tool":
                tool, key = TOOLS[session["channel"]]
                if key == "url":  # Claude Code's WebFetch input and result
                    url = f"https://{text.split(':', 1)[0]}/"
                    tool_input = {"url": url, "prompt": "Read the page."}
                    response = {"url": url, "code": 200, "codeText": "OK", "result": text}
                else:  # an MCP tool's input and its content blocks
                    tool_input = {key: sid}
                    response = [{"type": "text", "text": text}]
                self._hook("observation", {**base, "hook_event_name": "PostToolUse", "tool_name": tool,
                                           "tool_input": tool_input, "tool_response": response,
                                           "tool_use_id": f"toolu_{sid}_{i}"})
                queued(now)
            else:
                last_reply = text
            turn_open = True
        if turn_open:
            stop(now)
        at(session["end_ts"])
        self._hook("session-end", {**base, "hook_event_name": "SessionEnd", "reason": "other"})
        self._drain()
        if self.now is not None:
            self._write_clock(self.now)

    # --- reading --------------------------------------------------------------------------------------------------

    def context(self, prompt, channel, session_id):
        t0 = time.perf_counter()
        base = {"session_id": f"probe-{session_id}", "cwd": str(self.project), "transcript_path": "",
                "permission_mode": "default"}
        out = self._hook("context", {**base, "hook_event_name": "SessionStart", "source": "startup"})
        text = ""
        if out.strip():
            text = (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "")
        ids = self._item_ids(text)
        return ContextResult(text=text, item_ids=ids, latency_ms=(time.perf_counter() - t0) * 1000,
                             tokens=approx_tokens(text))

    def _item_ids(self, text):
        """The stored items the block's timeline shows, one per row: session summaries (`S12 <request> (<time>)`) and
        observations (`123 4:30p <icon> <title>`, or `**123** ...` when shown in full). The last summary's fields,
        when shown, belong to a summary already listed in the timeline."""
        seen = []
        for m in OBS_ROW.finditer(text):
            item = m.group(1) or m.group(2)
            if item not in seen:
                seen.append(item)
        return seen

    def cost(self):
        return {**self.proxy.cost(), "hook_calls": self.hook_calls}

    def dump(self):
        """Every stored observation and summary (for inspection, not used by the harness)."""
        return {"observations": self._http("GET", f"/api/observations?project={PROJECT}&limit=100000"),
                "summaries": self._http("GET", f"/api/summaries?project={PROJECT}&limit=100000")}
