"""MCP server for playing NetHack."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from nethackmcp.map import DungeonMap

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

## Tile legend:
- `@` you
- `.` floor | `#` cavefloor | `+` door (all passable)
- `>` stairs down (passable)
- `| -` walls | ` ` rockwall | `<` stairs up (all impassable)
- `X` visited rockwall | `?` unexplored adjacent

## Important:
The state hints show what is in each direction from `@` with visited status.
Only move into passable directions listed under "Move:".

The server maintains game state using tmux sessions. Each game gets its own tmux session.
"""

_DIRECTION_OFFSETS: dict[str, tuple[int, int]] = {
    "h": (0, -1),
    "j": (1, 0),
    "k": (-1, 0),
    "l": (0, 1),
    "y": (-1, -1),
    "u": (-1, 1),
    "b": (1, -1),
    "n": (1, 1),
}

_SPECIAL_KEYS: dict[str, str] = {
    "Enter": "Enter",
    "Escape": "Escape",
    "Space": "Space",
    "Tab": "Tab",
    "Backspace": "Backspace",
    "Ctrl+C": "C-c",
    "Ctrl+D": "C-d",
    "Ctrl+X": "C-x",
    "Ctrl+Z": "C-z",
    "Ctrl+P": "C-p",
    "Ctrl+N": "C-n",
    "Ctrl+L": "C-l",
}


def _log_error(context: str, exc: Exception) -> None:
    """Log an error to stderr without crashing."""
    print(f"[ERROR] {context}: {exc}", file=sys.stderr, flush=True)


