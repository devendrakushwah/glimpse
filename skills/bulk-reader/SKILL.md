---
name: bulk-reader
description: "Delegate reading or understanding large files to a cheap ephemeral worker model. Use this whenever a Read is blocked for size, or a question spans 3+ files. This is the default path for understanding a large file, not a bounded re-read."
---

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/glimpse.py" read --question "<question>" --paths <file1> [<file2> ...]
```

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

The worker cannot browse, edit, or run commands, and never sees your conversation. It
only sees the files you pass and the question you ask.
