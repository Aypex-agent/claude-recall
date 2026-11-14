# claude-recall

Verbatim memory for [Claude Code](https://claude.com/claude-code). It has two parts:

1. **`recall` MCP server**: full-text search over your past Claude Code session transcripts, plus a
   reader that returns the exact conversation around any point. It works like claude.ai's chat search.
2. **Memory hook**: every time Claude writes or edits an auto-memory file, the hook
   - stamps a transcript **anchor** into the memory's frontmatter, so any later reader can jump to the
     exact moment the memory was written and read what was actually said;
   - runs a **cold read**: a fresh Haiku with no context reads the memory as a future session would,
     and reports what it understood and what it couldn't resolve. The report goes back to the agent
     that wrote the memory, which still has the context to fix the gaps.

The cold read targets *authorship bias*. A writer leaves out assumptions the reader doesn't share, so
a note can mean something different to someone without context. Here, the reviewer is exactly
that reader.

Python standard library only, apart from the `mcp` package, which `uv` installs for the server on first run.

## How it works

**Anchors.** Each memory gets entries like these:

```yaml
metadata:
  type: project
  sources:
    - 5c552485-…:toolu_01Fzx9d1SYyYPHzniK28pnz8   # <sessionId>:<tool_use_id> of the write
```

`read_transcript("<sessionId>:<toolu_id>")` returns the turns around that write. Edits add more
entries, so each revision stays traceable.

**Index.** A SQLite FTS5 database (`index.db`, using the porter stemmer and bm25 ranking). It updates
incrementally on every query by reading only the bytes appended to each transcript since the last read,
so there is no daemon. Rows outlive Claude Code's transcript cleanup.

It indexes user prompts, assistant text, and one-line summaries of tool calls. It skips tool
results, thinking blocks and API request records. Subagent transcripts are indexed and tagged.

**Cold read.** `claude -p --safe-mode --model haiku --tools ""` runs from a temporary directory with no
hooks, MCP servers, CLAUDE.md or memory, and with session persistence turned off. It returns a READING,
up to five GAPS, and a VERDICT (PASS or REVISE). Each file gets at most 2 reviews per session, which
avoids revise/review loops. Failures never block a write.

## MCP tools

| Tool | What it does |
|------|--------------|
| `search_transcripts(query, project?, since?, limit?, include_subagents?)` | Ranked hits with snippet, session title, date and `anchor` |
| `read_transcript(anchor, before?, after?, include_tools?, max_chars?)` | Verbatim turns around an anchor (`session:uuid`, `session:toolu_…`, or a bare session id) |
| `list_sessions(project?, limit?)` | Recent sessions, newest first |

The server's `instructions` tell every session that memory anchors exist and when to follow them.

## Install

Requires `python3` (3.11+), [`uv`](https://docs.astral.sh/uv/), and the `claude` CLI on `PATH`.

```sh
git clone https://github.com/Aypex-agent/claude-recall ~/.claude/recall
claude mcp add -s user recall -- uv run --quiet --script ~/.claude/recall/server.py
```

Then add the following to `~/.claude/settings.json`. `cleanupPeriodDays` is optional; it raises Claude
Code's default transcript retention from 30 days to 10 years.

```json
{
  "cleanupPeriodDays": 3650,
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/recall/memory_hook.py",
            "timeout": 120,
            "statusMessage": "recall: stamping + cold-reading memory"
          }
        ]
      }
    ]
  }
}
```

The MCP tools appear in the next session you start.

## Try it without Claude

```sh
python3 ~/.claude/recall/recall_core.py index            # build or refresh the index
python3 ~/.claude/recall/recall_core.py search hook error
python3 ~/.claude/recall/recall_core.py read '<sessionId>:<uuid-or-toolu_id>'
python3 ~/.claude/recall/recall_core.py sessions
```

## Tuning

At the top of `memory_hook.py`:

- `REVIEW_PATTERNS`: path regexes that get stamped and cold-read. The default covers
  `~/.claude/projects/*/memory/*.md`. Add patterns to cover plans or handoff docs.
- `MAX_REVIEWS_PER_FILE_PER_SESSION` (default 2) and `REVIEW_TIMEOUT` (default 100 s).

Environment: `RECALL_DB` relocates the index. `CLAUDE_CONFIG_DIR` is respected.

## Caveats

- Claude Code's transcript JSONL format is internal and can change between versions. The parser skips
  records it doesn't recognise. If search ever comes back empty after an update, check the parser first.
- Each memory write costs one Haiku call, and the session waits about 7 seconds for it.
- The hook rewrites the memory file after Claude's write. Claude Code notices the change on disk and
  carries on.
- `index.db` holds your transcript text. It is gitignored here; keep it that way.
