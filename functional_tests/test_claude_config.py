#!/usr/bin/env python3
"""Tests using the actual clauthing configuration.

These tests verify that the generated tmux.conf works correctly.
"""

import os
import sys
import subprocess
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from harness import TmuxTestHarness, TestRunner, assert_eq, assert_true


def generate_clauthing_config(config_dir: Path, profile: str = None) -> Path:
    """Generate a clauthing tmux config for testing.
    
    This mimics what clauthing main.py does when generating the config.
    
    Args:
        config_dir: Directory to create config in
        profile: Optional profile name
        
    Returns:
        Path to the generated tmux.conf
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    
    jail_dir = config_dir / "jail"
    jail_dir.mkdir(exist_ok=True)
    
    claude_data_dir = config_dir / "claude-data"
    claude_data_dir.mkdir(exist_ok=True)
    
    # Generate config similar to clauthing
    if profile:
        clauthing_cmd = f"echo 'clauthing --profile {profile} --new-window'"
        profile_arg = f"--profile {profile} "
    else:
        clauthing_cmd = "echo 'clauthing --new-window'"
        profile_arg = ""
    
    tmux_config = config_dir / "tmux.conf"
    tmux_config.write_text(f"""\
# clauthing tmux config (test version)
set -g destroy-unattached off

# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"

# Default command (simplified for testing)
set -g default-command "bash"

# Bind C-n directly (no prefix) to open new window
bind -n C-n new-window -c "{jail_dir}"

# Also override default C-b c
bind c new-window -c "{jail_dir}"

# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1

# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window

# Disable automatic window renaming
set -g automatic-rename off
set -g allow-rename off

# C-p for session picker (simplified for testing - just echo)
bind -n C-p display-popup -E -w 80% -h 60% "echo 'picker would run here'; read"

# Quick escape for testing
set -sg escape-time 0
""")
    
    return tmux_config


class KittyClaudeTestHarness(TmuxTestHarness):
    """Test harness specifically for clauthing configs."""
    
    def __init__(self, profile: str = None):
        self.profile = profile
        self._config_dir = None
        super().__init__()
        
    def start(self, initial_command: str = "bash"):
        # Create temp directory
        self._config_dir = Path(tempfile.mkdtemp(prefix="clauthing-real-test-"))
        
        # Generate real clauthing config
        self.config_file = generate_clauthing_config(self._config_dir, self.profile)
        
        # Call parent start
        super().start(initial_command)
        
    def stop(self):
        super().stop()
        # Clean up config dir
        if self._config_dir and self._config_dir.exists():
            import shutil
            shutil.rmtree(self._config_dir, ignore_errors=True)


def run_clauthing_config_tests():
    """Test the actual clauthing configuration."""
    runner = TestRunner()
    
    print("Testing with Generated clauthing Configuration")
    print("=" * 55)
    print()
    
    print("Basic Functionality:")
    
    def test_config_generation():
        """Test that config can be generated and parsed."""
        with KittyClaudeTestHarness() as h:
            # Just verify it starts
            assert_eq(h.get_window_count(), 1, "Started with 1 window")
    runner.run_test("config_generation", test_config_generation)
    
    def test_jail_directory():
        """Test that new windows opened via ctrl+n start in the jail directory."""
        with KittyClaudeTestHarness() as h:
            # Open a new window via ctrl+n (bound to new-window -c jail_dir in config)
            h.ctrl('n')
            time.sleep(0.5)

            # Query tmux directly for the pane's current path
            import subprocess
            result = subprocess.run(
                ["tmux", "-L", h.socket_name, "display-message", "-p", "#{pane_current_path}"],
                capture_output=True, text=True, timeout=5
            )
            pane_path = result.stdout.strip()
            assert_true("jail" in pane_path,
                       f"New window should start in jail dir, got: {pane_path!r}")
    runner.run_test("jail_directory", test_jail_directory)
    
    print()
    print("Keybinding Tests (with real config):")
    
    def test_ctrl_n_creates_window():
        with KittyClaudeTestHarness() as h:
            h.ctrl('n')
            h.wait_for_window_count(2)
    runner.run_test("ctrl_n_creates_window", test_ctrl_n_creates_window)
    
    def test_navigation_with_real_config():
        with KittyClaudeTestHarness() as h:
            # Create some windows
            h.ctrl('n')
            h.wait_for_window_count(2)
            h.ctrl('n')
            h.wait_for_window_count(3)
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Navigate forward
            h.ctrl('k')
            h.wait_for_window_index(2, timeout=2)
            assert_eq(h.get_current_window_index(), 2)
            
            # Navigate backward
            h.ctrl('j')
            h.wait_for_window_index(1, timeout=2)
            assert_eq(h.get_current_window_index(), 1)
    runner.run_test("navigation_with_real_config", test_navigation_with_real_config)
    
    def test_close_protection():
        with KittyClaudeTestHarness() as h:
            # With only one window, Ctrl+W shouldn't close
            h.ctrl('w')
            time.sleep(0.3)
            assert_eq(h.get_window_count(), 1, "Can't close last window")
            
            # Create a second window and close it
            h.ctrl('n')
            h.wait_for_window_count(2)
            h.ctrl('w')
            h.wait_for_window_count(1)
    runner.run_test("close_protection", test_close_protection)
    
    print()
    print("Profile Tests:")
    
    def test_profile_config():
        """Test config generation with a profile."""
        with KittyClaudeTestHarness(profile="test-profile") as h:
            assert_eq(h.get_window_count(), 1)
            h.ctrl('n')
            h.wait_for_window_count(2)
    runner.run_test("profile_config", test_profile_config)
    
    return runner.summary()


if __name__ == "__main__":
    sys.exit(run_clauthing_config_tests())