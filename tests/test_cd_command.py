#!/usr/bin/env python3
"""Tests for :cd colon command."""

import subprocess
import tempfile
import time
import json
import shutil
from pathlib import Path
from harness import TmuxTestHarness, TestRunner, assert_eq, assert_true


class CdTestHarness(TmuxTestHarness):
    """Harness for testing :cd command."""

    def __init__(self):
        super().__init__()
        self.claude_data_dir = None
        self.test_message_file = None

    def start(self, initial_command: str = "bash"):
        super().start(initial_command)

        # Create a temp claude data directory for testing
        self.claude_data_dir = Path(tempfile.mkdtemp(prefix="claude-test-"))

        # Create a test file to capture tmux messages
        self.test_message_file = Path(tempfile.mkdtemp(prefix="cd-test-")) / "messages.txt"
        self.test_message_file.touch()

    def stop(self):
        super().stop()
        if self.claude_data_dir and self.claude_data_dir.exists():
            shutil.rmtree(self.claude_data_dir, ignore_errors=True)
        if self.test_message_file and self.test_message_file.exists():
            self.test_message_file.unlink()

    def send_cd_command(self, path: str):
        """Send :cd command to the hook handler.

        This simulates what happens when a user types :cd <path>.
        """
        # Create input data for the hook
        input_data = {
            "prompt": f":cd {path}",
            "cwd": "/tmp",
            "session_id": "test-session-123"
        }

        # Call the hook handler with test data
        input_json = json.dumps(input_data)
        cmd = [
            "python3", "-c",
            f"""
import sys
import json
import os
import io

# Set up environment
os.environ['CLAUDE_CONFIG_DIR'] = '{self.claude_data_dir}'
os.environ['KITTY_CLAUDE_TMUX_SOCKET'] = '{self.socket_name}'

# Import the handler
sys.path.insert(0, '/home/bruger/mine/kitty-claude')
from kitty_claude.colon_command import handle_user_prompt_submit

# Mock stdin with our data
input_json_str = '''{input_json.replace("'", "\\'")}'''
sys.stdin = io.StringIO(input_json_str)

# Call the handler
handle_user_prompt_submit(claude_data_dir='{self.claude_data_dir}')
"""
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5
        )

        return result

    def get_last_tmux_message(self):
        """Get the last tmux display-message that was sent."""
        # We'll capture this by checking tmux messages
        # For now, we'll rely on the return value from the command
        return None


def run_cd_tests():
    """Test :cd command functionality."""
    runner = TestRunner()

    print(":cd Command Tests")
    print("=" * 50)
    print()

    print("Directory Validation:")

    def test_cd_to_nonexistent_directory():
        """Test that :cd warns when directory doesn't exist."""
        with CdTestHarness() as h:
            # Try to cd to a directory that doesn't exist
            nonexistent_dir = "/tmp/this-directory-definitely-does-not-exist-12345"

            # Ensure it doesn't exist
            assert_true(not Path(nonexistent_dir).exists(),
                       "Test directory should not exist")

            result = h.send_cd_command(nonexistent_dir)

            # Check that the output contains an error message
            output = result.stdout + result.stderr
            assert_true("does not exist" in output.lower() or "directory" in output.lower(),
                       f"Should warn about non-existent directory. Output: {output}")

    runner.run_test("cd_to_nonexistent_directory", test_cd_to_nonexistent_directory)

    def test_cd_to_existing_directory():
        """Test that :cd works with an existing directory."""
        with CdTestHarness() as h:
            # Use /tmp which should always exist
            result = h.send_cd_command("/tmp")

            # Should not contain error about non-existent directory
            output = result.stdout + result.stderr
            assert_true("does not exist" not in output.lower(),
                       f"Should not warn for existing directory. Output: {output}")

    runner.run_test("cd_to_existing_directory", test_cd_to_existing_directory)

    def test_cd_with_tilde_expansion():
        """Test that :cd expands ~ correctly."""
        with CdTestHarness() as h:
            # Try cd with ~
            result = h.send_cd_command("~")

            # Should not error (home directory should exist)
            output = result.stdout + result.stderr
            assert_true("does not exist" not in output.lower(),
                       f"Should expand ~ to home directory. Output: {output}")

    runner.run_test("cd_with_tilde_expansion", test_cd_with_tilde_expansion)

    def test_cd_with_relative_path_to_nonexistent():
        """Test that :cd validates relative paths."""
        with CdTestHarness() as h:
            # Try a relative path that doesn't exist
            result = h.send_cd_command("./nonexistent-subdir-xyz")

            output = result.stdout + result.stderr
            assert_true("does not exist" in output.lower() or "directory" in output.lower(),
                       f"Should warn about non-existent relative path. Output: {output}")

    runner.run_test("cd_with_relative_path_to_nonexistent", test_cd_with_relative_path_to_nonexistent)

    return runner.summary()


if __name__ == "__main__":
    import sys
    sys.exit(run_cd_tests())
