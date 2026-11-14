#!/usr/bin/env python3
"""SessionStart hook: keep recall's index ahead of Claude Code's transcript cleanup.

1. Refreshes the index in the background (a detached process; the session never
   waits on it). Index rows outlive the transcript files they came from, so once a
   session is indexed its memory anchors keep resolving even after Claude Code
   deletes the file. Without this, a session is indexed only when a recall tool is
   next called, and one that goes 30 days without that can be deleted first.
2. Notes once when user settings keep transcripts for under a year
   (`cleanupPeriodDays`, default 30), and again if the value changes and is still short.

Never fails the session: every error exits quietly.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
DATA = CONFIG / "recall"
MIN_DAYS = 365
DEFAULT_DAYS = 30


def refresh_index():
    subprocess.Popen(
        [sys.executable, str(HERE / "recall_core.py"), "index"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def retention_note():
    try:
        days = json.loads((CONFIG / "settings.json").read_text()).get("cleanupPeriodDays", DEFAULT_DAYS)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(days, int) or days >= MIN_DAYS:
        return None
    DATA.mkdir(parents=True, exist_ok=True)
    flag = DATA / "noted-retention"
    if flag.exists() and flag.read_text().strip() == str(days):
        return None
    flag.write_text(str(days))
    return (f"recall: Claude Code deletes transcripts after {days} days. Memory anchors point into them; "
            "recall keeps an index copy, but only of sessions it indexed in time. Add "
            '"cleanupPeriodDays": 3650 to ~/.claude/settings.json to keep transcripts for 10 years.')


def main():
    try:
        refresh_index()
    except Exception:  # noqa: BLE001
        pass
    try:
        note = retention_note()
    except Exception:  # noqa: BLE001
        note = None
    if note:
        print(json.dumps({"systemMessage": note}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        sys.exit(0)
