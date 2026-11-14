"""Recall core: incremental SQLite FTS5 index over Claude Code transcripts.

Transcripts: ~/.claude/projects/<project>/<session>.jsonl (main) and
~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl (subagents).
The JSONL format is internal to Claude Code and may change; parsing is defensive
and unknown record types are ignored.

Anchors:  "<sessionId>:<uuid>"  or  "<sessionId>:<toolu_id>"  or bare "<sessionId>".
"""
import json
import os
import re
import sqlite3
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS = CLAUDE_DIR / "projects"
DB_PATH = Path(os.environ.get("RECALL_DB", CLAUDE_DIR / "recall" / "index.db"))
SCHEMA_VERSION = 1
MAX_TEXT = 20000          # per indexed message
TOOL_INPUT_KEYS = ("file_path", "path", "command", "pattern", "query", "url", "description", "prompt", "skill")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT, mtime REAL, offset INT,
    session TEXT, project TEXT, is_sub INT, agent TEXT, line INT);
CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY, project TEXT, title TEXT,
    first_ts TEXT, last_ts TEXT, file TEXT);
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, file TEXT, line INT, session TEXT,
    uuid TEXT, ts TEXT, role TEXT, text TEXT, project TEXT, is_sub INT, agent TEXT);
CREATE INDEX IF NOT EXISTS msgs_uuid ON msgs(uuid);
CREATE INDEX IF NOT EXISTS msgs_file_line ON msgs(file, line);
CREATE TABLE IF NOT EXISTS toolu(tool_use_id TEXT PRIMARY KEY, msg_id INT);
CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts USING fts5(text, content='msgs', content_rowid='id',
    tokenize='porter unicode61');
"""

SYSTEMISH = re.compile(r"^\s*<(system-reminder|task-notification|local-command|command-|agent-message|"
                       r"user-prompt-submit-hook|bash-)", re.S)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    v = db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    if v is None:
        db.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    elif int(v[0]) != SCHEMA_VERSION:
        db.close()
        DB_PATH.unlink()
        return connect()
    db.commit()
    return db


# ---------------------------------------------------------------- parsing

def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_summary(b):
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    bits = []
    for k in TOOL_INPUT_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v:
            v = v.replace("\n", " ")
            bits.append(f"{k}={v[:300]}")
    return f"[tool {b.get('name', '?')}] " + " ".join(bits)


def parse_record(rec):
    """Return (role, text, tool_use_ids) or None if the record isn't worth indexing."""
    t = rec.get("type")
    if t not in ("user", "assistant") or rec.get("isMeta"):
        return None
    msg = rec.get("message") or {}
    texts, toolus = [], []
    for b in _blocks(msg.get("content")):
        bt = b.get("type")
        if bt == "text" and isinstance(b.get("text"), str) and b["text"].strip():
            texts.append(b["text"])
        elif bt == "tool_use":
            texts.append(_tool_summary(b))
            if b.get("id"):
                toolus.append(b["id"])
        # tool_result / thinking / images are skipped: noisy or not verbatim conversation
    if not texts:
        return None
    text = "\n".join(texts)
    role = t
    if t == "user" and SYSTEMISH.match(text):
        role = "system"
    elif t == "assistant" and all(x.startswith("[tool ") for x in texts):
        role = "tool"
    return role, text[:MAX_TEXT], toolus


# ---------------------------------------------------------------- indexing

def _file_info(path):
    rel = path.relative_to(PROJECTS)
    project = rel.parts[0]
    if "subagents" in rel.parts:
        return project, rel.parts[1], 1, path.stem.removeprefix("agent-")
    return project, path.stem, 0, None


