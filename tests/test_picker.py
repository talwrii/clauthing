#!/usr/bin/env python3
"""Tests for Ctrl+P session picker functionality."""

import subprocess
import tempfile
import time
import shutil
from pathlib import Path
from harness import TmuxTestHarness, TestRunner, assert_eq, assert_true


class PickerTestHarness(TmuxTestHarness):
    """Harness that sets up Ctrl+P with testable behavior."""
    
    def __init__(self):
        super().__init__()
        self.marker_dir = None
        
    def start(self, initial_command: str = "bash"):
        super().start(initial_command)
        
        # Create marker directory
        self.marker_dir = Path(tempfile.mkdtemp(prefix="picker-test-"))
        
        # Rebind C-p to create a marker file (so we can verify it ran)
        subprocess.run([
            "tmux", "-L", self.socket_name,
            "bind", "-n", "C-p",
            "display-popup", "-E", f"touch {self.marker_dir}/popup_ran; sleep 0.3"
        ], check=True)
        
    def stop(self):
        super().stop()
        if self.marker_dir and self.marker_dir.exists():
            shutil.rmtree(self.marker_dir, ignore_errors=True)
            
    def popup_was_triggered(self) -> bool:
        """Check if the popup was triggered."""
        marker = self.marker_dir / "popup_ran"
        return marker.exists()
        
    def reset_popup_marker(self):
        """Reset the popup marker for another test."""
        marker = self.marker_dir / "popup_ran"
        if marker.exists():
            marker.unlink()


class FzfPickerHarness(TmuxTestHarness):
    """Harness with fzf-based window picker like real kitty-claude."""
    
    def start(self, initial_command: str = "bash"):
        super().start(initial_command)
        
        # Rebind C-p to use fzf menu that selects a window
        # This mimics what kitty-claude does
        subprocess.run([
            "tmux", "-L", self.socket_name,
            "bind", "-n", "C-p",
            "display-popup", "-E", "-w", "80%", "-h", "60%",
            f"tmux -L {self.socket_name} list-windows -F '#{{window_index}}:#{{window_name}}' | "
            f"fzf --height=100% --reverse | cut -d: -f1 | "
            f"xargs -I{{}} tmux -L {self.socket_name} select-window -t {{}}"
        ], check=True)
        
    def open_picker_and_select(self, search_text: str, timeout: float = 2.0):
        """Open picker and select an item by typing search text.
        
        Args:
            search_text: Text to type in fzf to filter/select
            timeout: How long to wait for selection to complete
        """
        self.ctrl('p')
        time.sleep(0.3)  # Wait for popup to open
        
        self.send_text(search_text)
        time.sleep(0.2)  # Wait for fzf to filter
        
        self.press_enter()
        time.sleep(0.3)  # Wait for selection and window switch
        
    def open_picker_and_arrow_select(self, down_presses: int = 0):
        """Open picker and select using arrow keys.
        
        Args:
            down_presses: Number of times to press down arrow
        """
        self.ctrl('p')
        time.sleep(0.3)
        
        for _ in range(down_presses):
            self.send_keys("Down")
            time.sleep(0.1)
            
        self.press_enter()
        time.sleep(0.3)


