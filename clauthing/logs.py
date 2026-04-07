#!/usr/bin/env python3
"""Log viewing commands for clauthing."""
import os
import sys
from pathlib import Path

from clauthing.logging import get_log_dir, get_run_log_file


def handle_last_logs(profile):
    """Show logs from the last run."""
    log_dir = get_log_dir(profile)
    if not log_dir.exists():
        print(f"No logs found for this profile")
        sys.exit(1)

    # Find most recent run log (sort numerically by run number)
    run_logs = sorted(log_dir.glob("run-*.log"), key=lambda p: int(p.stem.split('-')[1]))
    if not run_logs:
        print(f"No run logs found in {log_dir}")
        sys.exit(1)

    last_log = run_logs[-1]
    print(f"=== Showing {last_log} ===\n")

    # Print entire log file
    try:
        with open(last_log, "r") as f:
            print(f.read())
    except Exception as e:
        print(f"Error reading log file: {e}")
        sys.exit(1)

    sys.exit(0)


def handle_follow_logs(profile):
    """Follow the current run's log file."""
    run_log = get_run_log_file(profile)
    if not run_log.exists():
        print(f"Log file does not exist: {run_log}")
        print("Run some clauthing commands first to generate logs")
        sys.exit(1)

    # Print last 80 lines
    try:
        with open(run_log, "r") as f:
            lines = f.readlines()
            start = max(0, len(lines) - 80)
            for line in lines[start:]:
                print(line, end='')
    except Exception as e:
        print(f"Error reading log file: {e}")

    # Follow the log file
    print(f"\n--- Following {run_log} ---")
    os.execvp("tail", ["tail", "-f", str(run_log)])


