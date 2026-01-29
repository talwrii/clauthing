#!/usr/bin/env python3
"""End-to-end tests for :cd command using real kitty-claude + claude.

These tests spin up actual kitty-claude instances (without kitty) and
test the full :cd flow including hooks and respawn.
"""

import subprocess
import tempfile
import time
import os
import sys
from pathlib import Path

try:
    import pexpect
except ImportError:
    print("Error: pexpect not installed. Run: pip install pexpect")
    sys.exit(1)

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent))

from harness import TestRunner, assert_eq, assert_true


class KittyClaudeInstance:
    """Manages a kitty-claude --one-tab --no-kitty instance for testing."""
    
    def __init__(self, profile=None):
        self.profile = profile
        self.socket_name = None
        self.process = None
        self.target_dirs = []
        
    def start(self, timeout=15):
        """Start kitty-claude in one-tab mode without kitty."""
        cmd = "kitty-claude --one-tab --no-kitty"
        if self.profile:
            cmd += f" --profile {self.profile}"
        
        print(f"Starting: {cmd}")
        
        # Use pexpect to spawn with a PTY (tmux needs this)
        self.process = pexpect.spawn(cmd, encoding='utf-8', timeout=timeout)
        
        # Wait for tmux socket to appear
        start_time = time.time()
        while time.time() - start_time < timeout:
            sockets = self._find_sockets()
            if sockets:
                self.socket_name = sockets[0]
                print(f"Found socket: {self.socket_name}")
                
                if self._session_ready():
                    print("Session ready")
                    return True
            
            time.sleep(0.5)
        
        # Timeout - gather debug info
        raise TimeoutError(f"kitty-claude did not start in time. Socket: {self.socket_name}")
    
    def _find_sockets(self):
        """Find kc1-* tmux sockets (excluding test harness sockets)."""
        uid = os.getuid()
        tmpdir = os.environ.get('TMUX_TMPDIR', '/tmp')
        socket_dir = Path(tmpdir) / f"tmux-{uid}"
        
        sockets = []
        
        if socket_dir.exists():
            for sock in socket_dir.iterdir():
                # Skip test harness sockets (they have "test" in name)
                # Real kitty-claude sockets are like: kc1-{timestamp}-{pid}
                if sock.name.startswith("kc1-") and "-test-" not in sock.name:
                    # Only include if socket file exists and is recent (created in last 30s)
                    try:
                        age = time.time() - sock.stat().st_ctime
                        if age < 30:
                            sockets.append(sock.name)
                    except:
                        pass
        
        return sockets
    
    def _session_ready(self):
        """Check if tmux session is ready."""
        if not self.socket_name:
            return False
        
        result = subprocess.run(
            ["tmux", "-L", self.socket_name, "has-session"],
            capture_output=True
        )
        return result.returncode == 0
    
    def send_keys(self, keys, enter=True):
        """Send keys to the tmux pane."""
        cmd = ["tmux", "-L", self.socket_name, "send-keys", keys]
        if enter:
            cmd.append("Enter")
        subprocess.run(cmd, check=True)
        
    def capture_pane(self):
        """Capture current pane content."""
        result = subprocess.run(
            ["tmux", "-L", self.socket_name, "capture-pane", "-p"],
            capture_output=True,
            text=True
        )
        return result.stdout
    
    def wait_for_text(self, text, timeout=10):
        """Wait for text to appear in pane."""
        start = time.time()
        while time.time() - start < timeout:
            content = self.capture_pane()
            if text in content:
                return True
            time.sleep(0.2)
        return False
    
    def wait_for_claude_ready(self, timeout=30):
        """Wait for claude to be ready (showing prompt)."""
        # Claude shows ">" when ready, or we might see the welcome message
        start = time.time()
        while time.time() - start < timeout:
            content = self.capture_pane()
            # Look for signs claude is ready
            if ">" in content or "Claude" in content or "?" in content:
                # Give it a moment more to fully initialize
                time.sleep(1)
                return True
            time.sleep(0.5)
        return False
    
    def get_launcher_scripts(self):
        """Find launcher scripts created by :cd in one-tab mode."""
        uid = os.getuid()
        return list(Path("/tmp").glob(f"kc-cd-{uid}-*.sh"))
    
    def cleanup_launchers(self):
        """Remove launcher scripts."""
        for script in self.get_launcher_scripts():
            try:
                script.unlink()
            except:
                pass
    
    def stop(self):
        """Stop the kitty-claude instance."""
        if self.socket_name:
            # Kill the tmux server
            subprocess.run(
                ["tmux", "-L", self.socket_name, "kill-server"],
                capture_output=True
            )
        
        if self.process:
            try:
                self.process.terminate(force=True)
            except:
                pass
        
        # Cleanup target directories
        import shutil
        for d in self.target_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        
        self.cleanup_launchers()
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, *args):
        self.stop()


