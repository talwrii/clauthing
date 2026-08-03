#!/usr/bin/env python3
"""clauthing-convos — fuzzy-search your Claude conversation history.

Aggregates every message (your prompts + Claude's replies) from all clauthing
session transcripts into one searchable list and runs the shared fuzzy picker
(clauthing.fuzzy_tui). Contiguous matches rank best. Selecting a result prints
a reference (session id, cwd, timestamp) plus the message text.

Usage:  clauthing-convos [session-configs-dir]
"""
import argparse
import json
import sys
import time
from pathlib import Path

from clauthing.fuzzy_tui import pick


def _default_base():
    return Path.home() / ".config" / "clauthing" / "session-configs"


def _entry_text(entry):
    """The human-readable text of a transcript entry (a string, or the joined
    text blocks of a content list). Empty for tool-only turns."""
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _transcript_files(base):
    """(mtime, path, session, encoded_cwd) for every transcript, newest first."""
    files = []
    for sdir in base.iterdir():
        proj = sdir / "projects"
        if not proj.is_dir():
            continue
        for enc in proj.iterdir():
            if not enc.is_dir():
                continue
            for f in enc.glob("*.jsonl"):
                try:
                    files.append((f.stat().st_mtime, f, sdir.name, enc.name))
                except OSError:
                    continue
    files.sort(key=lambda x: x[0], reverse=True)
    return files


def load_conversations(base=None, days=None, max_files=None):
    """Every user/assistant text turn across clauthing session transcripts,
    newest first. Each entry: role, text, session, cwd, ts.

    Reads transcripts newest-first (by file mtime). `days` keeps only files
    touched in the last N days; `max_files` caps how many transcript files are
    read. Both bound the cost — the full history is ~half a million turns.
    """
    base = Path(base) if base else _default_base()
    if not base.exists():
        return []
    files = _transcript_files(base)
    if days is not None:
        cutoff = time.time() - days * 86400
        files = [x for x in files if x[0] >= cutoff]

    # Spend the file budget on DISTINCT conversations, not clones. A :cd clone
    # is a byte-copy of the transcript, so it shares the first line — use that
    # as a lineage signature to skip clones (otherwise clone-spam of one
    # conversation fills max_files and every result looks the same).
    chosen, sigs = [], set()
    for _mtime, f, session, enc in files:
        try:
            with open(f, "r", errors="ignore") as fh:
                sig = fh.readline().strip()
        except Exception:
            continue
        if not sig or sig in sigs:
            continue
        sigs.add(sig)
        chosen.append((f, session, enc))
        if max_files is not None and len(chosen) >= max_files:
            break

    out, seen = [], set()
    for f, session, enc in chosen:
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") not in ("user", "assistant"):
                continue
            text = _entry_text(e)
            if not text:
                continue
            ts = e.get("timestamp") or ""
            # A :cd clone copies the transcript into a new session dir but keeps
            # each turn's timestamp — so the same (ts, role, text) turn recurs in
            # many files. Dedup on it to collapse clones; genuinely distinct
            # turns differ in timestamp and survive.
            key = (ts, e["type"], text)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "role": e["type"],
                "text": text,
                "session": session,
                "cwd": e.get("cwd") or enc,
                "ts": ts,
            })
    out.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return out


def _text(e):
    return f"{e.get('role', '')} {e.get('cwd', '')} {e.get('text', '')}"


def _label(e, width):
    ts = (e.get("ts") or "")[:16].replace("T", " ")
    who = "you" if e.get("role") == "user" else "claude"
    cwd = Path(e.get("cwd", "")).name or "?"
    body = " ".join((e.get("text", "") or "").split())
    return f"{ts:16} {who:6} @{cwd}: {body}"[:width]


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="clauthing-convos",
        description="Fuzzy-search your Claude conversation history.")
    p.add_argument("base", nargs="?", help="session-configs dir (default: ~/.config/clauthing/session-configs)")
    p.add_argument("--max-files", type=int, default=100,
                   help="read the N most-recently-active transcripts (default 100; keeps it snappy)")
    p.add_argument("--days", type=int, default=None,
                   help="also restrict to transcripts touched in the last N days")
    p.add_argument("--all", action="store_true",
                   help="read the entire history (slow — ~half a million turns)")
    args = p.parse_args(argv)

    max_files = None if args.all else args.max_files
    print("loading conversations…", file=sys.stderr, flush=True)
    convos = load_conversations(args.base, days=args.days, max_files=max_files)
    if not convos:
        print("No conversations found"
              + ("" if args.all else f" in the {args.max_files} most recent transcripts (try --all)"))
        return 0
    e = pick(convos, _text, _label, "search conversations")
    if e is None:
        return 1
    who = "you" if e.get("role") == "user" else "claude"
    print(f"session: {e.get('session', '')}")
    print(f"cwd:     {e.get('cwd', '')}")
    print(f"when:    {e.get('ts', '')}")
    print(f"{who}: {e.get('text', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