def _ingest(db, path, start, line_no, project, session, is_sub, agent):
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    end = data.rfind(b"\n")
    if end < 0:
        return start, line_no
    chunk, consumed = data[: end + 1], end + 1
    for raw in chunk.split(b"\n")[:-1]:
        line_no += 1
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        sid = rec.get("sessionId") or session
        if rec.get("type") == "ai-title" and not is_sub and rec.get("aiTitle"):
            db.execute("UPDATE sessions SET title=? WHERE session=?", (rec["aiTitle"], session))
            continue
        parsed = parse_record(rec)
        if not parsed:
            continue
        role, text, toolus = parsed
        ts = rec.get("timestamp")
        cur = db.execute(
            "INSERT INTO msgs(file,line,session,uuid,ts,role,text,project,is_sub,agent) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(path), line_no, sid, rec.get("uuid"), ts, role, text, project, is_sub, agent))
        mid = cur.lastrowid
        db.execute("INSERT INTO msgs_fts(rowid, text) VALUES(?,?)", (mid, text))
        for tid in toolus:
            db.execute("INSERT OR REPLACE INTO toolu VALUES(?,?)", (tid, mid))
        if ts and not is_sub:
            db.execute("UPDATE sessions SET first_ts=COALESCE(MIN(first_ts, ?), ?), last_ts=MAX(COALESCE(last_ts,''), ?)"
                       " WHERE session=?", (ts, ts, ts, session))
    return start + consumed, line_no


def _drop_file(db, path):
    ids = [r[0] for r in db.execute("SELECT id FROM msgs WHERE file=?", (path,))]
    for mid in ids:
        row = db.execute("SELECT text FROM msgs WHERE id=?", (mid,)).fetchone()
        db.execute("INSERT INTO msgs_fts(msgs_fts, rowid, text) VALUES('delete', ?, ?)", (mid, row[0]))
    db.execute("DELETE FROM toolu WHERE msg_id IN (SELECT id FROM msgs WHERE file=?)", (path,))
    db.execute("DELETE FROM msgs WHERE file=?", (path,))
    db.execute("DELETE FROM files WHERE path=?", (path,))


def update_index(db=None):
    """Incrementally index new/changed transcript bytes. Returns number of files touched."""
    own = db is None
    db = db or connect()
    # One indexer at a time (the MCP server and the SessionStart hook can overlap):
    # take the write lock before reading any file's offset, so two never ingest
    # the same bytes twice. The other waits (connect's timeout) and then sees the
    # offsets this one wrote.
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    touched = 0
    seen = set()
    for path in PROJECTS.glob("**/*.jsonl"):
        if "memory" in path.parts:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        sp = str(path)
        seen.add(sp)
        row = db.execute("SELECT size, mtime, offset, line FROM files WHERE path=?", (sp,)).fetchone()
        if row and row[0] == st.st_size and row[1] == st.st_mtime:
            continue
        project, session, is_sub, agent = _file_info(path)
        if row and st.st_size < row[2]:          # truncated/rewritten: reindex from scratch
            _drop_file(db, sp)
            row = None
        start, line_no = (row[2], row[3]) if row else (0, 0)
        if not is_sub:
            db.execute("INSERT OR IGNORE INTO sessions(session, project, file) VALUES(?,?,?)", (session, project, sp))
        offset, line_no = _ingest(db, path, start, line_no, project, session, is_sub, agent)
        db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                   (sp, st.st_size, st.st_mtime, offset, session, project, is_sub, agent, line_no))
        touched += 1
    # Transcripts deleted on disk keep their index rows on purpose (search outlives cleanup).
    db.commit()
    if own:
        db.close()
    return touched


# ---------------------------------------------------------------- queries

def _fts_query(q, op):
    terms = re.findall(r"[\w][\w.\-/']*", q, re.U)
    return f" {op} ".join('"' + t.replace('"', '') + '"' for t in terms)


