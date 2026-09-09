# Architecture

This document explains how `glimpse` intercepts a read, what runs the
delegated work, and the process-lifecycle guarantees around that worker.
It assumes you've read the README's "what it actually does" section.

## Component map

```
.claude-plugin/plugin.json   plugin manifest (name, version, description)
hooks/hooks.json              wires PreToolUse -> scripts/glimpse.py hook
scripts/glimpse.py            all logic: hook decisions + worker invocation
skills/bulk-reader/SKILL.md   tells Claude when/how to call `glimpse.py read`
tests/test_glimpse.py         offline unit tests, one non-mocked process test
```

One file carries the actual logic. Splitting the hook logic, the worker
invocation, and the CLI plumbing into separate modules would cost three
files and buy nothing — nothing else imports this code, and it's under 400
lines total.

## Request flow

```
Claude wants to read a 900-line file
        |
        v
PreToolUse hook fires (matcher: Read or Bash)
        |
        v
check_read() / check_bash() in glimpse.py
        |
        +-- file <= threshold, or a bounded `limit` --> allow, Read proceeds normally
        |
        +-- broad read --> deny, with a message naming the exact
        |                   `glimpse.py read` invocation to use
        v
Claude either:
  - finds a cheaper native path (grep, then a bounded re-read of the matching
    section only), or
  - invokes the bulk-reader skill
        |
        v
glimpse.py read --question "..." --paths a.py b.py
        |
        v
read_main() loads the files itself (not Claude), wraps each in
<file path="..."> tags with line numbers, builds one message
        |
        v
invoke_worker() launches `claude -p --model haiku --tools ""
--no-session-persistence --max-turns 1 --safe-mode
--system-prompt <reader prompt>`, corpus + question on stdin
        |
        v
worker answers, exits; invoke_worker() parses its --output-format json
response, extracts `result`
        |
        v
read_main() prints only that answer to stdout
        |
        v
Claude's context gains the answer. The file contents existed only in the
Python process's memory and the worker's stdin — never in Claude's context.
```

The hook enforces the boundary; it does not force delegation. In practice,
when a broad read is denied, Claude sometimes finds a cheaper path on its
own (`grep` for a specific pattern, then a bounded re-read of the matching
lines) instead of calling the skill. That's fine — the goal is keeping large
file contents out of Claude's context, not routing every blocked read
through the worker specifically.

## Hook decision logic

Both hooks return the current, non-deprecated `hookSpecificOutput` /
`permissionDecision` shape (`allow` or `deny`), not the deprecated top-level
`decision` field.

**`check_read`** looks at `tool_input.file_path`, `offset`, and `limit`:

- Missing path, or file doesn't exist: allow. Let `Read` produce its own
  error rather than guessing.
- `limit` is set, is a positive number, and is `<= GLIMPSE_MIN_LINES`: allow.
  This is the only thing that counts as a targeted read.
- Otherwise, count the file's lines and bytes. Allow if both are under
  threshold; deny otherwise, with a message that includes the actual line
  count, byte count, and the exact command to run instead.

`offset` alone does **not** count as targeted, on purpose. A `Read` with only
`offset` set can still stream to end of file on a huge file — that's
precisely the read this hook exists to catch. Upstream `shunt`'s hook allows
any read with `offset` or `limit` present, which is also why its own test
suite documents `offset:0` and `limit:0` as "known bypass"; this
implementation closes that by requiring a positive, bounded `limit`
specifically.

The byte ceiling (`GLIMPSE_MAX_BYTES`) exists because line count alone is
blind to a file that's one enormous line — minified JS, a single huge JSON
blob. A 500KB file with no newlines passes a line-count-only check; it does
not pass this one.

**`check_bash`** looks for `cat`, `head`, `tail`, `less`, `more` in the
command:

- Any `|` or `>` anywhere in the command string allows it through. This is a
  heuristic, not a parser — see "Non-goals" below.
