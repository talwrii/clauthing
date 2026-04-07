#!/usr/bin/env python3
"""Logging utilities for kitty-claude."""
import os
import sys
import subprocess
import datetime
from pathlib import Path

def get_log_dir(profile=None):
    """Get the log directory for the current instance.

    If KITTY_CLAUDE_INSTANCE_UUID is set in the environment, logs are
    routed to that instance's per-uuid directory. Otherwise falls back
    to the legacy per-profile directory.
    """
    instance_uuid = os.environ.get("KITTY_CLAUDE_INSTANCE_UUID")
    if instance_uuid:
        from kitty_claude.instances import get_log_dir_for_uuid
        return get_log_dir_for_uuid(instance_uuid)

    uid = os.getuid()
    # Try /var/run first
    try:
        if profile:
            log_dir = Path(f"/var/run/{uid}/kitty-claude/logs-{profile}")
        else:
            log_dir = Path(f"/var/run/{uid}/kitty-claude/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except PermissionError:
        # Fallback to /tmp
        if profile:
            return Path(f"/tmp/kitty-claude-{profile}-logs")
        else:
            return Path("/tmp/kitty-claude-logs")

def get_run_log_file(profile=None):
    """Get the current run's log file path."""
    log_dir = get_log_dir(profile)
    log_dir.mkdir(exist_ok=True)

    # Get or create run ID for this session
    run_id_file = log_dir / "current-run-id"
    if not run_id_file.exists():
        # Find next run number
        existing_runs = sorted(log_dir.glob("run-*.log"))
        if existing_runs:
            last_num = int(existing_runs[-1].stem.split("-")[1])
            run_num = last_num + 1
        else:
            run_num = 1
        run_id_file.write_text(str(run_num))

    run_num = int(run_id_file.read_text().strip())
    return log_dir / f"run-{run_num}.log"

def get_combined_log_file(profile=None):
    """Get the combined log file path."""
    log_dir = get_log_dir(profile)
    log_dir.mkdir(exist_ok=True)
    return log_dir / "combined.log"

def cleanup_old_run_logs(profile=None, keep=5):
    """Keep only the last N run logs."""
    log_dir = get_log_dir(profile)
    if not log_dir.exists():
        return

    # Sort numerically by run number
    run_logs = sorted(log_dir.glob("run-*.log"), key=lambda p: int(p.stem.split('-')[1]))
    if len(run_logs) > keep:
        for old_log in run_logs[:-keep]:
            old_log.unlink()

def log(message, profile=None):
    """Log a message to both run-specific and combined log files.
    
    If KITTY_CLAUDE_LOG_STDERR is set, also prints to stderr.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"

    # Also log to stderr if --log flag was used
    if os.environ.get('KITTY_CLAUDE_LOG_STDERR'):
        short_timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[kc {short_timestamp}] {message}", file=sys.stderr, flush=True)

    try:
        # Write to current run log
        run_log = get_run_log_file(profile)
        with open(run_log, "a") as f:
            f.write(log_line)
            f.flush()  # Ensure it's written before execvp

        # Write to combined log
        combined_log = get_combined_log_file(profile)
        with open(combined_log, "a") as f:
            f.write(log_line)
            f.flush()  # Ensure it's written before execvp
    except:
        pass

def run(cmd, *args, profile=None, **kwargs):
    """Wrapper around subprocess.run that logs the command and sets CLAUDE_CONFIG_DIR.

    Args:
        cmd: Command to run (list or string)
        *args: Positional args passed to subprocess.run
        profile: Profile name for logging (optional)
        **kwargs: Keyword args passed to subprocess.run

    Returns:
        subprocess.CompletedProcess result
    """
    # Format command for logging
    if isinstance(cmd, list):
        cmd_str = ' '.join(str(x) for x in cmd)
    else:
        cmd_str = str(cmd)

    log(f"RUN: {cmd_str}", profile)

    # Set CLAUDE_CONFIG_DIR in environment
    env = kwargs.get('env', os.environ.copy())

    # Only set if not already set
    if 'CLAUDE_CONFIG_DIR' not in env:
        if profile:
            config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        else:
            config_dir = Path.home() / ".config" / "kitty-claude"
        claude_config_dir = str(config_dir / "claude-data")
        env['CLAUDE_CONFIG_DIR'] = claude_config_dir
        log(f"Setting CLAUDE_CONFIG_DIR={claude_config_dir}", profile)
    else:
        log(f"CLAUDE_CONFIG_DIR already set: {env['CLAUDE_CONFIG_DIR']}", profile)

    kwargs['env'] = env

    # Call subprocess.run with all the original arguments
    result = subprocess.run(cmd, *args, **kwargs)

    # Log stderr if it was captured and is not empty (for all commands)
    if hasattr(result, 'stderr') and result.stderr:
        stderr_str = result.stderr if isinstance(result.stderr, str) else result.stderr.decode('utf-8', errors='replace')
        if stderr_str.strip():
            log(f"STDERR: {stderr_str.strip()}", profile)

    # Log if command failed
    if result.returncode != 0:
        log(f"RUN FAILED (exit {result.returncode}): {cmd_str}", profile)

    return result