def search(query, project=None, since=None, limit=10, include_subagents=False, roles=("user", "assistant")):
    db = connect()
    update_index(db)
    out = []
    for op in ("AND", "OR"):
        fq = _fts_query(query, op)
        if not fq:
            return []
        sql = ("SELECT m.session, m.uuid, m.ts, m.role, m.project, m.is_sub, m.agent, s.title, "
               "snippet(msgs_fts, 0, '>>', '<<', ' … ', 24) FROM msgs_fts JOIN msgs m ON m.id = msgs_fts.rowid "
               "LEFT JOIN sessions s ON s.session = m.session WHERE msgs_fts MATCH ?")
        args = [fq]
        if roles:
            sql += f" AND m.role IN ({','.join('?' * len(roles))})"
            args += list(roles)
        if project:
            sql += " AND m.project LIKE ?"
            args.append(f"%{project}%")
        if since:
            sql += " AND m.ts >= ?"
            args.append(since)
        if not include_subagents:
            sql += " AND m.is_sub = 0"
        sql += " ORDER BY bm25(msgs_fts) LIMIT ?"
        args.append(limit)
        out = db.execute(sql, args).fetchall()
        if out:
            break
    db.close()
    return [{"anchor": f"{r[0]}:{r[1]}", "date": r[2], "role": r[3], "project": r[4],
             "subagent": r[6] if r[5] else None, "session_title": r[7], "snippet": r[8]} for r in out]


def list_sessions(project=None, limit=20):
    db = connect()
    update_index(db)
    sql = "SELECT session, project, title, first_ts, last_ts FROM sessions"
    args = []
    if project:
        sql += " WHERE project LIKE ?"
        args.append(f"%{project}%")
    sql += " ORDER BY COALESCE(last_ts, '') DESC LIMIT ?"
    args.append(limit)
    rows = db.execute(sql, args).fetchall()
    db.close()
    return [{"session": r[0], "project": r[1], "title": r[2], "started": r[3], "last_activity": r[4]} for r in rows]


def _resolve(db, anchor):
    anchor = anchor.strip()
    session, _, ref = anchor.partition(":")
    if ref.startswith("toolu_"):
        return db.execute("SELECT m.id, m.file, m.line FROM toolu t JOIN msgs m ON m.id = t.msg_id "
                          "WHERE t.tool_use_id=?", (ref,)).fetchone()
    if ref:
        return db.execute("SELECT id, file, line FROM msgs WHERE uuid=? ORDER BY is_sub LIMIT 1", (ref,)).fetchone()
    return db.execute("SELECT id, file, line FROM msgs WHERE session=? AND is_sub=0 ORDER BY line LIMIT 1",
                      (session,)).fetchone()


def read_transcript(anchor, before=6, after=6, include_tools=False, max_chars=6000):
    db = connect()
    update_index(db)
    hit = _resolve(db, anchor)
    if not hit:
        db.close()
        return None
    mid, f, line = hit
    roles = ("user", "assistant", "tool") if include_tools else ("user", "assistant")
    ph = ",".join("?" * len(roles))
    cols = "id, line, uuid, ts, role, text, session"
    prev = db.execute(f"SELECT {cols} FROM msgs WHERE file=? AND line<? AND role IN ({ph}) ORDER BY line DESC LIMIT ?",
                      (f, line, *roles, before)).fetchall()[::-1]
    cur = db.execute(f"SELECT {cols} FROM msgs WHERE id=?", (mid,)).fetchall()
    nxt = db.execute(f"SELECT {cols} FROM msgs WHERE file=? AND line>? AND role IN ({ph}) ORDER BY line LIMIT ?",
                     (f, line, *roles, after)).fetchall()
    title = db.execute("SELECT title FROM sessions WHERE session=?", (cur[0][6],)).fetchone()
    db.close()
    turns = []
    for r in prev + cur + nxt:
        text = r[5]
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…[truncated {len(r[5]) - max_chars} chars; narrow the window or raise max_chars]"
        turns.append({"anchor": f"{r[6]}:{r[2]}", "date": r[3], "role": r[4], "is_target": r[0] == mid, "text": text})
    return {"session_title": title[0] if title else None, "file": f, "turns": turns}


if __name__ == "__main__":  # tiny CLI for testing: recall_core.py search <q> | read <anchor> | sessions | index
    import sys
    cmd, *rest = sys.argv[1:] or ["index"]
    if cmd == "index":
        print("files touched:", update_index())
    elif cmd == "search":
        print(json.dumps(search(" ".join(rest)), indent=2))
    elif cmd == "read":
        print(json.dumps(read_transcript(rest[0]), indent=2))
    elif cmd == "sessions":
        print(json.dumps(list_sessions(), indent=2))