def run_picker_tests():
    """Test Ctrl+P picker functionality."""
    runner = TestRunner()
    
    print("Ctrl+P Picker Tests")
    print("=" * 50)
    print()
    
    print("Basic Functionality:")
    
    def test_ctrl_p_binding_exists():
        """Verify Ctrl+P is bound."""
        with TmuxTestHarness() as h:
            result = subprocess.run(
                ["tmux", "-L", h.socket_name, "list-keys", "-T", "root"],
                capture_output=True, text=True
            )
            assert_true("C-p" in result.stdout, "C-p binding should exist")
            print()
            for line in result.stdout.split("\n"):
                if "C-p" in line:
                    print(f"    {line}")
    runner.run_test("ctrl_p_binding_exists", test_ctrl_p_binding_exists)
    
    def test_ctrl_p_triggers_popup():
        """Test that Ctrl+P triggers the popup."""
        with PickerTestHarness() as h:
            assert_true(not h.popup_was_triggered(), "Popup not triggered yet")
            
            h.ctrl('p')
            time.sleep(0.8)  # Wait for popup to run and close
            
            assert_true(h.popup_was_triggered(), "Popup should have been triggered")
    runner.run_test("ctrl_p_triggers_popup", test_ctrl_p_triggers_popup)
    
    def test_ctrl_p_multiple_times():
        """Test Ctrl+P can be triggered multiple times."""
        with PickerTestHarness() as h:
            for i in range(3):
                h.reset_popup_marker()
                h.ctrl('p')
                time.sleep(0.8)
                assert_true(h.popup_was_triggered(), f"Popup triggered on attempt {i+1}")
    runner.run_test("ctrl_p_multiple_times", test_ctrl_p_multiple_times)
    
    print()
    print("Interaction with Other Keys:")
    
    def test_ctrl_p_after_navigation():
        """Test Ctrl+P works after navigating windows."""
        with PickerTestHarness() as h:
            # Create windows
            h.new_window(name="win2")
            h.new_window(name="win3")
            
            # Navigate around
            h.ctrl('j')
            time.sleep(0.1)
            h.ctrl('k')
            time.sleep(0.1)
            
            # Now try Ctrl+P
            h.ctrl('p')
            time.sleep(0.8)
            
            assert_true(h.popup_was_triggered(), "Popup should work after navigation")
    runner.run_test("ctrl_p_after_navigation", test_ctrl_p_after_navigation)
    
    def test_navigation_after_ctrl_p():
        """Test navigation still works after Ctrl+P popup closes."""
        with PickerTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            
            # We're at window 3
            assert_eq(h.get_current_window_index(), 3)
            
            # Trigger popup
            h.ctrl('p')
            time.sleep(0.8)
            
            # Navigation should still work
            h.ctrl('j')
            h.wait_for_window_index(2, timeout=2)
            assert_eq(h.get_current_window_index(), 2, "Navigation works after popup")
    runner.run_test("navigation_after_ctrl_p", test_navigation_after_ctrl_p)
    
    print()
    print("Edge Cases:")
    
    def test_ctrl_p_rapid_presses():
        """Test rapid Ctrl+P presses don't cause issues."""
        with PickerTestHarness() as h:
            # Rapidly press Ctrl+P - should handle gracefully
            for _ in range(5):
                h.ctrl('p')
                time.sleep(0.1)
            
            time.sleep(1)  # Let any popups close
            
            # Should still be functional
            assert_eq(h.get_window_count(), 1, "Still have window after rapid C-p")
    runner.run_test("ctrl_p_rapid_presses", test_ctrl_p_rapid_presses)
    
    def test_ctrl_p_with_single_window():
        """Test Ctrl+P works with only one window."""
        with PickerTestHarness() as h:
            assert_eq(h.get_window_count(), 1)
            
            h.ctrl('p')
            time.sleep(0.8)
            
            assert_true(h.popup_was_triggered(), "Popup works with single window")
            assert_eq(h.get_window_count(), 1, "Still have one window")
    runner.run_test("ctrl_p_single_window", test_ctrl_p_with_single_window)
    
    print()
    print("Window Selection Tests (with fzf):")
    
    def test_select_window_by_name():
        """Test selecting a specific window by typing its name."""
        with FzfPickerHarness() as h:
            h.new_window(name="alpha")
            h.new_window(name="beta")
            h.new_window(name="gamma")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Select "gamma" via picker
            h.open_picker_and_select("gamma")
            
            assert_eq(h.get_current_window_index(), 4, "Should be at gamma (window 4)")
    runner.run_test("select_window_by_name", test_select_window_by_name)
    
    def test_select_window_by_number():
        """Test selecting a window by typing its number."""
        with FzfPickerHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Select window 2 by number
            h.open_picker_and_select("2:")
            
            assert_eq(h.get_current_window_index(), 2, "Should be at window 2")
    runner.run_test("select_window_by_number", test_select_window_by_number)
    
    def test_select_different_windows():
        """Test selecting different windows in sequence."""
        with FzfPickerHarness() as h:
            h.new_window(name="first")
            h.new_window(name="second")
            h.new_window(name="third")
            
            # Start at window 4 (third)
            assert_eq(h.get_current_window_index(), 4)
            
            # Select "first"
            h.open_picker_and_select("first")
            assert_eq(h.get_current_window_index(), 2, "At 'first'")
            
            # Select "third"
            h.open_picker_and_select("third")
            assert_eq(h.get_current_window_index(), 4, "At 'third'")
            
            # Select "second"
            h.open_picker_and_select("second")
            assert_eq(h.get_current_window_index(), 3, "At 'second'")
    runner.run_test("select_different_windows", test_select_different_windows)
    
    def test_cancel_picker_with_escape():
        """Test that pressing Escape cancels the picker."""
        with FzfPickerHarness() as h:
            h.new_window(name="other")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Open picker but cancel
            h.ctrl('p')
            time.sleep(0.3)
            h.send_keys("Escape")
            time.sleep(0.3)
            
            # Should still be at window 1
            assert_eq(h.get_current_window_index(), 1, "Still at window 1 after cancel")
    runner.run_test("cancel_picker_with_escape", test_cancel_picker_with_escape)
    
    def test_picker_with_many_windows():
        """Test picker with many windows."""
        with FzfPickerHarness() as h:
            # Create 9 more windows (window2 through window10)
            for i in range(9):
                h.new_window(name=f"window{i+2}")
            
            assert_eq(h.get_window_count(), 10, "Have 10 windows")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Select window7 (which is at index 7 since window2 is at index 2)
            h.open_picker_and_select("window7")
            
            # window7 is the 7th window created, at index 7
            assert_eq(h.get_current_window_index(), 7, "At window7 (index 7)")
    runner.run_test("picker_with_many_windows", test_picker_with_many_windows)

    return runner.summary()


if __name__ == "__main__":
    import sys
    sys.exit(run_picker_tests())