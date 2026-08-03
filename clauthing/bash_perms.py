#!/usr/bin/env python3
"""Reading and matching Claude Code Bash(...) permission rules.

Kept dependency-free (stdlib only) so both the run MCP server and the curses
confirm dialog can import it without pulling in the MCP stack.

A rule is written `Bash(<pattern>)` in a settings file's permissions.allow /
permissions.deny. `<pattern>` is matched against a sub-command string:
  *            → anything
  foo:*        → foo, or foo followed by a space (prefix match)
  foo          → exactly foo (no trailing args)
"""
import json
import re
import shlex
import subprocess
from pathlib import Path


def git_repo_root(cwd):
    """The git-repo root of `cwd` — the nearest ancestor containing a `.git`,
    where Claude Code resolves `.claude/` to — or `cwd` itself if not in a repo."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return Path(cwd)


def repo_settings_local(cwd):
    """Where Claude writes "don't ask again" Bash rules for `cwd`:
    <repo-root>/.claude/settings.local.json."""
    return git_repo_root(cwd) / ".claude" / "settings.local.json"


def split_bash(cmd):
    """Split a shell command into (operator, segment) pairs on TOP-LEVEL
    &&, ||, ;, | — respecting single/double/backtick quotes and $(...)/${...}/
    (...) nesting so we never split inside them. Display-only, never executed.
    The first pair's operator is ''. Empty segments are dropped."""
    pairs, seg, op = [], [], ""
    quote = None
    depth = 0
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            seg.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            seg.append(c); seg.append(cmd[i + 1]); i += 2; continue
        if c in ("'", '"', "`"):
            quote = c; seg.append(c); i += 1; continue
        if c in ("(", "{"):
            depth += 1; seg.append(c); i += 1; continue
        if c in (")", "}"):
            depth = max(0, depth - 1); seg.append(c); i += 1; continue
        if depth == 0:
            two = cmd[i:i + 2]
            if two in ("&&", "||"):
                pairs.append((op, "".join(seg).strip())); seg = []; op = two; i += 2; continue
            if c == ";":
                pairs.append((op, "".join(seg).strip())); seg = []; op = ";"; i += 1; continue
            if c == "|":
                pairs.append((op, "".join(seg).strip())); seg = []; op = "|"; i += 1; continue
        seg.append(c); i += 1
    pairs.append((op, "".join(seg).strip()))
    return [(o, s) for o, s in pairs if s]


def ssh_remote(seg):
    """If `seg` is an `ssh … '<remote>'` command, return
    (prefix, quote, remote, suffix); else None. The remote command is the first
    quoted arg."""
    s = seg.strip()
    if not re.match(r"ssh(\s|$)", s):
        return None
    m = re.search(r"['\"]", s)
    if not m:
        return None
    q, start = m.group(0), m.start()
    j = start + 1
    while j < len(s):
        if q == '"' and s[j] == "\\" and j + 1 < len(s):
            j += 2
            continue
        if s[j] == q:
            break
        j += 1
    else:
        return None
    return s[:start].rstrip(), q, s[start + 1:j], s[j + 1:]


def parse_bash_rule(rule):
    """Extract the inner pattern of a Bash(...) rule, else None.

    A bare `Bash` (or `Bash()`) — the whole-tool rule that allows/denies every
    bash command in Claude Code — maps to `*` (match anything).
    """
    r = str(rule).strip()
    if r in ("Bash", "Bash()"):
        return "*"
    m = re.match(r"Bash\((.*)\)\s*$", r)
    return m.group(1).strip() if m else None


# ── BetterBash: a saner pattern language (opt-in, our run tool only) ──────────
#
#   BetterBash(git status)      exact — git status and nothing more
#   BetterBash(git ...)         git followed by any number of args
#   BetterBash(git _)           git + exactly one arg (any single token)
#   BetterBash(cat ~/mine/**)   cat + a path under ~/mine (any depth)
#   BetterBash(ls ~/mine/*)     ls + a path one level under ~/mine
#   BetterBash(git _ ...)       git + one specific arg + more
#
# `_` = any one token, `...` = zero-or-more tokens, and within a token `*`
# matches anything but `/`, `**` matches anything including `/`. No implicit
# prefixing — a trailing `...` is how you opt into "and more args".


