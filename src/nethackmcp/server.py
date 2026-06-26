"""MCP server for playing NetHack."""

from __future__ import annotations

import asyncio
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

SERVER_INSTRUCTIONS = """\
You are connected to NetHack through this MCP server.

## NetHack Commands (short):

- **Direction**: h(←) j(↓) k(↑) l(→) y(↖) u(↗) b(↙) n(↘)
- **Move one step**: [direction]
- **Move far**: [DIR] (uppercase)
- **Move silent**: m+[direction]
- **Pick up**: ,
- **Drop**: d
- **Fight**: f+[dir]
- **Look**: :
- **Search**: s
- **Wait/Rest**: .
- **Inventory**: i
- **Equipment**: *
- **Status**: ^X (Ctrl+x)
- **Extended commands**: #command
- **Help**: ?

## Workflow:

1. Start a game with nethack_start
2. Use nethack_move to send commands
3. Check screen state with nethack_state to see what's happening
4. Stop with nethack_stop when done

## Important:

The server maintains game state using tmux sessions. Each game gets its own tmux session.
"""


class NetHackClient:
    """Handles NetHack game using persistent tmux sessions."""

    TMUX_SESSION_PREFIX = "nethack_"
    STATE_FILE_PREFIX = "/tmp/nethack_state_"

    def __init__(self):
        self.session_name: str | None = None
        self.state_file: str | None = None
        self.game_state: dict[str, Any] = {}
        
        # Check for existing state from previous run
        existing_state = self._find_existing_state()
        if existing_state:
            try:
                with open(existing_state, "r") as f:
                    state = json.load(f)
                    self.session_name = state.get("session_name")
                    self.state_file = existing_state
                    self.game_state = state
            except Exception:
                pass

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape codes from text."""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)

    def _find_existing_state(self) -> str | None:
        """Find existing state file from previous game."""
        import glob

        state_files = glob.glob(f"{self.STATE_FILE_PREFIX}*.json")
        if state_files:
            return state_files[0]
        return None

    def _get_session_name(self) -> str:
        """Get or create a tmux session name."""
        if self.session_name is None:
            state_file = self._find_existing_state()
            if state_file:
                try:
                    with open(state_file, "r") as f:
                        state = json.load(f)
                        self.session_name = state.get("session_name")
                        self.state_file = state_file
                        return self.session_name
                except Exception:
                    pass

            import random
            import string

            suffix = "".join(random.choices(string.ascii_lowercase, k=8))
            self.session_name = f"{self.TMUX_SESSION_PREFIX}{suffix}"
            self.state_file = None
        return self.session_name

    def _get_state_file(self) -> str:
        """Get or create a state file path."""
        if self.state_file is None:
            state_file = self._find_existing_state()
            if state_file:
                self.state_file = state_file
                return state_file

            import random
            import string

            suffix = "".join(random.choices(string.ascii_lowercase, k=8))
            self.state_file = f"{self.STATE_FILE_PREFIX}{suffix}.json"
        return self.state_file

    def start_game(self, character: str = "random") -> dict[str, Any]:
        """Start a new NetHack game."""
        session_name = self._get_session_name()
        state_file = self._get_state_file()

        # Clean up any existing session
        self.stop_game()

        # Create tmux session with NetHack running in a loop
        # NetHack exits when stdin closes, so we wrap it to keep it running
        cmd = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x", "80",
            "-y", "24",
            "bash",
            "-c",
            "while true; do nethack; sleep 1; done",
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to start tmux session: {e}", "status": "error"}

        # Wait a moment for NetHack to start
        time.sleep(2)

        # Verify tmux session was created
        check_result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
        )
        if check_result.returncode != 0:
            return {
                "error": f"tmux session '{session_name}' was not created. Is NetHack installed and in PATH?",
                "status": "error"
            }

        # Write initial state
        state = {
            "status": "started",
            "screen": "",
            "full_output": "",
            "message": "Game started with random character. Waiting for character selection...",
            "session_name": session_name,
            "state_file": state_file,
        }

        try:
            with open(state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            self.stop_game()
            return {"error": f"Failed to write state file: {e}", "status": "error"}

        self.session_name = session_name
        self.state_file = state_file
        self.game_state = state

        # Read and return screen content
        time.sleep(0.5)
        state = self.get_state()
        # Update status to started after getting initial screen
        state["status"] = "started"
        state["message"] = "Game started with random character. Waiting for character selection..."
        return state

    def send_command(self, command: str) -> dict[str, Any]:
        """Send a command to NetHack."""
        if self.session_name is None:
            return {"error": "Game not started. Call start_game first.", "status": "error"}

        try:
            # Send command followed by enter
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, command, "Enter"],
                check=True,
                capture_output=True,
            )

            # Wait for NetHack to process
            time.sleep(0.5)

            return self.get_state()

        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to send command: {e}", "status": "error"}

    def get_state(self) -> dict[str, Any]:
        """Get current game state from tmux."""
        if self.session_name is None:
            return {"error": "Game not started. Call start_game first.", "status": "error"}

        # Check if tmux session exists
        check_result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
            text=True,
        )
        if check_result.returncode != 0:
            return {"error": f"tmux session '{self.session_name}' not found", "status": "error"}

        try:
            # Get tmux buffer content
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", self.session_name, "-p", "-S", "-100"],
                check=True,
                capture_output=True,
                text=True,
            )

            screen_text = self._strip_ansi(result.stdout)

            state = {
                "status": "playing",
                "screen": screen_text,
                "full_output": screen_text,
                "session_name": self.session_name,
            }

            # Update state file
            if self.state_file:
                try:
                    state["state_file"] = self.state_file
                    with open(self.state_file, "w") as f:
                        json.dump(state, f)
                except Exception:
                    pass

            self.game_state = state
            return state

        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to get state: {e}", "status": "error"}

    def stop_game(self) -> dict[str, Any]:
        """Stop the current game."""
        try:
            if self.session_name:
                subprocess.run(
                    ["tmux", "kill-session", "-t", self.session_name],
                    check=True,
                    capture_output=True,
                )
        except subprocess.CalledProcessError:
            pass

        try:
            if self.state_file and os.path.exists(self.state_file):
                os.remove(self.state_file)
        except Exception:
            pass

        self.session_name = None
        self.state_file = None
        self.game_state = {}

        return {"status": "stopped"}


server = Server("nethack-mcp")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available NetHack tools."""
    return [
        types.Tool(
            name="nethack_start",
            description="Start a new NetHack game",
            inputSchema={
                "type": "object",
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "Character name (optional, defaults to random)",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="nethack_move",
            description="Send a move/command to NetHack",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to send (movement keys, actions, or extended commands)",
                    }
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="nethack_state",
            description="Get current game state",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="nethack_stop",
            description="Stop current NetHack game",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    """Call a NetHack tool."""
    print(f"[AI CALL] Tool: {name}, Arguments: {json.dumps(arguments)}", file=sys.stderr, flush=True)
    
    client = NetHackClient()

    try:
        if name == "nethack_start":
            character = arguments.get("character", "human")
            result = await asyncio.to_thread(client.start_game, character)
            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            screen_output = result.get("screen", result.get("message", ""))
            if screen_output:
                print(f"=== SCREEN OUTPUT ===\n{screen_output}\n=== END SCREEN ===", file=sys.stderr, flush=True)
            # Return just the screen output for opencode to display
            text_output = screen_output if screen_output else json.dumps(result, indent=2)
            return [types.TextContent(type="text", text=f"=== STARTED ===\n{text_output}")]

        elif name == "nethack_move":
            command = arguments.get("command")
            if not command:
                error_result = {"error": "Command is required"}
                print(f"[AI CALL RESULT] {name}: ERROR - {json.dumps(error_result)}", file=sys.stderr, flush=True)
                return [
                    types.TextContent(
                        type="text", text=f"ERROR: {json.dumps(error_result, indent=2)}"
                    )
                ]
            result = await asyncio.to_thread(client.send_command, command)
            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            screen_output = result.get("screen", "")
            if screen_output:
                print(f"=== SCREEN OUTPUT ===\n{screen_output}\n=== END SCREEN ===", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=screen_output if screen_output else json.dumps(result, indent=2))]

        elif name == "nethack_state":
            state = await asyncio.to_thread(client.get_state)
            print(f"[AI CALL RESULT] {name}: {json.dumps(state)}", file=sys.stderr, flush=True)
            screen_output = state.get("screen", "")
            if screen_output:
                print(f"=== SCREEN OUTPUT ===\n{screen_output}\n=== END SCREEN ===", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=screen_output if screen_output else json.dumps(state, indent=2))]

        elif name == "nethack_stop":
            result = await asyncio.to_thread(client.stop_game)
            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"Stopped: {json.dumps(result, indent=2)}")]

        else:
            error_result = {"error": f"Unknown tool: {name}"}
            print(f"[AI CALL RESULT] {name}: ERROR - {json.dumps(error_result)}", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"Error: {json.dumps(error_result, indent=2)}")]

    except Exception as e:
        import traceback

        error_result = {"error": str(e), "traceback": traceback.format_exc()}
        print(f"[AI CALL RESULT] {name}: EXCEPTION - {json.dumps(error_result)}", file=sys.stderr, flush=True)
        return [
            types.TextContent(
                type="text",
                text=f"Exception: {json.dumps(error_result, indent=2)}",
            )
        ]


async def run() -> None:
    """Start the MCP server on stdio."""
    caps = server.get_capabilities(
        notification_options=NotificationOptions(),
        experimental_capabilities={},
    )
    caps.tools = types.ToolsCapability(listChanged=True)

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="nethack-mcp",
                server_version="0.1.0",
                capabilities=caps,
                instructions=SERVER_INSTRUCTIONS,
            ),
        )


def main() -> None:
    """CLI entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