def run_e2e_cd_tests():
    """Run end-to-end :cd tests with real claude."""
    runner = TestRunner()
    
    print("End-to-End :cd Tests (Real Claude)")
    print("=" * 50)
    print()
    print("NOTE: These tests use real claude authentication.")
    print("      They may take longer due to claude startup time.")
    print()
    
    # Check if kitty-claude is available
    if not subprocess.run(["which", "kitty-claude"], capture_output=True).returncode == 0:
        print("SKIP: kitty-claude not found in PATH")
        return 0
    
    # =========================================
    # Test: Basic startup
    # =========================================
    print("Startup Tests:")
    
    def test_kitty_claude_starts():
        """Test that kitty-claude --one-tab --no-kitty starts successfully."""
        instance = KittyClaudeInstance()
        try:
            instance.start(timeout=15)
            assert_true(instance.socket_name is not None, "Should have socket")
            assert_true(instance.socket_name.startswith("kc1-"), 
                       f"Socket should start with kc1-: {instance.socket_name}")
        finally:
            instance.stop()
    runner.run_test("kitty_claude_starts", test_kitty_claude_starts)
    
    def test_claude_becomes_ready():
        """Test that claude starts and becomes ready."""
        instance = KittyClaudeInstance()
        try:
            instance.start(timeout=15)
            ready = instance.wait_for_claude_ready(timeout=30)
            assert_true(ready, "Claude should become ready")
        finally:
            instance.stop()
    runner.run_test("claude_becomes_ready", test_claude_becomes_ready)
    
    # =========================================
    # Test: :cd command
    # =========================================
    print()
    print(":cd Command Tests:")
    
    def test_cd_to_valid_directory():
        """Test :cd to a valid directory creates launcher script."""
        instance = KittyClaudeInstance()
        instance.cleanup_launchers()
        
        try:
            instance.start(timeout=15)
            
            # Wait for claude to be ready
            ready = instance.wait_for_claude_ready(timeout=30)
            assert_true(ready, "Claude should be ready")
            
            # Create target directory
            target = Path(tempfile.mkdtemp(prefix="cd-e2e-target-"))
            instance.target_dirs.append(target)
            
            # Send :cd command
            instance.send_keys(f":cd {target}")
            
            # Wait for processing
            time.sleep(3)
            
            # In one-tab mode, should create launcher script
            launchers = instance.get_launcher_scripts()
            assert_true(len(launchers) > 0, 
                       f"Should create launcher script. Pane content:\n{instance.capture_pane()}")
            
            # Verify launcher content
            content = launchers[0].read_text()
            assert_true(str(target) in content, 
                       f"Launcher should cd to target: {content}")
            assert_true("claude --resume" in content,
                       f"Launcher should resume claude: {content}")
            
        finally:
            instance.stop()
    runner.run_test("cd_to_valid_directory", test_cd_to_valid_directory)
    
    def test_cd_to_nonexistent_directory():
        """Test :cd to nonexistent directory shows error, no launcher."""
        instance = KittyClaudeInstance()
        instance.cleanup_launchers()
        
        try:
            instance.start(timeout=15)
            
            # Wait for claude to be ready
            ready = instance.wait_for_claude_ready(timeout=30)
            assert_true(ready, "Claude should be ready")
            
            # Send :cd to nonexistent path
            nonexistent = "/tmp/this-path-definitely-does-not-exist-e2e-xyz-123"
            assert_true(not Path(nonexistent).exists(), "Path should not exist")
            
            instance.send_keys(f":cd {nonexistent}")
            
            # Wait for processing
            time.sleep(2)
            
            # Should NOT create launcher
            launchers = instance.get_launcher_scripts()
            assert_eq(len(launchers), 0,
                     f"Should NOT create launcher for nonexistent dir. Pane:\n{instance.capture_pane()}")
            
            # Should show error in pane
            content = instance.capture_pane()
            assert_true("does not exist" in content.lower() or "error" in content.lower() or "not" in content.lower(),
                       f"Should show error message. Pane:\n{content}")
            
        finally:
            instance.stop()
    runner.run_test("cd_to_nonexistent_directory", test_cd_to_nonexistent_directory)
    
    def test_cd_with_tilde():
        """Test :cd ~ expands to home directory."""
        instance = KittyClaudeInstance()
        instance.cleanup_launchers()
        
        try:
            instance.start(timeout=15)
            
            ready = instance.wait_for_claude_ready(timeout=30)
            assert_true(ready, "Claude should be ready")
            
            # Send :cd ~
            instance.send_keys(":cd ~")
            
            # Wait for processing
            time.sleep(3)
            
            # Should create launcher with home dir
            launchers = instance.get_launcher_scripts()
            assert_true(len(launchers) > 0,
                       f"Should create launcher for ~. Pane:\n{instance.capture_pane()}")
            
            content = launchers[0].read_text()
            home = str(Path.home())
            assert_true(home in content,
                       f"Launcher should cd to home ({home}): {content}")
            
        finally:
            instance.stop()
    runner.run_test("cd_with_tilde", test_cd_with_tilde)
    
    return runner.summary()


if __name__ == "__main__":
    sys.exit(run_e2e_cd_tests())