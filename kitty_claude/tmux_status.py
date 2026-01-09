#!/usr/bin/env python3
"""Tmux status bar window display."""
import subprocess
import sys

def get_window_display(line_num, socket="kitty-claude"):
    """Get formatted window list for the specified line.
    
    Args:
        line_num: 1 or 2 - which line to display
        socket: Tmux socket name
    """
    try:
        # Get terminal width
        result = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{client_width}"],
            capture_output=True,
            text=True
        )
        width = int(result.stdout.strip())
        
        # Get current window
        result = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True
        )
        current = result.stdout.strip()
        
        # Get all windows
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F", "#{window_index}:#{window_name}"],
            capture_output=True,
            text=True
        )
        windows = result.stdout.strip().split('\n')
        
        # Format windows and split across two lines
        line1 = ""
        line2 = ""
        line_width = 0
        current_line = 1
        
        for window in windows:
            if not window:
                continue
                
            idx, name = window.split(':', 1)
            
            # Format with styling
            if idx == current:
                formatted = f"#[bg=colour39,fg=colour235,bold] {idx}:{name} #[default]"
            else:
                formatted = f"#[bg=colour235,fg=colour248] {idx}:{name} #[default]"
            
            # Calculate visible width (approximate - ignoring tmux format codes)
            visible_width = len(idx) + len(name) + 4
            
            # Check if it fits on current line
            if line_width + visible_width < width:
                if current_line == 1:
                    line1 += formatted
                else:
                    line2 += formatted
                line_width += visible_width
            else:
                # Move to line 2
                current_line = 2
                line2 += formatted
                line_width = visible_width
        
        # Return requested line
        if line_num == 1:
            print(line1)
        else:
            print(line2)
    
    except Exception as e:
        # Silent failure - don't break tmux status bar
        print("")

def handle_tmux_status(line_num, profile=None):
    """Handle --tmux-status command."""
    if profile:
        socket = f"kitty-claude-{profile}"
    else:
        socket = "kitty-claude"
    
    get_window_display(line_num, socket)