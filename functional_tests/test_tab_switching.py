#!/usr/bin/env python3
"""Tests specifically for tab switching bugs in clauthing.

These tests focus on the Ctrl+J, Ctrl+K, and Alt+O keybindings.
"""

import subprocess
import time
from pathlib import Path
from harness import TmuxTestHarness, TestRunner, assert_eq, assert_true


def run_tab_switching_tests():
    """Comprehensive tests for tab switching."""
    runner = TestRunner()
    
    print("Tab Switching Bug Investigation Tests")
    print("=" * 50)
    print()
    
    # Test: Verify keybindings are actually set
    print("Keybinding Verification:")
    
    def test_keybindings_exist():
        """Verify the keybindings are set in tmux."""
        with TmuxTestHarness() as h:
            result = subprocess.run(
                ["tmux", "-L", h.socket_name, "list-keys"],
                capture_output=True,
                text=True
            )
            bindings = result.stdout
            
            # Check for our keybindings
            assert_true("C-j" in bindings, "C-j binding exists")
            assert_true("C-k" in bindings, "C-k binding exists")
            assert_true("M-o" in bindings, "M-o binding exists")
            
            # Verify they're bound to the right commands
            assert_true("previous-window" in bindings, "previous-window command exists")
            assert_true("next-window" in bindings, "next-window command exists")
            assert_true("last-window" in bindings, "last-window command exists")
    runner.run_test("keybindings_exist", test_keybindings_exist)
    
    def test_keybinding_detail():
        """Show exactly what the keybindings are."""
        with TmuxTestHarness() as h:
            result = subprocess.run(
                ["tmux", "-L", h.socket_name, "list-keys", "-T", "root"],
                capture_output=True,
                text=True
            )
            print()
            print("  Root keybindings:")
            for line in result.stdout.split('\n'):
                if 'C-j' in line or 'C-k' in line or 'M-o' in line:
                    print(f"    {line}")
    runner.run_test("keybinding_detail", test_keybinding_detail)
    
    print()
    print("Sequential Navigation Tests:")
    
    def test_navigate_through_all_windows():
        """Create 5 windows and navigate through all of them."""
        with TmuxTestHarness() as h:
            # Create windows
            for i in range(4):
                h.new_window(name=f"win{i+2}")
            
            windows = h.get_windows()
            print()
            print(f"  Created {len(windows)} windows: {[w.name for w in windows]}")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Navigate forward through all windows
            visited = [1]
            for i in range(5):  # Press Ctrl+K 5 times
                h.ctrl('k')
                time.sleep(0.15)
                current = h.get_current_window_index()
                visited.append(current)
            
            print(f"  Forward navigation (Ctrl+K): {visited}")
            
            # Verify we visited all windows in order
            expected = [1, 2, 3, 4, 5, 1]  # Should wrap around
            assert_eq(visited, expected, "Forward navigation path")
    runner.run_test("navigate_through_all_windows_forward", test_navigate_through_all_windows)
    
    def test_navigate_backwards():
        """Navigate backwards through windows."""
        with TmuxTestHarness() as h:
            for i in range(4):
                h.new_window(name=f"win{i+2}")
            
            # Start at window 5
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "5"],
                check=True
            )
            h.wait_for_window_index(5)
            
            # Navigate backward
            visited = [5]
            for i in range(5):
                h.ctrl('j')
                time.sleep(0.15)
                current = h.get_current_window_index()
                visited.append(current)
            
            print()
            print(f"  Backward navigation (Ctrl+J): {visited}")
            
            expected = [5, 4, 3, 2, 1, 5]  # Should wrap around
            assert_eq(visited, expected, "Backward navigation path")
    runner.run_test("navigate_backwards", test_navigate_backwards)
    
    print()
    print("Last Window Toggle Tests:")
    
    def test_last_window_basic():
        """Test Alt+O basic toggle."""
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            
            # We're at 3
            assert_eq(h.get_current_window_index(), 3, "Start at 3")
            
            # Go to 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Toggle sequence
            toggles = [1]
            for _ in range(4):
                h.alt('o')
                time.sleep(0.15)
                toggles.append(h.get_current_window_index())
            
            print()
            print(f"  Alt+O toggles: {toggles}")
            
            # Should toggle between 1 and 3
            expected = [1, 3, 1, 3, 1]
            assert_eq(toggles, expected, "Toggle sequence")
    runner.run_test("last_window_toggle", test_last_window_basic)
    
    def test_last_window_after_navigation():
        """Test that last-window tracks correctly after navigation."""
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            h.new_window(name="win3")
            
            # Start at 3, go to 2
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "2"],
                check=True
            )
            h.wait_for_window_index(2)
            
            # Now "last" should be 3
            h.alt('o')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 3, "Alt+O from 2 goes to 3")
            
            # Navigate to 1 via Ctrl+J
            h.ctrl('j')
            h.ctrl('j')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 1, "At window 1")
            
            # Now Alt+O should go to 2 (where we came from via navigation)
            # Note: This depends on whether navigation updates "last"
            h.alt('o')
            time.sleep(0.15)
            current = h.get_current_window_index()
            print()
            print(f"  After 3->2 (select) -> 3 (alt-o) -> 1 (nav): Alt+O goes to {current}")
    runner.run_test("last_window_after_navigation", test_last_window_after_navigation)
    
    print()
    print("Edge Cases:")
    
    def test_single_window_navigation():
        """Navigation with only one window."""
        with TmuxTestHarness() as h:
            # Only one window
            assert_eq(h.get_window_count(), 1, "Only one window")
            
            h.ctrl('k')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 1, "Still at 1 after Ctrl+K")
            
            h.ctrl('j')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 1, "Still at 1 after Ctrl+J")
    runner.run_test("single_window_navigation", test_single_window_navigation)
    
    def test_two_window_navigation():
        """Navigation with exactly two windows."""
        with TmuxTestHarness() as h:
            h.new_window(name="win2")
            
            # At window 2
            assert_eq(h.get_current_window_index(), 2, "At window 2")
            
            # Ctrl+K and Ctrl+J should both just toggle
            h.ctrl('k')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 1, "Ctrl+K to 1")
            
            h.ctrl('k')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 2, "Ctrl+K to 2")
            
            h.ctrl('j')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 1, "Ctrl+J to 1")
            
            h.ctrl('j')
            time.sleep(0.15)
            assert_eq(h.get_current_window_index(), 2, "Ctrl+J to 2")
    runner.run_test("two_window_navigation", test_two_window_navigation)
    
    def test_rapid_key_presses():
        """Test rapid key presses don't get dropped."""
        with TmuxTestHarness() as h:
            for i in range(9):
                h.new_window(name=f"win{i+2}")
            
            # Go to window 1
            subprocess.run(
                ["tmux", "-L", h.socket_name, "select-window", "-t", "1"],
                check=True
            )
            h.wait_for_window_index(1)
            
            # Rapidly press Ctrl+K 5 times with minimal delay
            for _ in range(5):
                h.ctrl('k')
            
            time.sleep(0.3)  # Wait for processing
            
            current = h.get_current_window_index()
            print()
            print(f"  After 5 rapid Ctrl+K from window 1: at window {current}")
            
            # Should be at window 6
            assert_eq(current, 6, "Rapid navigation")
    runner.run_test("rapid_key_presses", test_rapid_key_presses)
    
    return runner.summary()


if __name__ == "__main__":
    import sys
    sys.exit(run_tab_switching_tests())