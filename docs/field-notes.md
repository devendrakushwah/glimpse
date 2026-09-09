# Field notes: real-world verification

Every fix in this document was driven by watching actual Claude Code sessions in a
real repo (`partner-outbound-tsln`), not synthetic testing. Each entry below follows the
same shape: what was observed, why it happened, what changed in response, and whether a
later live session confirmed the change actually worked. Timestamps are session-relative
offsets pulled directly from the transcript JSONL files (`~/.claude/projects/.../*.jsonl`
and their `subagents/agent-*.jsonl`), not estimates.

This complements `architecture.md`'s "Known limitations" section, which describes the
same issues from a design perspective. This document is the evidence trail.

## Timeline

| Commit | What happened |
|---|---|
| `18a5cbf` | Pagination observed → deny message + skill tightened |
| `69a8a5b` | Added authoritative "worker ran on \<model\>" reporting (used as evidence below) |
| `dd719f7` | Forking-for-no-benefit observed → deny message + skill tightened |
| `51e1d51` | Serial execution observed → skill updated to recommend `run_in_background` |

Each fix's live-session outcome directly informed the next one — this ran in the order
below, not in parallel investigation.

## 1. Chunked pagination around the size limit

**Observed:** blocked from reading an 862-line file in one call, Claude issued
`Read(offset=1, limit=350)` followed by `Read(offset=350, limit=300)` — two bounded
reads that together reconstructed 649 of the file's 862 lines. A second file (427
lines) got a `Read(offset=1, limit=220)` the same way. Each individual call was
legitimately under the 350-line threshold; the block did nothing, because it was
evaluated per call with no memory of the call before it.

**Root cause:** `check_read`'s bounded-`limit` allowance is a pure function of one call's
`(file_path, offset, limit)`. Nothing tracks how much of a given file has already been
read across multiple calls in the same session.

**Fix (`18a5cbf`):** `redirect_message()` and `skills/bulk-reader/SKILL.md` both now
explicitly name this pattern and say not to do it, narrowing a legitimate bounded re-read
to "verifying a section you already know the line numbers of, right before editing it."
This is prompting only — the underlying per-call gap in `check_read` is unchanged and is
still open, tracked in `architecture.md`.

**Re-verified:** in both later sessions checked (the forking session and the two direct
sessions after it), **zero** bounded `Read` calls were made against any of the blocked
files — not even a legitimate single targeted re-read. Claude went straight to
`glimpse.py read` every time. No chunking attempts recurred.

## 2. Forking before calling the skill

**Observed:** asked to summarize three files (862, 427, 434 lines), Claude spawned three
`fork` subagents — one per file — each of which called `glimpse.py read` internally.
Pulling each fork's own transcript (`subagents/agent-*.jsonl`) and summing `usage` fields
across all its turns:

| Fork | Model | Cache-read input tokens | Fresh output tokens |
|---|---|---|---|
| handler.ts | `claude-sonnet-5` | 914,868 | 3,514 |
| utils.ts | `claude-sonnet-5` | 872,934 | 2,652 |
| awaiting-split-validation.ts | `claude-sonnet-5` | 848,836 | 4,779 |

Each fork's `.meta.json` confirms `"model":"inherit"` — it runs on the parent
conversation's model (Sonnet here), not the haiku worker.

**What this ruled out:** the hook still fired correctly inside every fork — each fork's
own `Read` on its oversized file was denied exactly like it would be in the main
conversation, confirmed directly in the transcripts. And thanks to the model-reporting
fix from `69a8a5b`, each fork's `glimpse.py` call showed `worker ran on
claude-haiku-4-5-20251001` — proof the raw file content only ever reached the cheap
worker, never the fork's own (expensive) model. So the earlier worry — that forking might
let a full file slip into an expensive model's context through a side door — didn't
happen.

**Root cause:** a `fork` (unlike a plain subagent) inherits the *entire* parent
conversation as its starting context, unconditionally. For a self-contained task like
"read this one file and report its structure," that inherited history is pure overhead —
these three tasks needed none of it, and paid for the whole conversation's cache-read
cost three times over regardless.

**Fix (`dd719f7`):** `redirect_message()` and `skills/bulk-reader/SKILL.md` both now say
to call the skill directly rather than forking first. No hook-level enforcement exists or
is planned — a `PreToolUse` hook on the `Agent` tool would have to guess a fork's purpose
from its description before it runs, which is unreliable and risks blocking forks that
have nothing to do with file reads.

**Re-verified:** the very next session that touched large files in this repo made **no**
forks at all — `glimpse.py` was called directly from the main conversation both times.

## 3. Serial execution once forking stopped

**Observed:** with forking no longer happening (fix #2 working), a session needing three
files answered called `glimpse.py read` three separate times via plain `Bash` calls, all
issued within 6 seconds of each other:

| | Offset from call 1 |
|---|---|
| Call 1 / Call 2 / Call 3 issued | +0.0s / +3.8s / +6.0s |
| Result 1 / 2 / 3 landed | +30.7s / +77.1s / +105.4s |

The gaps between consecutive results (46.4s, then 28.3s) are close to individual call
durations stacking on top of each other, not clustering together the way concurrent
processes finishing around the same time would. Total wall time (~105s) tracks the *sum*
of three sequential calls, not the *max* of three parallel ones.

**Root cause:** confirmed against the current tool docs — `Bash` is synchronous by
default. Concurrency requires `run_in_background: true` on each call; none of these three
had it set, so Claude Code ran them one after another even though all three were
requested in quick succession.

**Fix (`51e1d51`):** `skills/bulk-reader/SKILL.md` now recommends `run_in_background:
true` for multiple independent files, instead of either forking (fix #2 says don't) or
calling them in sequence. Each `glimpse.py read` invocation is a fully independent
`claude -p` subprocess with no shared state, so backgrounding several at once is safe.

**Re-verified:** the next session needing two files set `run_in_background: true` on
both `Bash` calls:

| | Offset from call 1 |
|---|---|
| Call 1 / Call 2 issued | +0.0s / +3.3s |
| Result 1 / 2 landed | +20.3s / +37.9s |

Individual durations were 20.3s and 34.5s. Run serially, call 2 (whose own 34.5s could
only start once call 1's 20.3s finished) would have landed around **+54.8s**. It actually
landed at **+37.9s** — about 17 seconds faster than serial execution could produce,
which only happens if the two subprocesses genuinely overlapped.

## What this leaves open

- Pagination is mitigated by prompting, not closed. An agent that decides chunking serves
  its task better can still do it — see `architecture.md`'s "Known limitations" for the
  session-scoped cumulative-tracking fix that would actually close it.
- Forking-for-no-benefit and serial execution are both prompting-only guidance too, with
  exactly one confirming live session each so far. Neither has been stress-tested against
  a session that ignores the guidance on purpose, or against a much larger batch of files
  at once.
