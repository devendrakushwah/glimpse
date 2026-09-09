# glimpse

💸 Stop burning expensive model tokens just to read big files — let a cheap
model do it. Every time Claude Code reads a large file straight into context,
you're paying frontier-model rates for what's usually just a lookup. Glimpse
cuts that bill by keeping large files out of Claude's context entirely: a
PreToolUse hook denies the read and routes it to a disposable, cheap worker
instead. Claude gets an answer to a specific question back; it never sees
the file contents.

The idea comes from Spotify's [`shunt`](https://github.com/spotify/portal-ai-plugins/tree/main/plugins/shunt)
plugin, which does the same thing through Spotify's internal Portal/AiKA
platform. This is an independent implementation of the same idea against the
`claude` CLI directly — no Portal, no MCP server, no extra services. The only
requirements are Python 3 and a working `claude` install.

## What it actually does

Claude Code fires a hook before every `Read` and `Bash` call. If a `Read`
targets a file over 350 lines (or 200KB) without a bounded `limit`, or a
`cat`/`head`/`tail`/`less`/`more` command targets one, the hook denies the
call and tells Claude to use the `bulk-reader` skill instead.

That skill runs `scripts/glimpse.py read`, which:

1. Reads the requested files itself, locally, with line numbers attached.
2. Sends them plus your question to a second `claude -p` process — no tools,
   no session, no memory, model pinned to `haiku` by default.
3. Prints only that worker's answer.

The worker is a fresh process every time. It can't browse the filesystem,
run commands, or see anything from your session besides the text you handed
it. When it's done answering, it exits; if it hangs, it and everything it
spawned get killed on a timeout (see `docs/architecture.md` for why that
needed more than the default `subprocess.run` timeout).

Claude still does normal targeted reads for anything under the threshold, and
still reads exact sections directly before editing — this only intercepts
the "read the whole file to understand it" case.

## Setup

Requirements: Python 3, `claude` CLI on `PATH` and already authenticated.
Nothing else to install.

There's no `claude plugin install` for a plain git repo like this one (that
command expects a marketplace entry). Two ways to actually load it:

**Persistent, every session** — symlink it into `~/.claude/skills/`. Any
folder there containing a `.claude-plugin/plugin.json` auto-loads as
`<name>@skills-dir`, no marketplace and no install step:

```bash
git clone https://github.com/devendrakushwah/glimpse.git
ln -s "$(pwd)/glimpse" ~/.claude/skills/glimpse
```

Confirm it loaded: `claude plugin list` should show `glimpse@skills-dir`.

**One session only**, without installing anything:

```bash
claude --plugin-dir /path/to/glimpse
```

## Commands

Once installed, this runs on its own — you don't call anything by hand.
Claude invokes the skill when a read gets blocked. If you want to run it
directly:

```bash
python3 scripts/glimpse.py read \
  --question "Which functions write to the database, and where?" \
  --paths src/service.py src/repository.py
```

`--paths` takes any number of files; they all go to the same worker call.
Ask a specific question — "summarize this file" gets a vague answer back,
"which functions handle retries" gets you exact line references.

The hook itself is `scripts/glimpse.py hook` — it reads Claude's tool-call
JSON on stdin and writes an allow/deny decision to stdout. You won't call
this directly; `hooks/hooks.json` wires it to `PreToolUse` on `Read` and
`Bash`.

## Configuration

Set these in `.claude/settings.json`'s `env` block:

| Variable | Default | Purpose |
|---|---|---|
| `GLIMPSE_MIN_LINES` | `350` | Line count above which a read is blocked |
| `GLIMPSE_MAX_BYTES` | `200000` | Byte-size ceiling — catches huge single-line or minified files the line check would miss |
| `GLIMPSE_MODEL` | `haiku` | Model alias passed to `claude -p --model` for the worker |
| `GLIMPSE_TIMEOUT_SECONDS` | `120` | How long a worker call can run before it's killed |
| `GLIMPSE_MAX_CHARS` | `200000` | Payload ceiling per call (~50k tokens) — split into smaller calls above this |

## Build & test

No build step — it's a stdlib-only Python script, nothing to compile. There
is a test suite:

```bash
python3 -m unittest discover -s tests -v
```

43 tests, all offline: hook routing decisions, JSON edge cases, and worker
error handling with `subprocess.Popen` mocked out. One test is deliberately
not mocked — it spawns a real process tree (a shell with a backgrounded
child) and checks that killing the worker actually kills the whole tree, not
just the process we called `kill()` on.

Nothing here talks to a real model, so the suite runs in well under a
second and costs nothing.

## Architecture

`docs/architecture.md` covers the request flow end to end, why the worker
runs as a separate `claude -p` process instead of a Claude Code subagent,
the process-lifecycle guarantees (and the gap in the naive approach that
made those guarantees necessary), and what this deliberately doesn't try to
do.

## Scope

This handles read delegation only. It does not generate code, edit files, or
make decisions that need reasoning about your whole codebase — the worker
extracts facts from the files you hand it and nothing else. It's also a
cost-routing guardrail, not a security boundary: the bash-command parser is
a heuristic (`shlex` plus a small flag table), not a full shell grammar, and
a command shape it doesn't recognize can still get a large file read past
it.

## License

MIT. Structure and approach adapted from Spotify's `shunt` plugin
([Apache-2.0](https://github.com/spotify/portal-ai-plugins/blob/main/LICENSE));
no code from that repository is reused directly.
