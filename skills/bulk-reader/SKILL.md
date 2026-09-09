---
name: bulk-reader
description: "Delegate reading or understanding large files to a cheap ephemeral worker model. Use this whenever a Read is blocked for size, or a question spans 3+ files. This is the default path for understanding a large file, not a bounded re-read. Call directly, not from a forked subagent -- a fork inherits the whole conversation for no benefit here."
---

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/glimpse.py" read --question "<question>" --paths <file1> [<file2> ...]
```

Call this directly from whatever context you're already in — **do not fork or spawn a
subagent just to call it.** This skill already keeps the raw file out of context on its
own, whether you call it from the main conversation or a subagent, so forking first adds
nothing: a fork inherits your entire conversation history and pays for it as cache-read
tokens on every one of its own turns, then reasons at the same model and price as the
conversation it forked from — all to do a job that needed none of that history. If you
already know a file is large before attempting to read it (e.g. from `wc -l` or a
directory listing), call this skill directly instead of forking first and discovering the
block from inside the fork.

If you still end up wanting a subagent for this — e.g. to structure several file
questions rather than raw backgrounded `Bash` calls — use a **plain subagent**
(`general-purpose` or similar), not a fork. A plain subagent starts with a fresh, empty
context: no inherited conversation, nothing to pay cache-read on. A fork inherits
everything on purpose, which is the right call when a task genuinely needs your shared
context — reading one file and reporting its structure doesn't.

Ask a specific question — "which methods write to the database?", not "summarize this
file". The worker reads the files; you only see its answer, so pass every file the
question needs in one call.

**Do not read a blocked file in bounded chunks instead** — `limit=350`, then `limit=350`
again for the next section, and so on until you've pieced the whole file back together.
That puts exactly as much content in your context as the full read this block exists to
prevent, just spread across more tool calls. If you're trying to understand or explore a
file, use this skill. A bounded `Read` (`offset`/`limit`) is for one thing only:
re-verifying the exact content of a specific section you already know the line numbers
of, immediately before editing it — it is not a substitute for reading a file you haven't
seen yet.

Each call is independent and stateless. For a follow-up, call again with the same
`--paths` — the files never entered your context, so re-sending them costs you nothing
here.

**Need this for several files with different questions?** Don't call it once per file in
sequence, and don't fork to parallelize it either — set `run_in_background: true` on each
`Bash` call instead. Each call is an independent `claude -p` subprocess with no shared
state, so they run correctly in parallel with no extra setup. This gets you the same wall
time as forking would, without a fork's overhead: no inherited conversation, no extra
model cost, just N ordinary background commands you check on once they're done — a serial
run of three ~30-45s calls takes 90-135s total, backgrounded they all land around the
time of the slowest one.

The worker cannot browse, edit, or run commands, and never sees your conversation. It
only sees the files you pass and the question you ask.
