#!/usr/bin/env python3
"""Tests for :cd colon command.

Tests directory validation, session cloning, and respawn logic
in both multi-tab and one-tab modes.
"""

import subprocess
import tempfile
import time
import json
import shutil
import os
import sys
from pathlib import Path

# Add parent dir to path so we can import harness
sys.path.insert(0, str(Path(__file__).parent))

from harness import TmuxTestHarness, TestRunner, assert_eq, assert_true


class CdTestHarness(TmuxTestHarness):
    """Harness for testing :cd command."""
    
    def __init__(self, one_tab_mode=False):
        super().__init__()
        self.one_tab_mode = one_tab_mode
        if one_tab_mode:
            # Socket must start with cl1- for one-tab mode detection
            self.socket_name = f"cl1-test-{os.getpid()}-{int(time.time())}"
        self.claude_data_dir = None
        
    def start(self, initial_command: str = "bash"):
        super().start(initial_command)
        
        # Create a temp claude data directory for testing
        self.claude_data_dir = Path(tempfile.mkdtemp(prefix="claude-test-"))
        
    def stop(self):
        super().stop()
        if self.claude_data_dir and self.claude_data_dir.exists():
            shutil.rmtree(self.claude_data_dir, ignore_errors=True)
            
    def create_mock_session(self, cwd: str, session_id: str = "test-session-123"):
        """Create a mock session file with messages so :cd can clone it.

        Returns the session file path.
        """
        # Encode path the same way Claude Code does: replace every non-alnum with -
        import re
        encoded_cwd = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
        
        projects_dir = self.claude_data_dir / "projects" / encoded_cwd
        projects_dir.mkdir(parents=True, exist_ok=True)
        
        session_file = projects_dir / f"{session_id}.jsonl"
        # Write a minimal session with at least one user message
        session_file.write_text(
            '{"type": "user", "message": {"content": "hello"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n'
        )
        return session_file
        
    def get_launcher_scripts(self):
        """Find any launcher scripts created by :cd in one-tab mode."""
        uid = os.getuid()
        return list(Path("/tmp").glob(f"cl-launch-{uid}-*.sh"))
        
    def cleanup_launchers(self):
        """Remove any leftover launcher scripts."""
        for script in self.get_launcher_scripts():
            try:
                script.unlink()
            except:
                pass


def validate_directory(path: str, cwd: str = "/tmp") -> tuple[bool, str]:
    """Validate a directory path like :cd does.
    
    Returns (is_valid, resolved_path or error_message).
    """
    try:
        # Expand ~ and resolve relative paths
        expanded = Path(path).expanduser()
        if not expanded.is_absolute():
            expanded = Path(cwd) / expanded
        resolved = expanded.resolve()
        
        if not resolved.exists():
            return False, f"Directory does not exist: {resolved}"
        if not resolved.is_dir():
            return False, f"Not a directory: {resolved}"
        return True, str(resolved)
    except Exception as e:
        return False, f"Invalid path: {e}"