class NetHackClient:
    """Handles NetHack game using persistent tmux sessions."""

    TMUX_SESSION_PREFIX = "nethack_"
    STATE_FILE_PREFIX = "/tmp/nethack_state_"

    def __init__(self):
        self.session_name: str | None = None
        self.state_file: str | None = None
        self.current_goal: str = "Explore questionmarks"
        self.goal_counter: int = 0
        self.goal_locked_until_counter: int = -1
        self.game_state: dict[str, Any] = {}

        self.maps: dict[int, DungeonMap] = {}
        self.current_level: int = 1

        self.previous_screen: str | None = None
        self.previous_stripped_screen: list[str] | None = None

        self._restore_state()

    def _restore_state(self) -> None:
        """Restore game state from a persisted state file."""
        existing_state = self._find_existing_state()
        if not existing_state:
            return

        try:
            with open(existing_state, "r") as f:
                state = json.load(f)
        except Exception as exc:
            _log_error("_restore_state: reading state file", exc)
            return

        try:
            self.session_name = state.get("session_name")
            self.state_file = existing_state
            self.current_goal = state.get("current_goal", "")
            self.goal_counter = state.get("goal_counter", 0)
            self.goal_locked_until_counter = state.get("goal_locked_until_counter", -1)

            maps_data = state.get("maps", {})
            for level_str, map_data in maps_data.items():
                level = int(level_str)
                self.maps[level] = DungeonMap.from_dict(map_data)

            self.current_level = state.get("current_level", 1)
            self.previous_screen = state.get("previous_screen")
            self.previous_stripped_screen = state.get("previous_stripped_screen", [])
            self.game_state = state
        except Exception as exc:
            _log_error("_restore_state: parsing state", exc)

    def _strip_ansi(self, text: str) -> tuple[str, str]:
        """Strip ANSI escape codes from text. Returns (stripped_text, colored_text)."""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        stripped = ansi_escape.sub("", text)
        return stripped, text

    def _create_diff(self, current: list[str], previous: list[str]) -> str:
        """Create character-by-character diff showing changes."""
        max_lines = max(len(current), len(previous))
        max_width = max(
            max(len(line) for line in current) if current else 0,
            max(len(line) for line in previous) if previous else 0,
        )

        diff_lines = []
        for row in range(max_lines):
            current_line = current[row] if row < len(current) else ""
            previous_line = previous[row] if row < len(previous) else ""

            diff_row = []
            has_changes = False
            for col in range(max_width):
                current_char = current_line[col] if col < len(current_line) else " "
                previous_char = previous_line[col] if col < len(previous_line) else " "

                if current_char != previous_char:
                    diff_row.append(current_char)
                    has_changes = True
                else:
                    diff_row.append(" ")

            diff_line = "".join(diff_row).rstrip()
            if has_changes and diff_line:
                diff_lines.append(diff_line)

        return "\n".join(diff_lines)

    def _find_existing_state(self) -> str | None:
        """Find existing state file from previous game."""
        import glob

        state_files = glob.glob(f"{self.STATE_FILE_PREFIX}*.json")
        return state_files[0] if state_files else None

    def _find_all_state_files(self) -> list[str]:
        """Find all state files from previous games."""
        import glob

        return glob.glob(f"{self.STATE_FILE_PREFIX}*.json")

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
                except Exception as exc:
                    _log_error("_get_session_name: reading state file", exc)

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

    def _extract_level_from_status(self, screen_lines: list[str]) -> int | None:
        """Extract current dungeon level from status bar."""
        for line in screen_lines:
            match = re.search(r"Dlvl:\s*(\d+)", line)
            if match:
                return int(match.group(1))
        return None

    # ------------------------------------------------------------------ #
    #  Map / positioning
    # ------------------------------------------------------------------ #

    def _find_player(self, screen_lines: list[str]) -> tuple[int, int] | None:
        """Locate the @ symbol on screen."""
        for r, line in enumerate(screen_lines):
            for c, char in enumerate(line):
                if char == "@":
                    return (r, c)
        return None

    def _update_map_from_screen(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> dict[str, Any]:
        """Update the dungeon map using terrain matching for offset detection."""
        result = {
            "player_world_pos": None,
            "map_updated": False,
            "player_moved_world": False,
            "screen_shifted": False,
        }

        if self.current_level not in self.maps:
            self.maps[self.current_level] = DungeonMap(level=self.current_level)

        current_map = self.maps[self.current_level]
        TERRAIN_CHARS = {"#", ".", "+", "-", "|", "<", ">"}
        game_start = 2
        game_end = len(screen_lines) - 3

        if not current_map.tiles:
            current_map.dungeon_offset = (-player_row, -player_col)
            current_map.player_world_pos = (0, 0)
            result["screen_shifted"] = True
            result["map_updated"] = True
        else:
            if current_map.dungeon_offset == (0, 0) and current_map.player_world_pos:
                current_map.dungeon_offset = (
                    current_map.player_world_pos[0] - player_row,
                    current_map.player_world_pos[1] - player_col,
                )

            old_offset = current_map.dungeon_offset
            current_map.dungeon_offset = current_map.find_best_dungeon_offset(
                screen_lines, search_range=20
            )
            if current_map.dungeon_offset != old_offset:
                result["screen_shifted"] = True
            result["map_updated"] = True

        player_world_row = current_map.dungeon_offset[0] + player_row
        player_world_col = current_map.dungeon_offset[1] + player_col
        current_map.player_world_pos = (player_world_row, player_world_col)

        for row in range(game_start, min(game_end + 1, len(screen_lines))):
            line = screen_lines[row]
            for col, char in enumerate(line):
                if char not in TERRAIN_CHARS:
                    continue
                world_row = current_map.dungeon_offset[0] + row
                world_col = current_map.dungeon_offset[1] + col
                current_map.set_tile(world_row, world_col, char, seen=True)

        current_map.mark_visited(player_world_row, player_world_col)

        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                adj_row = player_row + dr
                adj_col = player_col + dc
                if 0 <= adj_row < len(screen_lines):
                    line = screen_lines[adj_row]
                    if 0 <= adj_col < len(line) and line[adj_col] == " ":
                        world_row = current_map.dungeon_offset[0] + adj_row
                        world_col = current_map.dungeon_offset[1] + adj_col
                        existing = current_map.get_tile(world_row, world_col)
                        if not existing or (existing.terrain == " " and not existing.visited):
                            current_map.set_tile(world_row, world_col, " ", seen=True, visited=True)

        result["player_world_pos"] = [player_world_row, player_world_col]
        return result

    # ------------------------------------------------------------------ #
    #  Game lifecycle
    # ------------------------------------------------------------------ #

    def start_game(self, character: str = "random") -> dict[str, Any]:
        """Start a new NetHack game."""
        session_name = self._get_session_name()
        state_file = self._get_state_file()

        self.stop_game()

        cmd = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "80",
            "-y",
            "24",
            "bash",
            "-c",
            'export NETHACKOPTIONS="color"; while true; do nethack; sleep 1; done',
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to start tmux session: {e}", "status": "error"}

        time.sleep(2)

        check_result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
        )
        if check_result.returncode != 0:
            return {
                "error": f"tmux session '{session_name}' was not created. Is NetHack installed and in PATH?",
                "status": "error",
            }

        state = {
            "status": "started",
            "screen": "",
            "full_output": "",
            "message": "Game started. Waiting for character selection...",
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

        time.sleep(0.5)
        state = self.get_state()
        state["status"] = "started"
        state["message"] = "Game started. Waiting for character selection..."
        return state

    def send_command(self, command: str) -> dict[str, Any]:
        """Send a command to NetHack."""
        if self.session_name is None:
            return {"error": "Game not started. Call start_game first.", "status": "error"}

        current_state = self.get_state()
        full_output = current_state.get("full_output", "")
        self.previous_screen = full_output
        self.previous_stripped_screen = full_output.split("\n")

        try:
            if command in _SPECIAL_KEYS:
                tmux_key = _SPECIAL_KEYS[command]
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.session_name, tmux_key],
                    check=True,
                    capture_output=True,
                )
            else:
                for char in command:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", self.session_name, char],
                        check=True,
                        capture_output=True,
                    )
                if command.startswith("#"):
                    subprocess.run(
                        ["tmux", "send-keys", "-t", self.session_name, "Enter"],
                        check=True,
                        capture_output=True,
                    )

            time.sleep(1.0)

            state = self.get_state()

            if self.state_file:
                try:
                    with open(self.state_file, "r") as f:
                        saved_state = json.load(f)
                    saved_state["previous_screen"] = self.previous_screen
                    saved_state["previous_stripped_screen"] = self.previous_stripped_screen
                    with open(self.state_file, "w") as f:
                        json.dump(saved_state, f)
                except Exception as exc:
                    _log_error("send_command: saving previous screen", exc)

            if self.previous_stripped_screen is not None:
                current_stripped = state.get("full_output", "").split("\n")
                if len(current_stripped) > 0 and current_stripped[0] != "":
                    diff = self._create_diff(current_stripped, self.previous_stripped_screen)
                    state["diff"] = diff

            return state

        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to send command: {e}", "status": "error"}

    def stop_game(self) -> dict[str, Any]:
        """Stop the current game."""
        if self.session_name is None:
            return {"status": "stopped", "message": "No game running"}

        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, "#quit"],
                check=False,
                capture_output=True,
            )
            time.sleep(0.5)
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, "Enter"],
                check=False,
                capture_output=True,
            )
            time.sleep(0.5)
        except Exception:
            pass

        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass

        try:
            if self.state_file and os.path.exists(self.state_file):
                os.remove(self.state_file)
        except Exception as exc:
            _log_error("stop_game: removing state file", exc)

        for sf in self._find_all_state_files():
            try:
                if os.path.exists(sf):
                    os.remove(sf)
            except Exception as exc:
                _log_error("stop_game: removing old state file", exc)

        self.session_name = None
        self.state_file = None
        self.current_goal = "Explore questionmarks"
        self.goal_counter = 0
        self.goal_locked_until_counter = -1
        self.current_level = 1
        self.maps = {}
        self.previous_screen = None
        self.previous_stripped_screen = None
        self.game_state = {}

        return {"status": "stopped", "message": "Game stopped successfully"}

    # ------------------------------------------------------------------ #
    #  State / screen
    # ------------------------------------------------------------------ #

    def _capture_screen(self) -> tuple[str, list[str]]:
        """Capture the current tmux pane and return (raw_text, stripped_lines)."""
        if self.session_name is None:
            raise RuntimeError("Game not started")

        check_result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
            text=True,
        )
        if check_result.returncode != 0:
            raise RuntimeError(f"tmux session '{self.session_name}' not found")

        time.sleep(0.2)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.session_name, "-p", "-J"],
            check=True,
            capture_output=True,
            text=True,
        )

        stripped_text, _ = self._strip_ansi(result.stdout)
        screen_lines = stripped_text.split("\n")
        return result.stdout, screen_lines

    def _build_hints(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> tuple[str, dict[str, str]]:
        """Build directional hints and allowed-move list."""
        direction_tiles: dict[str, str] = {}
        for key, (dr, dc) in _DIRECTION_OFFSETS.items():
            r, c = player_row + dr, player_col + dc
            if 0 <= r < len(screen_lines) and 0 <= c < len(screen_lines[r]):
                direction_tiles[key] = screen_lines[r][c]
            else:
                direction_tiles[key] = " "

        current_map = self.maps.get(self.current_level)

        hints_lines = ["HINTS", ""]
        for key in "hjklynub":
            tile = direction_tiles.get(key, " ")
            map_info = ""

            if current_map and current_map.player_world_pos:
                row_offset, col_offset = _DIRECTION_OFFSETS.get(key, (0, 0))
                direction_row = player_row + row_offset
                direction_col = player_col + col_offset
                world_row = current_map.dungeon_offset[0] + direction_row
                world_col = current_map.dungeon_offset[1] + direction_col
                tile_obj = current_map.get_tile(world_row, world_col)
                if tile_obj:
                    visited_str = " (VISITED)" if tile_obj.visited else " (UNVISITED)"
                    map_info = visited_str
                    if tile in ("|", "-") and current_map._is_terrain_passable(
                        world_row, world_col
                    ):
                        tile = "+"
            hints_lines.append(f"`{key}`:{tile}{map_info}")

        
        if self.current_goal:
            hints_lines.append(f"Goal: {self.current_goal}")

        return "\n".join(hints_lines), direction_tiles

    def _persist_state(self, state: dict[str, Any]) -> None:
        """Write full state to the state file for later restoration."""
        if not self.state_file:
            return

        maps_data = {str(level): map_obj.to_dict() for level, map_obj in self.maps.items()}

        state["state_file"] = self.state_file
        state["current_goal"] = self.current_goal
        state["goal_counter"] = self.goal_counter
        state["goal_locked_until_counter"] = self.goal_locked_until_counter
        state["maps"] = maps_data
        state["current_level"] = self.current_level

        current_map = self.maps.get(self.current_level)
        if current_map and current_map.player_world_pos:
            state["player_world_pos"] = list(current_map.player_world_pos)

        state["previous_screen"] = self.previous_screen
        state["previous_stripped_screen"] = self.previous_stripped_screen

        try:
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception as exc:
            _log_error("_persist_state", exc)

    def get_state(self) -> dict[str, Any]:
        """Get current game state from tmux."""
        if self.session_name is None:
            return {"error": "Game not started. Call start_game first.", "status": "error"}

        try:
            raw_text, screen_lines = self._capture_screen()
        except RuntimeError as e:
            return {"error": str(e), "status": "error"}
        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to capture screen: {e}", "status": "error"}

        player_pos = self._find_player(screen_lines)

        new_level = self._extract_level_from_status(screen_lines)
        if new_level is not None and new_level != self.current_level:
            self.current_level = new_level

        if player_pos is not None and "--More--" not in raw_text:
            player_row, player_col = player_pos
            self._update_map_from_screen(screen_lines, player_row, player_col)

        self.previous_stripped_screen = screen_lines.copy()

        current_map = self.maps.get(self.current_level)
        state: dict[str, Any] = {
            "status": "playing",
            "screen": raw_text,
            "full_output": raw_text,
            "session_name": self.session_name,
            "map": {
                "level": self.current_level,
                "player_world_pos": current_map.player_world_pos if current_map else None,
                "explored_tiles": current_map.get_explored_count() if current_map else 0,
                "seen_tiles": current_map.get_seen_count() if current_map else 0,
                "bounds": current_map.bounds
                if current_map
                else {"min_row": 0, "max_row": 0, "min_col": 0, "max_col": 0},
            },
        }

        if self.previous_stripped_screen is not None:
            state["diff"] = self._create_diff(screen_lines, self.previous_stripped_screen)

        if self.current_goal:
            state["screen"] = f"Goal: {self.current_goal}\n\n{raw_text}"

        if player_pos is not None:
            player_row, player_col = player_pos
            hints_text, direction_tiles = self._build_hints(screen_lines, player_row, player_col)
            state["hints"] = hints_text
            state["direction_tiles"] = direction_tiles

            if current_map:
                map_result = self._show_map()
                if map_result.get("status") == "success":
                    state["hints"] = hints_text + "\n\n###### MAP ######\n" + map_result.get("map", "")

        self._persist_state(state)
        self.game_state = state
        return state

    def _show_map(self) -> dict[str, Any]:
        """Generate a string representation of the current map."""
        if self.current_level not in self.maps:
            return {"error": f"No map data for level {self.current_level}", "status": "error"}

        current_map = self.maps[self.current_level]
        map_string = current_map.get_map_string()

        return {
            "map": map_string,
            "level": self.current_level,
            "bounds": current_map.bounds,
            "explored_tiles": current_map.get_explored_count(),
            "seen_tiles": current_map.get_seen_count(),
            "player_world_pos": current_map.player_world_pos,
            "downstairs_pos": current_map.downstairs_pos,
            "status": "success",
        }


# ------------------------------------------------------------------ #
#  MCP server
# ------------------------------------------------------------------ #

server = Server("nethack-mcp")
_fullscreen = False


def _show_fullscreen(screen: str, hints: str) -> None:
    """Clear terminal and print screen + map."""
    map_start = hints.find("###### MAP ######")
    if map_start != -1:
        map_part = hints[map_start + len("###### MAP ######"):].strip()
        hints_part = hints[:map_start].strip()
    else:
        map_part = hints[hints.rfind("\n"):].strip() if hints else ""
        hints_part = hints

    sys.stdout.write("\033[H\033[J")
    sys.stdout.write(screen)
    if hints_part:
        sys.stdout.write(f"\n{hints_part}\n")
    if map_part:
        sys.stdout.write(f"\n{map_part}\n")
    sys.stdout.flush()

_fullscreenclear: asyncio.Event | None = None


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
        types.Tool(
            name="nethack_diff",
            description="Get the difference between current and previous screen",
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
            text_output = screen_output if screen_output else json.dumps(result, indent=2)
            return [types.TextContent(type="text", text=f"```\n{text_output}\n```")]

        elif name == "nethack_move":
            command = arguments.get("command")
            if not command:
                return [
                    types.TextContent(
                        type="text", text="```\nERROR: Command is required\n```"
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
                    except Exception as exc:
                        _log_error("nethack_move: saving goal counter", exc)

            print(f"[AI CALL RESULT] {name}: {json.dumps(result)}", file=sys.stderr, flush=True)
            screen_output = result.get("screen", "")
            hints = result.get("hints", "")
            diff = result.get("diff", "")

            if _fullscreen:
                _show_fullscreen(screen_output, hints)

            output = screen_output if screen_output else json.dumps(result, indent=2)
            if hints:
                output = output + "\n" + hints
            if diff:
                output = output + f"\n\n=== DIFF ===\n{diff}"
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
                return [
                    types.TextContent(
                        type="text",
                        text="```\nChoose lower level objectives and stick to them!\n```",
                    )
                ]

            if client.goal_locked_until_counter > client.goal_counter:
                remaining = client.goal_locked_until_counter - client.goal_counter
                return [
                    types.TextContent(
                        type="text",
                        text=f"```\nGoal is locked. Wait {remaining} more moves before changing.\n```",
                    )
                ]

            client.current_goal = goal
            client.goal_counter = 0
            client.goal_locked_until_counter = 10

            if client.state_file:
                try:
                    existing: dict[str, Any] = {}
                    if os.path.exists(client.state_file):
                        with open(client.state_file, "r") as f:
                            existing = json.load(f)
                    existing["session_name"] = client.session_name
                    existing["current_goal"] = goal
                    existing["goal_counter"] = 0
                    existing["goal_locked_until_counter"] = 10
                    existing["status"] = "playing"
                    with open(client.state_file, "w") as f:
                        json.dump(existing, f)
                except Exception as exc:
                    _log_error("nethack_set_goal: saving state", exc)

            print(f"[AI CALL RESULT] {name}: Goal set to '{goal}'", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\nGoal set: {goal}\n```")]

        elif name == "nethack_diff":
            state = await asyncio.to_thread(client.get_state)
            diff = state.get("diff", "No diff available")
            return [types.TextContent(type="text", text=f"```\n{diff}\n```")]

        else:
            return [
                types.TextContent(
                    type="text",
                    text=f"```\nError: Unknown tool: {name}\n```",
                )
            ]

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


async def run_sse(port: int = 8000, host: str = "0.0.0.0", fullscreen: bool = False) -> None:
    """Start the MCP server over SSE/HTTP so you can watch the game."""
    from starlette.responses import PlainTextResponse

    import uvicorn

    global _fullscreen
    _fullscreen = fullscreen

    sse = mcp.server.sse.SseServerTransport("/messages/")

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
            caps = server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            )
            caps.tools = types.ToolsCapability(listChanged=True)
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

    async def app(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/sse" and scope["method"] == "GET":
                await handle_sse(scope, receive, send)
                return
            elif path.startswith("/messages/") and scope["method"] == "POST":
                await sse.handle_post_message(scope, receive, send)
                return
        response = PlainTextResponse("Not found", status_code=404)
        await response(scope, receive, send)

    print(f"NetHack MCP server listening on http://{host}:{port}/sse", flush=True)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_uv = uvicorn.Server(config)
    await server_uv.serve()


def main() -> None:
    """CLI entry point — defaults to stdio transport."""
    import argparse

    parser = argparse.ArgumentParser(description="NetHack MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for SSE mode (default: 8000)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for SSE mode (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Watch mode: print screen + map to terminal",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        print(f"Starting NetHack MCP server in SSE mode on {args.host}:{args.port}", flush=True)
        asyncio.run(run_sse(port=args.port, host=args.host, fullscreen=args.fullscreen))
    else:
        asyncio.run(run())


if __name__ == "__main__":
    main()