- Otherwise, `shlex.split` the command and walk the tokens after the command
  name. A token starting with `-` is a flag; if it's one of the flags known
  to consume a following value (`head -n`, `head -c`, `tail -n`, `tail -c`),
  the next token is skipped too. Everything else is a candidate file path.
- Every candidate path that exists gets checked against the same line/byte
  thresholds as `check_read`. If *any* of them is over threshold, the whole
  command is denied.

Two things this fixes relative to naive parsing: `head -n 800 file.txt`
doesn't mistake `800` for a second file path (the flag-value table skips it),
and `cat small.txt large.txt` doesn't stop checking after the first operand.

## The worker

### Why a separate `claude -p` process instead of a Claude Code subagent

A custom subagent shares the parent session's plugin/hook/skill loading
unless carefully isolated, and its transcript persists by default. A
separate `claude -p` invocation gives explicit, one-line control over all of
that: `--safe-mode` skips this plugin's own hooks and every other
customization, `--tools ""` removes every built-in tool so it can't act on
anything but the text it was handed, and `--no-session-persistence` means
nothing is written to `~/.claude/projects/`. `--max-turns 1` bounds it to
exactly one exchange. The tradeoff is that the worker can't use `Read`,
`Grep`, or any other tool to explore beyond what's in its prompt — which is
the point: the corpus is handed to it explicitly, not discovered by it.

### Process lifecycle guarantees

`subprocess.run(..., timeout=N)` looks like it should be enough: on timeout,
Python's own implementation calls `process.kill()` then `process.wait()`
before raising, so the direct child is confirmed dead by the time your code
sees the exception. That's true and is not the gap.

The gap is `process.kill()` signals exactly one PID — the direct child. If
that child had already forked a grandchild before being killed, the
grandchild is orphaned, not terminated; nothing about `subprocess.run`'s
timeout handling reaches it. This was verified directly: a `bash -c 'sleep &
...; sleep'` process killed via the naive approach left its backgrounded
`sleep` running.

`invoke_worker()` avoids this by starting the worker in its own process
group (`start_new_session=True` on POSIX) and killing the whole group
(`os.killpg`) rather than relying on `Popen.kill()`, on timeout, on any other
exception, and again as a backstop in a `finally` block if the process is
somehow still alive by the time that runs. `tests/test_glimpse.py`'s
`ProcessGroupKillTests` exercises this against a real process tree, not a
mock — the same bash/sleep scenario above, asserting the grandchild is
actually gone afterward.

In practice `--tools ""` already prevents the worker from spawning shells or
subagents through its own tool use, so this defends against something the
CLI's own internals might do independent of the tools you gave it, not
against a scenario this plugin's configuration invites. It's a small amount
of code for removing an assumption rather than resting on one.

### Response handling

The worker runs with `--output-format json`, so the response is a single
JSON object with a `result` field (the answer) and `is_error`. `invoke_worker`
treats a non-zero exit code, unparseable stdout, `is_error: true`, and an
empty `result` as four distinct failure modes, each surfaced with a specific
message rather than a generic "something went wrong" — a stale worker
response should never look like a successful summary.

## Known limitations (tracked, not yet fixed)

See `field-notes.md` for the transcript evidence behind each of the three
items below — exact timestamps, token counts, and which later sessions
confirmed each fix actually changed behavior.

- **A blocked file can still be fully read via repeated bounded calls.**
  `check_read`'s bounded-`limit` allowance is evaluated per call, with no
  memory of earlier calls. Nothing stops `Read(offset=1, limit=350)` followed
  by `Read(offset=350, limit=300)` and so on until the whole file has been
  paginated into context piece by piece — which delivers exactly the content
  the block exists to keep out, just spread across more tool calls. Observed
  in practice against an 862-line file: two bounded reads reconstructed 649
  of its 862 lines before stopping.

  Current mitigation is prompting only: `redirect_message()` and
  `skills/bulk-reader/SKILL.md` both now explicitly name this pattern and
  tell Claude not to do it, narrowing the legitimate use of a bounded
  re-read to "verifying a section you already know the line numbers of,
  right before editing it." This measurably changes behavior but doesn't
  make the pagination path infeasible — an agent that decides pagination
  serves its task better (e.g. because it wants exact code, not a worker's
  summary, for something like debugging) can still do it.

  The real fix needs session-scoped state: every hook call carries a stable
  `session_id`, so `check_read` can track cumulative lines allowed per file
  per session and deny once that total crosses the same threshold a single
  call would have. Not yet implemented.

