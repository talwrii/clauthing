#!/usr/bin/env python3
"""
E2E test for the MCP proxy with tmux popup approval.

Launches clauthing --one-tab --no-kitty, adds a proxied test MCP server
via :mcp-approve, reloads, asks Claude to call the tool, approves the popup,
and verifies the response.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Need pexpect for PTY (tmux requires it)
try:
    import pexpect
except ImportError:
    print("SKIP: pexpect not installed")
    sys.exit(0)


# ── Helpers ──────────────────────────────────────────────────────────────────

def find_socket(start_time):
    """Scan for a cl1-* tmux socket created after start_time."""
    uid = os.getuid()
    tmpdir = os.environ.get('TMUX_TMPDIR', '/tmp')
    socket_dir = Path(tmpdir) / f"tmux-{uid}"

    if not socket_dir.exists():
        return None

    for sock in socket_dir.iterdir():
        if sock.name.startswith("cl1-") and "-test-" not in sock.name:
            try:
                ctime = sock.stat().st_ctime
                if ctime >= start_time - 1:
                    return sock.name
            except:
                pass
    return None


def session_ready(socket):
    """Check if tmux session is ready."""
    result = subprocess.run(
        ["tmux", "-L", socket, "has-session"],
        capture_output=True
    )
    return result.returncode == 0


def capture_pane(socket):
    """Capture current pane content."""
    result = subprocess.run(
        ["tmux", "-L", socket, "capture-pane", "-p", "-S", "-200"],
        capture_output=True, text=True
    )
    return result.stdout


def send_keys(socket, keys, enter=True):
    """Send keys to the tmux pane."""
    cmd = ["tmux", "-L", socket, "send-keys", keys]
    if enter:
        cmd.append("Enter")
    subprocess.run(cmd, check=True)


def wait_for_text(socket, text, timeout=30):
    """Wait for text to appear in pane."""
    start = time.time()
    while time.time() - start < timeout:
        content = capture_pane(socket)
        if text in content:
            return content
        time.sleep(0.5)
    return None


def wait_for_prompt(socket, timeout=30):
    """Wait for claude to show a prompt (> character)."""
    start = time.time()
    while time.time() - start < timeout:
        content = capture_pane(socket)
        # Claude shows ">" when ready for input
        if ">" in content:
            time.sleep(1)
            return True
        time.sleep(0.5)
    return False


def wait_for_new_content(socket, old_content, timeout=60):
    """Wait for pane content to change from old_content, then stabilize.

    Returns the new content, or None on timeout.
    """
    start = time.time()
    # Phase 1: wait for content to change
    while time.time() - start < timeout:
        content = capture_pane(socket)
        if content != old_content:
            break
        time.sleep(0.5)
    else:
        return None

    # Phase 2: wait for content to stabilize (no change for 3 seconds)
    last_content = content
    stable_start = time.time()
    while time.time() - start < timeout:
        content = capture_pane(socket)
        if content != last_content:
            last_content = content
            stable_start = time.time()
        elif time.time() - stable_start >= 3:
            return content
        time.sleep(0.5)
    return last_content  # return whatever we have


def kill_server(socket):
    """Kill the tmux server."""
    subprocess.run(
        ["tmux", "-L", socket, "kill-server"],
        capture_output=True
    )


# ── Test MCP server ─────────────────────────────────────────────────────────

TEST_MCP_SERVER = r'''#!/usr/bin/env python3
"""Tiny MCP server with one tool for testing."""
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

async def run():
    server = Server("test-server")

    @server.list_tools()
    async def list_tools():
        return [Tool(
            name="say_hello",
            description="Says hello to someone. Always use this when asked to say hello.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Name to greet"}},
                "required": ["name"],
            },
        )]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "say_hello":
            return [TextContent(type="text", text=f"PROXY_TEST_OK: Hello, {arguments.get('name', 'world')}!")]
        return [TextContent(type="text", text=f"Unknown: {name}")]

    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(run())
'''


# ── Main test ────────────────────────────────────────────────────────────────

def main():
    # Write test MCP server
    test_server_path = Path("/tmp/test-mcp-server.py")
    test_server_path.write_text(TEST_MCP_SERVER)
    test_server_path.chmod(0o755)
    print(f"[setup] Wrote test MCP server to {test_server_path}")

    socket = None
    process = None

    try:
        # Launch clauthing
        start_time = time.time()
        cmd = "clauthing --one-tab --no-kitty"
        print(f"[setup] Starting: {cmd}")
        process = pexpect.spawn(cmd, encoding='utf-8', timeout=30)

        # Wait for socket
        print("[setup] Waiting for tmux socket...")
        for _ in range(30):
            socket = find_socket(start_time)
            if socket and session_ready(socket):
                break
            time.sleep(0.5)
        else:
            print("[FAIL] Timed out waiting for tmux socket")
            return 1

        print(f"[setup] Socket: {socket}")

        # Handle trust prompt if it appears
        print("[setup] Checking for trust prompt...")
        time.sleep(2)
        content = capture_pane(socket)
        if "trust" in content.lower() or "Yes, proceed" in content:
            print("[setup] Trust prompt detected, accepting...")
            send_keys(socket, "", enter=True)
            time.sleep(2)

        # Wait for claude to be ready
        print("[setup] Waiting for claude prompt...")
        if not wait_for_prompt(socket, timeout=30):
            # Maybe another trust prompt or onboarding
            content = capture_pane(socket)
            if "trust" in content.lower() or "Yes, proceed" in content:
                print("[setup] Another trust prompt, accepting...")
                send_keys(socket, "", enter=True)
                time.sleep(2)
                if not wait_for_prompt(socket, timeout=30):
                    print("[FAIL] Claude did not show prompt after trust")
                    print(f"  Pane content:\n{capture_pane(socket)}")
                    return 1
            else:
                print("[FAIL] Claude did not show prompt")
                print(f"  Pane content:\n{content}")
                return 1
        print("[setup] Claude ready")

        # ── Step 0: Send "hi" and wait for actual Claude response ──
        # Count how many "> " prompt lines exist before sending
        before = capture_pane(socket)
        prompt_count_before = before.count('\n> ')
        print(f"[test] Sending 'hi' (prompts before: {prompt_count_before})...")
        send_keys(socket, "hi")

        # Wait for a NEW prompt to appear (meaning Claude responded)
        # After "hi" is processed, there will be response text then a new "> " line
        print("[test] Waiting for Claude to respond (new prompt after response)...")
        start = time.time()
        while time.time() - start < 90:
            content = capture_pane(socket)
            prompt_count = content.count('\n> ')
            # We need MORE prompts than before — the new empty prompt after response
            if prompt_count > prompt_count_before:
                # Verify it's a real new prompt (not our input line)
                lines = content.split('\n')
                # Find prompt lines that are just "> " or "> " followed by nothing useful
                empty_prompts = [l for l in lines if l.strip() == '>' or l.strip() == '> ']
                if empty_prompts:
                    print(f"[test] Claude responded (prompts now: {prompt_count})")
                    break
            time.sleep(1)
        else:
            print("[WARN] Timed out waiting for response, checking pane...")
            content = capture_pane(socket)
            print(f"[debug] Pane (last 400):\n{content[-400:]}")

        # Extra settle time
        time.sleep(2)
        print(f"[debug] After 'hi' (last 300):\n{capture_pane(socket)[-300:]}")

        # ── Step 1: :mcp-approve ──
        print("[test] Sending :mcp-approve...")
        send_keys(socket, f":mcp-approve {test_server_path}")

        # Wait for the hook response text — not just any content change
        print("[test] Waiting for hook response text...")
        result = wait_for_text(socket, "Operation stopped by hook", timeout=45)
        if not result:
            content = capture_pane(socket)
            if "MCP server" in content:
                print("[test] :mcp-approve succeeded (found MCP server text)")
            else:
                print("[FAIL] :mcp-approve hook did not respond in time")
                print(f"  Pane (last 500):\n{content[-500:]}")
                return 1
        else:
            print("[test] :mcp-approve hook responded")

        # Now wait for the new prompt to appear AFTER the hook response
        print("[test] Waiting for new prompt...")
        time.sleep(2)
        # The hook response should have shown, now there should be a fresh ">" prompt
        if not wait_for_prompt(socket, timeout=10):
            print("[WARN] No prompt after hook, continuing...")

        # ── Step 2: :reload ──
        print("[test] Sending :reload...")
        send_keys(socket, ":reload")

        # :reload respawn-pane kills and restarts — pane content resets completely
        time.sleep(3)
        # Check for trust prompt repeatedly
        for _ in range(15):
            content = capture_pane(socket)
            if "trust" in content.lower() or "Yes, proceed" in content:
                print("[test] Trust prompt after reload, accepting...")
                send_keys(socket, "", enter=True)
                time.sleep(2)
                break
            if ">" in content and "Welcome" in content:
                break  # Claude loaded without trust prompt
            time.sleep(1)

        print("[test] Waiting for claude to restart...")
        if not wait_for_prompt(socket, timeout=45):
            print("[FAIL] Claude did not restart after :reload")
            print(f"  Pane content:\n{capture_pane(socket)}")
            return 1

        # Wait for MCP servers to connect
        time.sleep(8)
        print("[test] Claude reloaded")
        print(f"[debug] After reload (last 300):\n{capture_pane(socket)[-300:]}")

        # ── Step 3: Trigger tool call ──
        print("[test] Asking Claude to use say_hello tool...")
        before = capture_pane(socket)
        send_keys(socket, "Use the say_hello tool with name 'test'. Just call the tool, nothing else.")

        # Wait for popup to appear (the proxy will show it)
        # The popup blocks the confirm_popup() call, so we just wait and send Enter
        print("[test] Waiting for popup...")
        time.sleep(5)

        # Approve the popup
        print("[test] Sending Enter to approve popup...")
        send_keys(socket, "", enter=True)
        # Also try sending to any popup that might be active
        time.sleep(1)
        send_keys(socket, "", enter=True)

        # Wait for the result
        print("[test] Waiting for response...")
        result = wait_for_text(socket, "PROXY_TEST_OK", timeout=30)
        if result:
            print("[PASS] Found 'PROXY_TEST_OK' in response!")
            return 0
        else:
            # Check if it was denied
            content = capture_pane(socket)
            if "User denied" in content:
                print("[FAIL] Tool call was denied (popup not approved in time)")
            else:
                print("[FAIL] Did not find 'PROXY_TEST_OK' in response")
            print(f"  Pane content:\n{content}")
            return 1

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        if socket:
            kill_server(socket)
        if process:
            try:
                process.terminate(force=True)
            except:
                pass
        print("[cleanup] Done")


if __name__ == "__main__":
    sys.exit(main())
