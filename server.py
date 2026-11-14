#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""Recall MCP server: verbatim search over Claude Code session transcripts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_core as core  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

INSTRUCTIONS = """\
Recall gives verbatim access to past Claude Code sessions (transcripts are kept ~10 years and stay \
searchable in the index even after cleanup).
- Auto-memory files carry `source:`/`sources:` anchors in their frontmatter (`<sessionId>:<toolu_id>`), \
pointing at the exact moment the memory was written. When a memory is ambiguous, seems stale, or you need \
the reasoning behind it, call read_transcript(anchor) and read the surrounding conversation before acting on it. \
The user's own words there outrank the memory's paraphrase.
- To recall something from earlier sessions ("what did we decide about X", "last time we..."), use \
search_transcripts, then read_transcript on the best hit's anchor.
"""

mcp = FastMCP("recall", instructions=INSTRUCTIONS)


@mcp.tool()
def search_transcripts(query: str, project: str | None = None, since: str | None = None,
                       limit: int = 10, include_subagents: bool = False) -> list[dict]:
    """Full-text search (bm25, porter-stemmed) over past session transcripts: user prompts and assistant replies.

    query: words to find (all words first, falls back to any word).
    project: substring of the project dir name, e.g. "Work". since: ISO date like "2026-11-01".
    Returns hits with `anchor` (pass to read_transcript), date, role, session title, and a snippet (>>match<<).
    """
    return core.search(query, project=project, since=since, limit=limit, include_subagents=include_subagents)


@mcp.tool()
def read_transcript(anchor: str, before: int = 6, after: int = 6, include_tools: bool = False,
                    max_chars: int = 6000) -> dict:
    """Return the verbatim conversation around an anchor.

    anchor: "<sessionId>:<uuid>" (from search hits), "<sessionId>:<toolu_id>" (from memory `source:` fields),
    or a bare "<sessionId>" (start of that session).
    before/after: number of user/assistant messages either side. include_tools: also show tool-call summaries.
    The target message is marked is_target=true.
    """
    res = core.read_transcript(anchor, before=before, after=after, include_tools=include_tools, max_chars=max_chars)
    return res or {"error": f"anchor not found: {anchor}"}


@mcp.tool()
def list_sessions(project: str | None = None, limit: int = 20) -> list[dict]:
    """List recent sessions (newest first) with titles and dates. Pass a session id to read_transcript to open one."""
    return core.list_sessions(project=project, limit=limit)


if __name__ == "__main__":
    mcp.run()
