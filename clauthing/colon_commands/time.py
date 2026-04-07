#!/usr/bin/env python3
"""Timing utilities for colon commands."""
import json
from pathlib import Path


def get_state_dir():
    """Get the XDG state directory for clauthing."""
    import os
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "clauthing"
    else:
        state_dir = Path.home() / ".local" / "state" / "clauthing"

    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def save_request_start_time(session_id):
    """Save timestamp when request starts for a specific session."""
    import time
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_dir.mkdir(exist_ok=True)
    timing_file = timing_dir / f"{session_id}.json"

    timing_data = {
        "start_time": time.time()
    }
    timing_file.write_text(json.dumps(timing_data))


def save_response_duration(session_id):
    """Calculate and save the duration of the last response for a specific session."""
    import time
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_file = timing_dir / f"{session_id}.json"

    if not timing_file.exists():
        return

    try:
        timing_data = json.loads(timing_file.read_text())
        start_time = timing_data.get("start_time")

        if start_time:
            duration = time.time() - start_time
            timing_data["duration"] = duration
            timing_data["timestamp"] = time.time()
            timing_file.write_text(json.dumps(timing_data))
    except:
        pass


def get_last_response_duration(session_id):
    """Get the duration of the last response in seconds for a specific session."""
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_file = timing_dir / f"{session_id}.json"

    if not timing_file.exists():
        return None

    try:
        timing_data = json.loads(timing_file.read_text())
        return timing_data.get("duration")
    except:
        return None


