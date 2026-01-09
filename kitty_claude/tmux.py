#!/usr/bin/env python3
"""Tmux operations for kitty-claude."""
import os
import subprocess
from pathlib import Path
from kitty_claude.logging import run

def send_tmux_message(message, socket="kitty-claude"):
    """Send a message via tmux display-message"""
    try:
        run([
            "tmux", "-L", socket,
            "display-message", message
        ], stderr=subprocess.DEVNULL)
    except:
        pass

def get_runtime_tmux_state_file(profile=None):
    """Get the runtime tmux state file path (for window restoration)."""
    uid = os.getuid()
    # Try /var/run first
    try:
        runtime_dir = Path(f"/var/run/{uid}/kitty-claude")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to /tmp
        runtime_dir = Path(f"/tmp/kitty-claude-{uid}")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    
    # Use profile-specific state file if profile is set
    if profile:
        return runtime_dir / f"tmux-state-{profile}.json"
    else:
        return runtime_dir / "tmux-state.json"