- **Forking before calling the skill adds overhead the fork gets no benefit
  from.** The hook fires correctly inside forked subagents (verified: a
  fork's own `Read` on an oversized file is denied exactly like the main
  conversation's), and the raw file content only ever reaches the cheap
  worker either way. But a `fork` (as opposed to a plain subagent) inherits
  the entire parent conversation as its starting context. Observed directly
  in a real session: three forks spawned purely to call this skill on three
  files each carried 850K-915K cache-read tokens of prior conversation
  history, plus 2,600-4,800 tokens of the fork's *own* full-price reasoning
  at the parent's model (Sonnet, not the haiku worker) — for a task that
  needed none of that history. Nothing about this plugin's design asked for
  a fork; the main model chose to spawn one before ever attempting a `Read`
  on the file.

  Same category of fix as the pagination issue above: prompting only.
  `redirect_message()` and `skills/bulk-reader/SKILL.md` both now tell
  Claude to call the skill directly rather than forking first. There is no
  hook-level enforcement for this and it's a harder problem than the read
  hooks: a `PreToolUse` hook matching the `Agent` tool would have to guess
  the *purpose* of a fork from its task description before it runs, which
  is unreliable and risks blocking forks that have nothing to do with file
  reads. Not attempted.

- **Calling the skill directly (no fork) loses the concurrency forking got
  for free.** Observed in a separate real session: three `glimpse.py read`
  calls for three different files, issued within 6 seconds of each other,
  landed 30.7s / 77.1s / 105.4s after the first call — consecutive gaps of
  46.4s and 28.3s, summing close to what three sequential calls would take
  rather than clustering near the slowest one. `Bash` is synchronous by
  default; without `run_in_background: true` on each call, Claude Code runs
  them one after another even if Claude requested all three up front. A
  fork gets concurrency because forks run in the background by default —
  but pays the inherited-conversation cost documented above for it.

  `skills/bulk-reader/SKILL.md` now tells Claude to background each call
  (`run_in_background: true`) when it needs several files answered
  independently, instead of either forking or calling them in sequence:
  each `glimpse.py read` invocation is a fully independent `claude -p`
  subprocess with no shared state, so backgrounding them is safe and gets
  the same wall-clock benefit as forking without a fork's overhead. Not
  yet re-verified against a live session with this guidance in place.

## Non-goals

- **Not a security boundary.** The bash-command parser is `shlex` plus a
  small flag table, not a shell grammar. A command shape it doesn't
  recognize can still get a large file's contents into context. If that
  needs to be airtight, replace the denylist approach with an allowlist of
  specific bash subcommands.
- **No code generation.** Upstream `shunt` has a second mode (`code-writer`)
  that generates boilerplate and writes it straight to disk. This plugin
  only handles reads; writing generated code without Claude seeing it is a
  different review problem and wasn't part of this port.
- **No editing or debugging delegation.** The worker extracts facts from
  exactly the files it's handed; it has no visibility into the rest of the
  codebase and shouldn't be asked to reason about architecture or bugs.
  Claude still needs to read exact lines directly before making an edit —
  the worker's line numbers are for navigation, not guaranteed edit-safe
  coordinates.
- **No cross-call memory.** Every `glimpse.py read` call is independent by
  design, matching upstream `shunt`'s reasoning: the point of keeping files
  out of Claude's context is defeated if the plugin has to replay them from
  somewhere to maintain state. A follow-up question re-sends the same
  `--paths`; that cost is paid by the worker call, not by Claude's context.
