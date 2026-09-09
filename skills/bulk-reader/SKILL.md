---
name: bulk-reader
description: "Delegate reading large files to a cheap ephemeral worker model. Use when a Read is blocked, or a question spans 3+ files."
---

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/glimpse.py" read --question "<question>" --paths <file1> [<file2> ...]
```

Ask a specific question — "which methods write to the database?", not "summarize this file".
The worker reads the files; you only see its answer, so pass every file the question needs in one call.

Each call is independent and stateless. For a follow-up, call again with the same `--paths` —
the files never entered your context, so re-sending them costs you nothing here.

The worker cannot browse, edit, or run commands, and never sees your conversation. It only sees
the files you pass and the question you ask.

Before editing code based on the answer, verify the exact lines with a bounded `Read`
(`limit` <= the threshold) — the worker's line numbers are for reference, not guaranteed precise.