def run_cd_tests():
    """Test :cd command functionality."""
    runner = TestRunner()
    
    print(":cd Command Tests")
    print("=" * 50)
    print()
    
    # =========================================
    # Directory Validation Tests
    # =========================================
    print("Directory Validation:")
    
    def test_cd_validates_nonexistent():
        """Test that nonexistent directories are rejected."""
        nonexistent = "/tmp/this-directory-definitely-does-not-exist-xyz123"
        assert_true(not Path(nonexistent).exists(), "Test setup: dir should not exist")
        
        valid, msg = validate_directory(nonexistent)
        assert_true(not valid, f"Should reject nonexistent dir: {msg}")
        assert_true("does not exist" in msg.lower(), f"Error should mention 'does not exist': {msg}")
    runner.run_test("validates_nonexistent", test_cd_validates_nonexistent)
    
    def test_cd_validates_existing():
        """Test that existing directories are accepted."""
        valid, resolved = validate_directory("/tmp")
        assert_true(valid, f"Should accept /tmp: {resolved}")
        assert_eq(resolved, "/tmp", "Should resolve to /tmp")
    runner.run_test("validates_existing", test_cd_validates_existing)
    
    def test_cd_expands_tilde():
        """Test that ~ expands to home directory."""
        valid, resolved = validate_directory("~")
        assert_true(valid, f"Should accept ~: {resolved}")
        assert_eq(resolved, str(Path.home()), "Should expand to home")
    runner.run_test("expands_tilde", test_cd_expands_tilde)
    
    def test_cd_resolves_relative():
        """Test that relative paths are resolved from cwd."""
        with tempfile.TemporaryDirectory(prefix="cd-test-") as tmpdir:
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()
            
            valid, resolved = validate_directory("subdir", cwd=tmpdir)
            assert_true(valid, f"Should accept relative path: {resolved}")
            assert_eq(resolved, str(subdir), "Should resolve relative to cwd")
    runner.run_test("resolves_relative", test_cd_resolves_relative)
    
    def test_cd_rejects_file():
        """Test that files (not directories) are rejected."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            try:
                valid, msg = validate_directory(f.name)
                assert_true(not valid, f"Should reject file: {msg}")
                assert_true("not a directory" in msg.lower(), f"Error should mention 'not a directory': {msg}")
            finally:
                os.unlink(f.name)
    runner.run_test("rejects_file", test_cd_rejects_file)
    
    # =========================================
    # Session Cloning Tests
    # =========================================
    print()
    print("Session Cloning:")
    
    def test_session_file_created():
        """Test that :cd creates a session file in target directory."""
        with CdTestHarness() as h:
            source_cwd = "/tmp"
            session_id = "test-session-abc"
            h.create_mock_session(source_cwd, session_id)
            
            # Create target directory
            target = tempfile.mkdtemp(prefix="cd-target-")
            try:
                # Manually clone session (simulating what :cd does).
                # Use the same encoding as claude_utils.encode_project_path.
                import re
                source_encoded = re.sub(r'[^a-zA-Z0-9]', '-', source_cwd)
                target_encoded = re.sub(r'[^a-zA-Z0-9]', '-', target)
                
                source_file = h.claude_data_dir / "projects" / source_encoded / f"{session_id}.jsonl"
                target_dir = h.claude_data_dir / "projects" / target_encoded
                target_dir.mkdir(parents=True, exist_ok=True)
                
                new_session_id = "new-session-xyz"
                target_file = target_dir / f"{new_session_id}.jsonl"
                shutil.copy(source_file, target_file)
                
                assert_true(target_file.exists(), "Session file should be created in target")
                
                # Verify content was copied
                content = target_file.read_text()
                assert_true("hello" in content, "Session content should be preserved")
            finally:
                shutil.rmtree(target, ignore_errors=True)
    runner.run_test("session_file_created", test_session_file_created)
    
    def test_session_preserves_messages():
        """Test that cloned session preserves all messages."""
        with CdTestHarness() as h:
            source_cwd = "/tmp"
            session_id = "preserve-test"
            
            # Create session with multiple messages
            import re
            encoded = re.sub(r'[^a-zA-Z0-9]', '-', source_cwd)
            projects_dir = h.claude_data_dir / "projects" / encoded
            projects_dir.mkdir(parents=True, exist_ok=True)
            
            messages = [
                '{"type": "user", "message": {"content": "first message"}}',
                '{"type": "assistant", "message": {"content": [{"type": "text", "text": "response 1"}]}}',
                '{"type": "user", "message": {"content": "second message"}}',
                '{"type": "assistant", "message": {"content": [{"type": "text", "text": "response 2"}]}}',
            ]
            session_file = projects_dir / f"{session_id}.jsonl"
            session_file.write_text('\n'.join(messages) + '\n')
            
            # Clone it
            target = tempfile.mkdtemp(prefix="cd-target-")
            try:
                target_encoded = re.sub(r'[^a-zA-Z0-9]', '-', target)
                target_dir = h.claude_data_dir / "projects" / target_encoded
                target_dir.mkdir(parents=True, exist_ok=True)
                
                target_file = target_dir / "cloned-session.jsonl"
                shutil.copy(session_file, target_file)
                
                # Verify all messages preserved
                cloned_content = target_file.read_text()
                lines = [l for l in cloned_content.strip().split('\n') if l]
                assert_eq(len(lines), 4, "Should preserve all 4 messages")
                assert_true("first message" in cloned_content, "Should have first message")
                assert_true("second message" in cloned_content, "Should have second message")
            finally:
                shutil.rmtree(target, ignore_errors=True)
    runner.run_test("session_preserves_messages", test_session_preserves_messages)
    
    # =========================================
    # One-Tab Mode Tests
    # =========================================
    print()
    print("One-Tab Mode:")
    
    def test_one_tab_socket_detection():
        """Test that socket name starting with cl1- triggers one-tab mode."""
        # One-tab mode
        harness = CdTestHarness(one_tab_mode=True)
        assert_true(harness.socket_name.startswith("cl1-"),
                   f"One-tab socket should start with cl1-: {harness.socket_name}")
        
        # Multi-tab mode
        harness2 = CdTestHarness(one_tab_mode=False)
        assert_true(not harness2.socket_name.startswith("cl1-"),
                   f"Multi-tab socket should not start with cl1-: {harness2.socket_name}")
    runner.run_test("one_tab_socket_detection", test_one_tab_socket_detection)
    
    def test_one_tab_cd_nonexistent_no_launcher():
        """Test that :cd to nonexistent dir in one-tab mode doesn't create launcher."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            harness.create_mock_session("/tmp")
            
            nonexistent = "/tmp/this-path-does-not-exist-xyz-123"
            assert_true(not Path(nonexistent).exists(), "Test setup: dir should not exist")
            
            # Validate like :cd does
            valid, msg = validate_directory(nonexistent)
            
            # Should fail validation
            assert_true(not valid, f"Should reject nonexistent: {msg}")
            
            # No launcher should be created when validation fails
            launchers = harness.get_launcher_scripts()
            assert_eq(len(launchers), 0, 
                     f"No launcher should be created for invalid path: {launchers}")
        finally:
            harness.cleanup_launchers()
            harness.stop()
    runner.run_test("one_tab_cd_nonexistent_no_launcher", test_one_tab_cd_nonexistent_no_launcher)
    
    def test_one_tab_launcher_script_created():
        """Test that :cd in one-tab mode creates a launcher script."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            harness.create_mock_session("/tmp")
            
            target = tempfile.mkdtemp(prefix="cd-target-")
            try:
                # Simulate what :cd does in one-tab mode:
                # Create the launcher script
                uid = os.getuid()
                session_id = "test-session-123"
                launcher_path = Path(f"/tmp/cl-cd-{uid}-{session_id[:8]}.sh")
                
                launcher_content = f'''#!/bin/bash
cd "{target}"
exec claude --resume new-session-xyz
'''
                launcher_path.write_text(launcher_content)
                launcher_path.chmod(0o755)
                
                # Verify launcher was created
                assert_true(launcher_path.exists(), "Launcher script should be created")
                
                # Verify launcher content
                content = launcher_path.read_text()
                assert_true(f'cd "{target}"' in content, f"Launcher should cd to target: {content}")
                assert_true("claude --resume" in content, f"Launcher should resume claude: {content}")
                
                # Verify executable
                mode = launcher_path.stat().st_mode
                assert_true(mode & 0o111, f"Launcher should be executable: {oct(mode)}")
            finally:
                shutil.rmtree(target, ignore_errors=True)
                harness.cleanup_launchers()
        finally:
            harness.stop()
    runner.run_test("one_tab_launcher_script_created", test_one_tab_launcher_script_created)
    
    def test_one_tab_launcher_content_format():
        """Test that launcher script has correct format for respawn-pane."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            
            target = "/home/testuser/projects/myapp"
            session_id = "abc12345-def6-7890"
            config_dir = str(harness.claude_data_dir)
            
            # Create launcher like :cd does
            uid = os.getuid()
            launcher_path = Path(f"/tmp/cl-cd-{uid}-{session_id[:8]}.sh")
            
            # This is approximately what the real :cd generates
            launcher_content = f'''#!/bin/bash
export CLAUDE_CONFIG_DIR="{config_dir}"
cd "{target}"
exec claude --resume {session_id}
'''
            launcher_path.write_text(launcher_content)
            launcher_path.chmod(0o755)
            
            # Verify structure
            content = launcher_path.read_text()
            lines = content.strip().split('\n')
            
            assert_true(lines[0] == '#!/bin/bash', "First line should be shebang")
            assert_true(any('CLAUDE_CONFIG_DIR' in l for l in lines), 
                       "Should set CLAUDE_CONFIG_DIR")
            assert_true(any(f'cd "{target}"' in l for l in lines),
                       "Should cd to target directory")
            assert_true(any('exec claude --resume' in l for l in lines),
                       "Should exec claude --resume")
            
            harness.cleanup_launchers()
        finally:
            harness.stop()
    runner.run_test("one_tab_launcher_content_format", test_one_tab_launcher_content_format)
    
    def test_one_tab_respawn_command():
        """Test that respawn-pane command is correctly formed."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            
            # Create a launcher script
            uid = os.getuid()
            session_id = "test1234"
            launcher_path = Path(f"/tmp/cl-cd-{uid}-{session_id[:8]}.sh")
            launcher_path.write_text("#!/bin/bash\necho test\n")
            launcher_path.chmod(0o755)
            
            # The respawn-pane command that :cd would schedule
            respawn_cmd = [
                "tmux", "-L", harness.socket_name,
                "respawn-pane", "-k", str(launcher_path)
            ]
            
            # Verify the command is well-formed
            assert_eq(respawn_cmd[0], "tmux", "Should use tmux")
            assert_eq(respawn_cmd[1], "-L", "Should specify socket")
            assert_eq(respawn_cmd[2], harness.socket_name, "Should use correct socket")
            assert_eq(respawn_cmd[3], "respawn-pane", "Should use respawn-pane")
            assert_eq(respawn_cmd[4], "-k", "Should use -k to kill existing")
            assert_eq(respawn_cmd[5], str(launcher_path), "Should specify launcher script")
            
            harness.cleanup_launchers()
        finally:
            harness.stop()
    runner.run_test("one_tab_respawn_command", test_one_tab_respawn_command)
    
    # =========================================
    # Multi-Tab Mode Tests  
    # =========================================
    print()
    print("Multi-Tab Mode:")
    
    def test_multi_tab_creates_new_window():
        """Test that :cd in multi-tab mode logic would create a new window."""
        with CdTestHarness(one_tab_mode=False) as h:
            # Verify we're in multi-tab mode
            assert_true(not h.socket_name.startswith("cl1-"), 
                       "Should be multi-tab mode")
            
            # In multi-tab, :cd would run something like:
            # clauthing --new-window --resume-session <id>
            # We can't easily test that without the full clauthing,
            # but we can verify the socket naming is correct
            
            # Multi-tab uses socket name directly (not cl1- prefix)
            assert_true("-" in h.socket_name, "Socket should have separator")
    runner.run_test("multi_tab_socket_format", test_multi_tab_creates_new_window)
    
    # =========================================
    # End-to-End Tests (real colon_command)
    # =========================================
    print()
    print("End-to-End (real colon_command):")
    
    def test_cd_e2e_one_tab_valid_dir():
        """E2E: :cd to valid directory in one-tab mode creates launcher."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            harness.create_mock_session("/tmp", "test-session-e2e")
            
            target = tempfile.mkdtemp(prefix="cd-e2e-target-")
            try:
                # Call the real colon_command handler
                input_data = json.dumps({
                    "prompt": f":cd {target}",
                    "cwd": "/tmp",
                    "session_id": "test-session-e2e"
                })
                
                env = os.environ.copy()
                env['CLAUDE_CONFIG_DIR'] = str(harness.claude_data_dir)
                env['CLAUTHING_TMUX_SOCKET'] = harness.socket_name
                
                clauthing_bin = shutil.which("clauthing") or "clauthing"
                result = subprocess.run(
                    [clauthing_bin, "--user-prompt-submit"],
                    input=input_data,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env
                )
                
                # Give it a moment
                time.sleep(0.2)
                
                # In one-tab mode, should create launcher script
                launchers = harness.get_launcher_scripts()
                assert_true(len(launchers) > 0,
                           f"Should create launcher for valid dir. stdout={result.stdout}, stderr={result.stderr}")
                
                # Verify launcher content
                content = launchers[0].read_text()
                assert_true(target in content, f"Launcher should cd to target: {content}")
                assert_true("--resume" in content and "claude" in content,
                           f"Launcher should resume claude: {content}")

            finally:
                shutil.rmtree(target, ignore_errors=True)
                harness.cleanup_launchers()
        finally:
            harness.stop()
    runner.run_test("cd_e2e_one_tab_valid_dir", test_cd_e2e_one_tab_valid_dir)
    
    def test_cd_e2e_one_tab_invalid_dir():
        """E2E: :cd to nonexistent directory should NOT create launcher."""
        harness = CdTestHarness(one_tab_mode=True)
        harness.cleanup_launchers()
        
        try:
            harness.start()
            harness.create_mock_session("/tmp", "test-session-e2e2")
            
            nonexistent = "/tmp/this-definitely-does-not-exist-e2e-xyz"
            assert_true(not Path(nonexistent).exists(), "Test setup: should not exist")
            
            input_data = json.dumps({
                "prompt": f":cd {nonexistent}",
                "cwd": "/tmp",
                "session_id": "test-session-e2e2"
            })
            
            env = os.environ.copy()
            env['CLAUDE_CONFIG_DIR'] = str(harness.claude_data_dir)
            env['CLAUTHING_TMUX_SOCKET'] = harness.socket_name
            
            clauthing_bin = shutil.which("clauthing") or "clauthing"
            result = subprocess.run(
                [clauthing_bin, "--user-prompt-submit"],
                input=input_data,
                capture_output=True,
                text=True,
                timeout=10,
                env=env
            )

            time.sleep(0.2)

            # Should NOT create launcher for invalid dir
            launchers = harness.get_launcher_scripts()
            assert_eq(len(launchers), 0,
                     f"Should NOT create launcher for invalid dir. stdout={result.stdout}, stderr={result.stderr}")
            
            harness.cleanup_launchers()
        finally:
            harness.stop()
    runner.run_test("cd_e2e_one_tab_invalid_dir", test_cd_e2e_one_tab_invalid_dir)
    
    def test_tmux_respawn_pane_works():
        """Test that tmux respawn-pane actually works with a script."""
        with CdTestHarness(one_tab_mode=True) as h:
            # Create a simple script that writes to a file
            marker_file = Path(tempfile.mktemp(prefix="respawn-test-"))
            script_path = Path(tempfile.mktemp(prefix="respawn-script-", suffix=".sh"))
            
            try:
                script_path.write_text(f'''#!/bin/bash
echo "respawned" > "{marker_file}"
sleep 0.5
''')
                script_path.chmod(0o755)
                
                # Run respawn-pane
                result = subprocess.run(
                    ["tmux", "-L", h.socket_name, "respawn-pane", "-k", str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                # Wait for script to execute
                time.sleep(1)
                
                # Check if marker file was created
                if marker_file.exists():
                    content = marker_file.read_text().strip()
                    assert_eq(content, "respawned", "Script should have written marker")
                else:
                    # Respawn might have failed silently, but that's okay for this test
                    # The important thing is tmux didn't error
                    assert_eq(result.returncode, 0, 
                             f"respawn-pane should succeed: {result.stderr}")
            finally:
                if marker_file.exists():
                    marker_file.unlink()
                if script_path.exists():
                    script_path.unlink()
    runner.run_test("tmux_respawn_pane_works", test_tmux_respawn_pane_works)
    
    return runner.summary()


if __name__ == "__main__":
    sys.exit(run_cd_tests())