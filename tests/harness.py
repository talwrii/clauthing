#!/usr/bin/env python3
"""Test harness for kitty-claude.

This harness runs tmux in a PTY and allows sending keystrokes and verifying state.
We test tmux directly (without kitty) since that's where the keybindings live.
"""

import os
import sys
import time
import subprocess
import tempfile
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Callable

# Try to import pexpect, give helpful error if not installed
try:
    import pexpect
except ImportError:
    print("Error: pexpect not installed. Run: pip install pexpect")
    sys.exit(1)


@dataclass
class Window:
    """Represents a tmux window."""
    index: int
    name: str
    active: bool


class TmuxTestHarness:
    """Test harness for tmux-based testing of kitty-claude.
    
    This spawns a tmux server with a unique socket name and allows
    sending keys and querying state.
    """
    
    def __init__(self, socket_name: Optional[str] = None, config_file: Optional[Path] = None):
        """Initialize the harness.
        
        Args:
            socket_name: Unique tmux socket name (auto-generated if None)
            config_file: Path to tmux.conf (uses minimal config if None)
        """
        self.socket_name = socket_name or f"test-{os.getpid()}-{int(time.time())}"
        self.config_file = config_file
        self.temp_dir = None
        self.process: Optional[pexpect.spawn] = None
        self._started = False
        
    def __enter__(self):
        self.start()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False
        
    def start(self, initial_command: str = "bash"):
        """Start the tmux session.
        
        Args:
            initial_command: Command to run in the first window
        """
        if self._started:
            return
            
        # Create temp directory for our test config
        self.temp_dir = Path(tempfile.mkdtemp(prefix="kitty-claude-test-"))
        
        # Create minimal tmux config if none provided
        if self.config_file is None:
            self.config_file = self.temp_dir / "tmux.conf"
            self._write_test_config()
        
        # Build tmux command
        cmd = [
            "tmux", "-L", self.socket_name,
            "-f", str(self.config_file),
            "new-session", "-s", "test"
        ]
        
        if initial_command:
            cmd.extend([initial_command])
        
        # Spawn tmux in a PTY
        self.process = pexpect.spawn(
            cmd[0], cmd[1:],
            encoding='utf-8',
            timeout=10,
            env={**os.environ, 'TERM': 'xterm-256color'}
        )
        
        self._started = True
        
        # Wait for tmux to be ready
        self._wait_for_ready()
        
    def _write_test_config(self):
        """Write a minimal test tmux config with kitty-claude keybindings."""
        config = """\
# Test config for kitty-claude harness
set -g base-index 1
setw -g pane-base-index 1

# kitty-claude keybindings
bind -n C-n new-window
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window

# C-p for picker (simplified for testing)
bind -n C-p display-popup -E "echo 'picker'; sleep 0.5"

# Disable automatic rename for predictable testing
set -g automatic-rename off
set -g allow-rename off

# Quick escape time for testing
set -sg escape-time 0
"""
        self.config_file.write_text(config)
        
    def _wait_for_ready(self, timeout: float = 5.0):
        """Wait for tmux to be ready to accept commands."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    ["tmux", "-L", self.socket_name, "list-sessions"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                if result.returncode == 0 and "test" in result.stdout:
                    time.sleep(0.1)  # Small extra delay for stability
                    return
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.1)
        raise TimeoutError("tmux did not become ready in time")
        
    def stop(self):
        """Stop the tmux session and clean up."""
        if not self._started:
            return
            
        # Kill the tmux server
        try:
            subprocess.run(
                ["tmux", "-L", self.socket_name, "kill-server"],
                capture_output=True,
                timeout=5
            )
        except:
            pass
            
        # Terminate pexpect process
        if self.process:
            try:
                self.process.terminate(force=True)
            except:
                pass
            self.process = None
            
        # Clean up temp directory
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            
        self._started = False
        
    def send_keys(self, keys: str, literal: bool = False):
        """Send keys directly through the PTY (bypasses tmux send-keys).
        
        This is needed because 'bind -n' keybindings are in tmux's root table,
        and 'tmux send-keys' sends to the pane's shell, not to tmux.
        
        Args:
            keys: Key sequence (e.g., "C-j" for Ctrl+J, "M-o" for Alt+O)
            literal: If True, send as literal text
        """
        if self.process is None:
            raise RuntimeError("Harness not started")
        
        if literal:
            self.process.send(keys)
        else:
            # Convert tmux key notation to actual bytes
            key_bytes = self._convert_key(keys)
            self.process.send(key_bytes)
        
        time.sleep(0.1)  # Allow tmux to process
        
    def _convert_key(self, key: str) -> str:
        """Convert tmux key notation to actual characters.
        
        Args:
            key: Key in tmux notation (e.g., "C-j", "M-o", "Enter")
            
        Returns:
            The actual character(s) to send
        """
        # Control keys: C-x means Ctrl+x
        if key.startswith("C-"):
            char = key[2:].lower()
            if len(char) == 1:
                # Ctrl+letter = chr(ord(letter) - ord('a') + 1)
                return chr(ord(char) - ord('a') + 1)
            elif char == "space":
                return chr(0)
                
        # Alt/Meta keys: M-x means Alt+x (ESC + x)
        if key.startswith("M-"):
            char = key[2:]
            return "\x1b" + char
            
        # Special keys
        special = {
            "Enter": "\r",
            "Space": " ",
            "Tab": "\t",
            "Escape": "\x1b",
            "BSpace": "\x7f",
        }
        if key in special:
            return special[key]
            
        # Just a regular character
        return key
        
    def send_keys_to_pane(self, keys: str, literal: bool = False):
        """Send keys via tmux send-keys (to the pane's application).
        
        Use this for sending input to the shell/application in the pane,
        not for triggering tmux keybindings.
        
        Args:
            keys: Key sequence in tmux notation
            literal: If True, send as literal text
        """
        cmd = ["tmux", "-L", self.socket_name, "send-keys"]
        if literal:
            cmd.append("-l")
        cmd.append(keys)
        
        subprocess.run(cmd, check=True, timeout=5)
        time.sleep(0.05)
        
    def send_text(self, text: str):
        """Send literal text to tmux."""
        self.send_keys(text, literal=True)
        
    def press_enter(self):
        """Press Enter."""
        self.send_keys("Enter")
        
    def ctrl(self, key: str):
        """Send Ctrl+key."""
        self.send_keys(f"C-{key}")
        
    def alt(self, key: str):
        """Send Alt+key."""
        self.send_keys(f"M-{key}")
        
    def new_window(self, name: Optional[str] = None, command: Optional[str] = None):
        """Create a new window via tmux command (not keybinding)."""
        cmd = ["tmux", "-L", self.socket_name, "new-window"]
        if name:
            cmd.extend(["-n", name])
        if command:
            cmd.append(command)
        subprocess.run(cmd, check=True, timeout=5)
        time.sleep(0.1)
        
    def rename_window(self, name: str):
        """Rename current window."""
        subprocess.run(
            ["tmux", "-L", self.socket_name, "rename-window", name],
            check=True,
            timeout=5
        )
        
    def get_windows(self) -> List[Window]:
        """Get list of all windows."""
        result = subprocess.run(
            ["tmux", "-L", self.socket_name, "list-windows", 
             "-F", "#{window_index}:#{window_name}:#{window_active}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5
        )
        
        windows = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':')
            windows.append(Window(
                index=int(parts[0]),
                name=parts[1],
                active=(parts[2] == '1')
            ))
        return windows
        
    def get_current_window(self) -> Window:
        """Get the currently active window."""
        windows = self.get_windows()
        for w in windows:
            if w.active:
                return w
        raise RuntimeError("No active window found")
        
    def get_current_window_index(self) -> int:
        """Get index of currently active window."""
        return self.get_current_window().index
        
    def get_window_count(self) -> int:
        """Get number of windows."""
        return len(self.get_windows())
        
    def capture_pane(self, start_line: int = 0, end_line: int = -1) -> str:
        """Capture content of current pane.
        
        Args:
            start_line: Starting line (0 = top of visible area, negative = scrollback)
            end_line: Ending line (-1 = bottom of visible area)
        """
        cmd = ["tmux", "-L", self.socket_name, "capture-pane", 
               "-p", "-S", str(start_line), "-E", str(end_line)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
        return result.stdout
        
    def wait_for(self, condition: Callable[[], bool], timeout: float = 5.0, 
                 poll_interval: float = 0.1, description: str = "condition"):
        """Wait for a condition to become true.
        
        Args:
            condition: Callable that returns True when condition is met
            timeout: Maximum time to wait
            poll_interval: Time between checks
            description: Description for error message
        """
        start = time.time()
        while time.time() - start < timeout:
            try:
                if condition():
                    return
            except Exception:
                pass
            time.sleep(poll_interval)
        raise TimeoutError(f"Timeout waiting for {description}")
        
    def wait_for_window_count(self, count: int, timeout: float = 5.0):
        """Wait for a specific number of windows."""
        self.wait_for(
            lambda: self.get_window_count() == count,
            timeout=timeout,
            description=f"window count to be {count}"
        )
        
    def wait_for_window_index(self, index: int, timeout: float = 5.0):
        """Wait for a specific window to be active."""
        self.wait_for(
            lambda: self.get_current_window_index() == index,
            timeout=timeout,
            description=f"window {index} to be active"
        )


class TestRunner:
    """Simple test runner with pass/fail tracking."""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []
        
    def run_test(self, name: str, test_func: Callable):
        """Run a single test."""
        print(f"  {name}...", end=" ", flush=True)
        try:
            test_func()
            print("✓")
            self.passed += 1
        except AssertionError as e:
            print(f"✗ FAILED: {e}")
            self.failed += 1
            self.errors.append((name, str(e)))
        except Exception as e:
            print(f"✗ ERROR: {e}")
            self.failed += 1
            self.errors.append((name, f"Error: {e}"))
            
    def summary(self):
        """Print summary and return exit code."""
        print()
        print(f"Results: {self.passed} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for name, error in self.errors:
                print(f"  - {name}: {error}")
        return 0 if self.failed == 0 else 1


def assert_eq(actual, expected, msg: str = ""):
    """Assert equality with nice message."""
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_true(condition, msg: str = ""):
    """Assert condition is true."""
    if not condition:
        raise AssertionError(msg or "condition was False")


# Example/default tests
def run_default_tests():
    """Run default test suite."""
    runner = TestRunner()
    
    print("Running kitty-claude tmux tests...")
    print()
    
    # Test: Basic window creation
    print("Window Creation Tests:")
    
    def test_initial_window():
        with TmuxTestHarness() as h:
            assert_eq(h.get_window_count(), 1, "Initial window count")
            assert_eq(h.get_current_window_index(), 1, "Initial window index")
    runner.run_test("initial_window", test_initial_window)
    
    def test_create_window_via_keybinding():
        with TmuxTestHarness() as h:
            h.ctrl('n')
            h.wait_for_window_count(2)
            assert_eq(h.get_window_count(), 2, "After Ctrl+N")
    runner.run_test("create_window_ctrl_n", test_create_window_via_keybinding)
    
    def test_create_multiple_windows():
        with TmuxTestHarness() as h:
            h.ctrl('n')
            h.wait_for_window_count(2)
            h.ctrl('n')
            h.wait_for_window_count(3)
            h.ctrl('n')
            h.wait_for_window_count(4)
            assert_eq(h.get_window_count(), 4, "After multiple Ctrl+N")
    runner.run_test("create_multiple_windows", test_create_multiple_windows)
    
    print()
    print("Tab Switching Tests:")
    
    def test_ctrl_k_next_window():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            # Start at window 3 (most recently created)
            assert_eq(h.get_current_window_index(), 3, "Start at window 3")
            # Go back to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            # Ctrl+K should go to next (window 2)
            h.ctrl('k')
            h.wait_for_window_index(2, timeout=2)
            assert_eq(h.get_current_window_index(), 2, "After Ctrl+K from 1")
    runner.run_test("ctrl_k_next_window", test_ctrl_k_next_window)
    
    def test_ctrl_j_previous_window():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            # We're at window 3
            assert_eq(h.get_current_window_index(), 3, "Start at window 3")
            # Ctrl+J should go to previous (window 2)
            h.ctrl('j')
            h.wait_for_window_index(2, timeout=2)
            assert_eq(h.get_current_window_index(), 2, "After Ctrl+J from 3")
    runner.run_test("ctrl_j_previous_window", test_ctrl_j_previous_window)
    
    def test_ctrl_k_wraps_around():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            # At window 3, Ctrl+K should wrap to window 1
            assert_eq(h.get_current_window_index(), 3, "Start at window 3")
            h.ctrl('k')
            h.wait_for_window_index(1, timeout=2)
            assert_eq(h.get_current_window_index(), 1, "Wrap from 3 to 1")
    runner.run_test("ctrl_k_wraps_around", test_ctrl_k_wraps_around)
    
    def test_ctrl_j_wraps_around():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            # Ctrl+J should wrap to window 3
            h.ctrl('j')
            h.wait_for_window_index(3, timeout=2)
            assert_eq(h.get_current_window_index(), 3, "Wrap from 1 to 3")
    runner.run_test("ctrl_j_wraps_around", test_ctrl_j_wraps_around)
    
    def test_alt_o_last_window():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            # We're at window 3
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            # Alt+O should go back to window 3 (last window)
            h.alt('o')
            h.wait_for_window_index(3, timeout=2)
            assert_eq(h.get_current_window_index(), 3, "Alt+O goes to last")
            # Alt+O again should go back to window 1
            h.alt('o')
            h.wait_for_window_index(1, timeout=2)
            assert_eq(h.get_current_window_index(), 1, "Alt+O toggles back")
    runner.run_test("alt_o_last_window", test_alt_o_last_window)
    
    print()
    print("Window Close Tests:")
    
    def test_ctrl_w_closes_window():
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            assert_eq(h.get_window_count(), 2, "Have 2 windows")
            h.ctrl('w')
            h.wait_for_window_count(1)
            assert_eq(h.get_window_count(), 1, "After Ctrl+W")
    runner.run_test("ctrl_w_closes_window", test_ctrl_w_closes_window)
    
    def test_ctrl_w_wont_close_last():
        with TmuxTestHarness() as h:
            assert_eq(h.get_window_count(), 1, "Only 1 window")
            h.ctrl('w')
            time.sleep(0.3)  # Give time for potential close
            assert_eq(h.get_window_count(), 1, "Still 1 window after Ctrl+W")
    runner.run_test("ctrl_w_wont_close_last", test_ctrl_w_wont_close_last)
    
    return runner.summary()


if __name__ == "__main__":
    sys.exit(run_default_tests())