def parse_rule(rule):
    """Parse a permission rule string into (kind, pattern), or None.

    kind is "better" for BetterBash(...) rules, else "bash" (Bash(...) / bare
    Bash). Non-Bash rules (Read, mcp__…) return None.
    """
    r = str(rule).strip()
    m = re.match(r"BetterBash\((.*)\)\s*$", r)
    if m:
        return ("better", m.group(1).strip())
    p = parse_bash_rule(r)
    return ("bash", p) if p is not None else None


def _glob_token(pat, s):
    """Match one token: `**` matches any run incl `/`, `*` matches any run but
    `/`, everything else literal."""
    rx, i = [], 0
    while i < len(pat):
        if pat[i:i + 2] == "**":
            rx.append(".*")
            i += 2
        elif pat[i] == "*":
            rx.append("[^/]*")
            i += 1
        else:
            rx.append(re.escape(pat[i]))
            i += 1
    return re.fullmatch("".join(rx), s) is not None


def _token_match(pat_tok, cmd_tok):
    if pat_tok == "_":
        return True
    if "*" in pat_tok:
        return _glob_token(pat_tok, cmd_tok)
    return pat_tok == cmd_tok


def _match_tokens(pat, cmd):
    if not pat:
        return not cmd
    if pat[0] == "...":                       # zero-or-more tokens
        return (_match_tokens(pat[1:], cmd)
                or (bool(cmd) and _match_tokens(pat, cmd[1:])))
    if not cmd:
        return False
    return _token_match(pat[0], cmd[0]) and _match_tokens(pat[1:], cmd[1:])


def better_match(pattern, command):
    """Does `command` match a BetterBash `pattern` (token wildcards + globs)?"""
    try:
        cmd_tokens = shlex.split(command)
    except Exception:
        cmd_tokens = command.split()
    return _match_tokens(pattern.split(), cmd_tokens)


def matches(kind, pattern, seg):
    """Does sub-command `seg` match a (kind, pattern) rule?"""
    if kind == "better":
        return better_match(pattern, seg)
    return rule_matches(seg.strip(), pattern)


def settings_files(config_dir, cwds=()):
    """The Claude settings files that apply, in read order: the session config
    dir's settings(.local).json plus any project .claude/settings(.local).json
    for the given cwds."""
    files = []
    if config_dir:
        cd = Path(config_dir)
        files += [cd / "settings.json", cd / "settings.local.json"]
    for d in cwds:
        if d:
            root = git_repo_root(d)   # resolve to the repo root like Claude does
            files += [root / ".claude" / "settings.json",
                      root / ".claude" / "settings.local.json"]
    return files


def load_bash_permissions(config_dir, cwds=(), files=None):
    """Return (allow, deny) lists of Bash patterns from the applicable files.

    `files` overrides the derived list — pass explicit paths to read exactly
    those (used by tests, and by callers that already know the files).
    """
    allow, deny = [], []
    for f in (files if files is not None else settings_files(config_dir, cwds)):
        try:
            perms = json.loads(f.read_text()).get("permissions", {})
        except Exception:
            continue
        for rule in perms.get("allow", []) or []:
            kp = parse_rule(rule)
            if kp is not None:
                allow.append(kp)
        for rule in perms.get("deny", []) or []:
            kp = parse_rule(rule)
            if kp is not None:
                deny.append(kp)
    return allow, deny


def rule_matches(seg, pattern):
    """Does sub-command `seg` match Bash rule `pattern`? `foo:*` is a prefix
    match, `*` matches anything, else exact."""
    seg = seg.strip()
    if pattern == "*":
        return True
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        return seg == prefix or seg.startswith(prefix + " ")
    return seg == pattern


def _norm(rule):
    """A rule may be a (kind, pattern) tuple (from load) or a bare pattern
    string (from older callers/tests) — normalise to (kind, pattern)."""
    return rule if isinstance(rule, tuple) else ("bash", rule)


def seg_status(seg, allow, deny):
    """'deny' | 'allow' | 'ask' for a sub-command (deny wins). `allow`/`deny`
    entries may be (kind, pattern) tuples or bare bash pattern strings."""
    if any(matches(k, p, seg) for k, p in map(_norm, deny)):
        return "deny"
    if any(matches(k, p, seg) for k, p in map(_norm, allow)):
        return "allow"
    return "ask"


def add_allow_rule(settings_file, pattern):
    """Append `pattern` (a Bash(...) rule string) to permissions.allow in
    `settings_file`, creating the file as needed. Idempotent."""
    f = Path(settings_file)
    try:
        data = json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if pattern not in allow:
        allow.append(pattern)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2))
