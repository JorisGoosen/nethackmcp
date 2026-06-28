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
        self.current_goal: str = "Explore"
        self.goal_counter: int = 0
        self.goal_locked_until_counter: int = -1
        self.game_state: dict[str, Any] = {}
        
        # Check for existing state from previous run
        existing_state = self._find_existing_state()
        if existing_state:
            try:
                with open(existing_state, "r") as f:
                    state = json.load(f)
                    self.session_name = state.get("session_name")
                    self.state_file = existing_state
                    self.current_goal = state.get("current_goal", "")
                    self.goal_counter = state.get("goal_counter", 0)
                    self.goal_locked_until_counter = state.get("goal_locked_until_counter", -1)
                    self.game_state = state
            except Exception:
                pass

    def _strip_ansi(self, text: str) -> tuple[str, str]:
        """Strip ANSI escape codes from text. Returns (stripped_text, colored_text)."""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        stripped = ansi_escape.sub("", text)
        return stripped, text

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
            'export NETHACKOPTIONS="color"; while true; do nethack; sleep 1; done',
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
            # Handle special key names that tmux understands
            special_keys = {
                "Enter": "Enter",
                "Escape": "Escape",
                "Space": "Space",
                "Tab": "Tab",
                "Backspace": "Backspace",
                "Ctrl+C": "Ctrl-C",
                "Ctrl+D": "Ctrl-D",
                "Ctrl+X": "Ctrl-X",
                "Ctrl+Z": "Ctrl-Z",
                "Ctrl+P": "Ctrl-P",
                "Ctrl+N": "Ctrl-N",
                "Ctrl+L": "Ctrl-L",
            }
            
            # Check if command is a special key
            if command in special_keys:
                tmux_key = special_keys[command]
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.session_name, tmux_key],
                    check=True,
                    capture_output=True,
                )
            else:
                # Send each character as a separate keypress
                for char in command:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", self.session_name, char],
                        check=True,
                        capture_output=True,
                    )

            # Wait for NetHack to process
            time.sleep(0.5)

            return self.get_state()

        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to send command: {e}", "status": "error"}

    def _get_direction_tiles(self, screen_lines: list[str], player_row: int, player_col: int) -> dict[str, str]:
        """Get the tile character in each direction from the player."""
        directions = {
            "h": (0, -1),   # left
            "j": (1, 0),    # down
            "k": (-1, 0),   # up
            "l": (0, 1),    # right
            "y": (-1, -1),  # up-left
            "u": (-1, 1),   # up-right
            "b": (1, -1),   # down-left
            "n": (1, 1),    # down-right
        }
        
        result = {}
        for key, (dr, dc) in directions.items():
            r, c = player_row + dr, player_col + dc
            if 0 <= r < len(screen_lines) and 0 <= c < len(screen_lines[r]):
                result[key] = screen_lines[r][c]
            else:
                result[key] = " "
        
        return result

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
            # Get exactly what's visible on screen
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", self.session_name, "-p", "-J"],
                check=True,
                capture_output=True,
                text=True,
            )

            stripped_text, colored_text = self._strip_ansi(result.stdout)
            
            # Find player position (the @ symbol) in stripped text
            screen_lines = stripped_text.split("\n")
            player_row, player_col = -1, -1
            for r, line in enumerate(screen_lines):
                for c, char in enumerate(line):
                    if char == "@":
                        player_row, player_col = r, c
                        break
                if player_row >= 0:
                    break

            # Build state with optional hints - use colored text for screen, stripped for analysis
            state = {
                "status": "playing",
                "screen": colored_text,
                "full_output": colored_text,
                "session_name": self.session_name,
            }
            
            # Add goal at top
            if self.current_goal:
                state["screen"] = f"Goal: {self.current_goal}\nCounter: {self.goal_counter}/10\n\n{colored_text}"
            
            # Add hints if player position found (use stripped text for tile detection)
            if player_row >= 0:
                direction_tiles = self._get_direction_tiles(screen_lines, player_row, player_col)
                hints_lines = [
                    "HINTS",
                    "@ Has the following next tiles behind the movement keys:",
                    "",
                    "Legend:",
                    "'@' => you",
                    "'d' => could be your dog, if so dont worry about it blocking you, you'll swap places on movement, this is OK",
                    "'>' => stairs down (passable and where you want to go!)",
                    "'#' => cavefloor (passable)",
                    "'.' => tilefloor (passable)",
                    "'+' => door (passable)",
                    "'|' => wall (impassable)",
                    "'-' => wall (impassable)",
                    "' ' => rockwall (impassable, YES THE EMPTY SPACE IS IMPASSABLE!)",
                    "'<' => stairs up (impassable)",
                    "",
                ]
                for key in "hjklynub":
                    tile = direction_tiles.get(key, " ")
                    hints_lines.append(f"`{key}`:'{tile}'")
                
                blocked_tiles = {' ', '|', '-'}
                allowed_keys = [key for key, tile in direction_tiles.items() if tile not in blocked_tiles]
                
                hints_lines.extend([
                    "",
                    f"SUGGESTIONS: {' '.join(allowed_keys)}",
                ])

                if self.current_goal:
                    hints_lines.append("")
                    hints_lines.append(f"Current goal: {self.current_goal}")
                    hints_lines.append(f"Counter: {self.goal_counter}/10")
                
                state["hints"] = "\n".join(hints_lines)
                state["direction_tiles"] = direction_tiles

            # Update state file
            if self.state_file:
                try:
                    state["state_file"] = self.state_file
                    state["current_goal"] = self.current_goal
                    state["goal_counter"] = self.goal_counter
                    state["goal_locked_until_counter"] = self.goal_locked_until_counter
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
        self.current_goal = "Explore"
        self.goal_counter = 0
        self.goal_locked_until_counter = -1
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
        types.Tool(
            name="nethack_set_goal",
            description="Set the current goal for the AI player",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The goal to set (e.g., 'Find stairs down', 'Eat food', 'Kill monster')",
                    }
                },
                "required": ["goal"],
            },
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
            return [types.TextContent(type="text", text=f"```\n{text_output}\n```")]

        elif name == "nethack_move":
            command = arguments.get("command")
            if not command:
                error_result = {"error": "Command is required"}
                print(f"[AI CALL RESULT] {name}: ERROR - {json.dumps(error_result)}", file=sys.stderr, flush=True)
                return [
                    types.TextContent(
                        type="text", text=f"```\nERROR: {json.dumps(error_result, indent=2)}\n```"
                    )
                ]
            result = await asyncio.to_thread(client.send_command, command)
            
            if isinstance(result, dict) and result.get("status") == "playing":
                client.goal_counter += 1
                if client.state_file:
                    try:
                        with open(client.state_file, "r") as f:
                            state = json.load(f)
                        state["goal_counter"] = client.goal_counter
                        with open(client.state_file, "w") as f:
                            json.dump(state, f)
                    except Exception:
                        pass
            
            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            screen_output = result.get("screen", "")
            hints = result.get("hints", "")
            if screen_output:
                print(f"=== SCREEN OUTPUT ===\n{screen_output}\n=== END SCREEN ===", file=sys.stderr, flush=True)
            output = screen_output if screen_output else json.dumps(result, indent=2)
            if hints:
                output = output + "\n" + hints
            return [types.TextContent(type="text", text=f"```\n{output}\n```")]

        elif name == "nethack_state":
            state = await asyncio.to_thread(client.get_state)
            print(f"[AI CALL RESULT] {name}: {json.dumps(state)}", file=sys.stderr, flush=True)
            screen_output = state.get("screen", "")
            hints = state.get("hints", "")
            output = screen_output if screen_output else json.dumps(state, indent=2)
            if hints:
                output = output + "\n" + hints
            return [types.TextContent(type="text", text=f"```\n{output}\n```")]

        elif name == "nethack_stop":
            result = await asyncio.to_thread(client.stop_game)
            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\n{result}\n```")]

        elif name == "nethack_set_goal":
            goal = arguments.get("goal", "")
            
            if "stair" in goal.lower():
                print(f"[AI CALL RESULT] {name}: ERROR - Goal contains 'stair'", file=sys.stderr, flush=True)
                return [types.TextContent(type="text", text="```\nChoose lower level objectives and stick to them!\n```")]
            
            if client.goal_locked_until_counter > client.goal_counter:
                remaining = client.goal_locked_until_counter - client.goal_counter
                print(f"[AI CALL RESULT] {name}: ERROR - Goal is locked for {remaining} more moves", file=sys.stderr, flush=True)
                return [types.TextContent(type="text", text=f"```\nGoal is locked. Wait {remaining} more moves before changing.\n```")]
            
            client.current_goal = goal
            client.goal_counter = 0
            client.goal_locked_until_counter = 10
            
            if client.state_file:
                try:
                    with open(client.state_file, "w") as f:
                        json.dump({
                            "session_name": client.session_name,
                            "current_goal": goal,
                            "goal_counter": 0,
                            "goal_locked_until_counter": 10,
                            "status": "playing"
                        }, f)
                    print(f"[DEBUG] Saved goal '{goal}' to {client.state_file}", file=sys.stderr, flush=True)
                except Exception as e:
                    import traceback
                    print(f"[ERROR saving goal]: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            else:
                print("[WARNING] No state_file available for persisting goal", file=sys.stderr, flush=True)
            print(f"[AI CALL RESULT] {name}: Goal set to '{goal}'", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\nGoal set: {goal}\n```")]

        else:
            error_result = {"error": f"Unknown tool: {name}"}
            print(f"[AI CALL RESULT] {name}: ERROR - {json.dumps(error_result)}", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\nError: {json.dumps(error_result, indent=2)}\n```")]

    except Exception as e:
        import traceback

        error_result = {"error": str(e), "traceback": traceback.format_exc()}
        print(f"[AI CALL RESULT] {name}: EXCEPTION - {json.dumps(error_result)}", file=sys.stderr, flush=True)
        return [
            types.TextContent(
                type="text",
                text=f"```\nException: {json.dumps(error_result, indent=2)}\n```",
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
