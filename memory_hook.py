#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit|MultiEdit) for auto-memory files.

1. Stamps a transcript anchor (<session_id>:<tool_use_id>) into the memory's frontmatter
   under metadata.sources, so any reader can jump to the verbatim moment it was written
   (recall MCP: read_transcript).
2. Runs a cold-read review: a no-context Haiku (`claude -p --safe-mode`) reads the memory as a
   future agent would and reports its reading + gaps. The result goes back to the authoring agent
   via additionalContext, while it still has the context to fix them (authorship-bias check).

Never blocks; any failure degrades to "stamped only" or a silent exit.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Files that get stamped + reviewed. Add patterns (e.g. plans, handoff docs) here.
REVIEW_PATTERNS = [
    re.compile(r"/\.claude/projects/[^/]+/memory/[^/]+\.md$"),
]
SKIP_NAMES = {"MEMORY.md"}
MAX_REVIEWS_PER_FILE_PER_SESSION = 2
REVIEW_TIMEOUT = 100
STATE = Path.home() / ".claude" / "recall" / "state.json"

PROMPT = """\
Below is a note from the memory system of an AI coding assistant (Claude Code). Notes like this are \
loaded into future sessions where the reader has NO access to the conversation that produced it, only \
this note and its one-line index entry. You are that future reader, reading cold.

1. READING: In 1-2 sentences, say what you believe this note tells you and how it would change what you do.
2. GAPS: List what a cold reader cannot resolve: undefined referents (names, "this", "the bug", "that \
setup"), unstated assumptions, jargon, claims that could be read more than one way, and anything that \
could steer a future agent wrong if taken literally. Quote the phrase. At most 5 items, most important \
first. No nitpicks; write "none" if there are none.
3. VERDICT: PASS if a cold reader would understand and act on it as intended, otherwise REVISE.

Ignore `sources:` entries; they are transcript pointers, not content.

Index entry: {index_line}

--- NOTE ({name}) ---
{body}
--- END NOTE ---
"""


def out(ctx):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx}}))
    sys.exit(0)


def stamp(text, anchor):
    """Insert anchor into frontmatter metadata.sources. Returns (new_text, changed)."""
    if anchor in text:
        return text, False
    entry = f"    - {anchor}"
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return f"---\nmetadata:\n  sources:\n{entry}\n---\n\n" + text, True
    fm = m.group(1).split("\n")
    try:
        mi = next(i for i, l in enumerate(fm) if re.match(r"metadata:\s*$", l))
    except StopIteration:
        fm += ["metadata:", "  sources:", entry]
    else:
        end = mi + 1
        while end < len(fm) and (fm[end].startswith(" ") or not fm[end].strip()):
            end += 1
        si = next((i for i in range(mi + 1, end) if re.match(r"\s+sources:\s*$", fm[i])), None)
        if si is None:
            fm[end:end] = ["  sources:", entry]
        else:
            j = si + 1
            while j < end and re.match(r"\s+- ", fm[j]):
                j += 1
            fm.insert(j, entry)
    return "---\n" + "\n".join(fm) + "\n---\n" + text[m.end():], True


def bump_count(key):
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    n = st.get(key, 0) + 1
    st[key] = n
    if len(st) > 1000:
        st = dict(list(st.items())[-500:])
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)  # data dir, separate from the code under a plugin install
        STATE.write_text(json.dumps(st))
    except Exception:
        pass
    return n


def index_line(path):
    idx = path.parent / "MEMORY.md"
    try:
        for line in idx.read_text().splitlines():
            if f"({path.name})" in line:
                return line.strip()
    except Exception:
        pass
    return "(no index entry found)"


def review(path, body):
    claude = shutil.which("claude") or str(Path.home() / ".local/share/mise/shims/claude")
    no_sources = re.sub(r"\n\s+sources:\s*\n(\s+- .*\n?)+", "\n", body)
    prompt = PROMPT.format(index_line=index_line(path), name=path.name, body=no_sources)
    env = dict(os.environ, RECALL_REVIEWER="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    with tempfile.TemporaryDirectory(prefix="recall-coldread-") as tmp:
        r = subprocess.run(
            [claude, "-p", "--safe-mode", "--model", "haiku", "--tools", "", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, timeout=REVIEW_TIMEOUT, cwd=tmp, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError((r.stderr or r.stdout or f"exit {r.returncode}").strip()[:300])
    return r.stdout.strip()


def main():
    if os.environ.get("RECALL_REVIEWER") == "1":
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    fp = (data.get("tool_input") or {}).get("file_path") or ""
    path = Path(fp)
    if path.name in SKIP_NAMES or not any(p.search(fp) for p in REVIEW_PATTERNS) or not path.is_file():
        sys.exit(0)

    session, toolu = data.get("session_id", "?"), data.get("tool_use_id", "?")
    anchor = f"{session}:{toolu}"
    text = path.read_text()
    new, changed = stamp(text, anchor)
    if changed:
        path.write_text(new)
    head = (f"[recall] Stamped transcript anchor `{anchor}` into {path.name} (metadata.sources)"
            f"{'; the file changed on disk, so re-read it before editing again' if changed else ''}.")

    n = bump_count(f"{session}|{fp}")
    if n > MAX_REVIEWS_PER_FILE_PER_SESSION:
        out(head + f" Cold-read skipped (already reviewed {n - 1}x this session).")
    try:
        verdict = review(path, new)
    except Exception as e:  # noqa: BLE001
        out(head + f" Cold-read review failed ({type(e).__name__}: {e}); the memory is saved as written.")

    passed = re.search(r"VERDICT:?\**\s*PASS", verdict, re.I) is not None
    tail = ("It passed; check that its READING matches what you meant." if passed else
            "If its READING isn't what you meant, or a GAP would mislead a future agent, revise the memory now: "
            "you still have the context it lacks. Don't pad it to satisfy nitpicks.")
    out(f"{head}\n[recall cold-read] A no-context Haiku reader reviewed {path.name}:\n{verdict}\n\n{tail}")


if __name__ == "__main__":
    main()
