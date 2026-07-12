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

from nethackmcp.map import DungeonMap, Tile
from nethackmcp.simple_odometry import SimpleOdometry

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
    """Handles NetHack game using persistent tmux sessions.
    
    State is persisted via state files across MCP invocations.
    NetHack save files are cleared to ensure fresh game starts.
    """

    TMUX_SESSION_PREFIX = "nethack_"
    STATE_FILE_PREFIX = "/tmp/nethack_state_"

    def __init__(self):
        self.session_name: str | None = None
        self.state_file: str | None = None
        self.current_goal: str = "Explore ?"
        self.goal_counter: int = 0
        self.goal_locked_until_counter: int = -1
        self.game_state: dict[str, Any] = {}
        
        # Mapping system
        self.maps: dict[int, DungeonMap] = {}
        self.current_level: int = 1
        
        # Previous screen state for diff
        self.previous_screen: str | None = None
        self.previous_stripped_screen: list[str] | None = None
        
        # Terrain chunk tracking for movement detection (deprecated, kept for backwards compatibility)
        self._player_chunks_4x4: list[list[str]] | None = None
        self._previous_player_screen_pos: tuple[int, int] | None = None
        self._previous_player_world_pos: tuple[int, int] | None = None
        
        # Visual odometry system for robust positioning
        self._simple_odom: SimpleOdometry | None = None
        
        # Restore state from existing state file (but NOT NetHack save files)
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
                    
                    # Restore maps
                    maps_data = state.get("maps", {})
                    for level_str, map_data in maps_data.items():
                        level = int(level_str)
                        self.maps[level] = DungeonMap.from_dict(map_data)
                    
                    self.current_level = state.get("current_level", 1)
                    self.previous_screen = state.get("previous_screen")
                    self.previous_stripped_screen = state.get("previous_stripped_screen", [])
                    self._player_chunks_4x4 = state.get("player_chunks_4x4")
                    if "previous_player_screen_pos" in state:
                        self._previous_player_screen_pos = tuple(state["previous_player_screen_pos"])
                    if "previous_player_world_pos" in state:
                        self._previous_player_world_pos = tuple(state["previous_player_world_pos"])
                    
                    # Restore visual odometry state if available
                    if "simple_odometry_state" in state:
                        try:
                            odom_state = state["simple_odometry_state"]
                            self._simple_odom = SimpleOdometry()
                            # Simple odom just needs to know initial player pos
                            # We'll set it when we get the first screen
                        except Exception:
                            pass
                    
                    self.game_state = state
            except Exception:
                pass

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
            max(len(line) for line in previous) if previous else 0
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
        if state_files:
            return state_files[0]
        return None
    
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

    def _extract_level_from_status(self, screen_lines: list[str]) -> int | None:
        """Extract current dungeon level from status bar."""
        for line in screen_lines:
            # Look for "Dlvl:" pattern
            match = re.search(r"Dlvl:\s*(\d+)", line)
            if match:
                return int(match.group(1))
        return None
    
    def _detect_level_change(self, screen_lines: list[str]) -> bool:
        """Check if player moved between dungeon levels."""
        current_level = self._extract_level_from_status(screen_lines)
        if current_level is not None and current_level != self.current_level:
            return True
        return False
    
    def _get_terrain_chunk(self, screen_lines: list[str], screen_row: int, screen_col: int, size: int = 4) -> list[list[str]]:
        """Extract a chunk of terrain from screen space (static terrain only).
        
        This extracts a 4x4 chunk at absolute screen position (screen_row, screen_col).
        The chunk contains only static terrain characters (|, -, +, ., #, <, >).
        Empty/unknown positions are filled with space ' '.
        """
        chunk = []
        half = size // 2
        for dr in range(-half, half):
            row = screen_row + dr
            if 0 <= row < len(screen_lines):
                line = screen_lines[row]
                row_chunk = []
                for dc in range(-half, half):
                    col = screen_col + dc
                    if 0 <= col < len(line):
                        char = line[col]
                        # Only include static terrain (not player @, creatures, or items)
                        if char in {'|', '-', '+', '.', '#', '<', '>'}:
                            row_chunk.append(char)
                        else:
                            row_chunk.append(' ')  # Empty/unknown for non-terrain
                    else:
                        row_chunk.append(' ')
            else:
                # Out of bounds - use empty chunk
                row_chunk = [' '] * size
            chunk.append(row_chunk)
        return chunk
    
    def _find_best_chunk_match(self, current_chunk: list[list[str]], prev_chunks_with_offsets: list[tuple[int, int, list[list[str]]]]) -> tuple[int, int] | None:
        """Find which previous chunk position matches current chunk best. Returns (screen_row, screen_col) of best match."""
        best_match = None
        best_score = -1
        size = len(current_chunk)
        
        for screen_row, screen_col, prev_chunk in prev_chunks_with_offsets:
            if len(prev_chunk) != size or len(prev_chunk[0]) != size:
                continue
            
            score = 0
            for r in range(size):
                for c in range(size):
                    if prev_chunk[r][c] == current_chunk[r][c] and prev_chunk[r][c] not in " @":
                        score += 1
            
            if score > best_score:
                best_score = score
                best_match = (screen_row, screen_col)
        
        return best_match
    
    def _update_map_from_screen(self, screen_lines: list[str], player_row: int, player_col: int) -> dict[str, Any]:
        """Update the map using dungeon offset tracking.
        
        Finds best dungeon offset by matching static terrain tiles,
        updates map, and calculates player world position from offset.
        
        The dungeon_offset represents where screen position (0,0) is in world coordinates.
        player_world_pos = dungeon_offset + player_screen_pos
        """
        result = {
            "player_world_pos": None,
            "map_updated": False,
            "player_moved_world": False,
            "screen_shifted": False,
        }
        
        # Get or create map for current level
        if self.current_level not in self.maps:
            self.maps[self.current_level] = DungeonMap(level=self.current_level)
        
        current_map = self.maps[self.current_level]
        
        # Extract terrain from screen
        game_start = 0
        game_end = 23
        
        # If map is empty, establish initial dungeon_offset from player screen position
        # player_world = dungeon_offset + player_screen, and player_world = (0,0) initially
        # So dungeon_offset = (0,0) - player_screen = -player_screen
        if not current_map.tiles:
            current_map.dungeon_offset = (-player_row, -player_col)
            current_map.player_world_pos = (0, 0)
            result["screen_shifted"] = True
            result["map_updated"] = True
        
        # If map already has tiles, find best dungeon offset by matching static terrain
        else:
            best_offset = current_map.find_best_dungeon_offset(screen_lines, search_range=20)
            current_map.dungeon_offset = best_offset
            result["screen_shifted"] = True
            result["map_updated"] = True
        
        # Calculate player world position from dungeon offset + screen position
        player_world_row = current_map.dungeon_offset[0] + player_row
        player_world_col = current_map.dungeon_offset[1] + player_col
        current_map.player_world_pos = (player_world_row, player_world_col)
        
        # Update map with terrain from current screen using dungeon offset
        for row in range(game_start, min(game_end + 1, len(screen_lines))):
            line = screen_lines[row]
            for col, char in enumerate(line):
                if char in DungeonMap.PASSABLE_TERRAIN or char in DungeonMap.IMPASSABLE_TERRAIN:
                    # Convert screen coords to world coords using dungeon offset
                    world_row = current_map.dungeon_offset[0] + row
                    world_col = current_map.dungeon_offset[1] + col
                    existing_tile = current_map.get_tile(world_row, world_col)
                    if existing_tile and existing_tile.visited:
                        current_map.mark_visited(world_row, world_col)
                    else:
                        current_map.set_tile(world_row, world_col, char, seen=True, visited=True)
        
        # Mark player position as visited
        current_map.mark_visited(player_world_row, player_world_col)
        
        # Mark adjacent spaces as visited
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if dr == 0 and dc == 0:
                    continue
                adj_world_row = player_world_row + dr
                adj_world_col = player_world_col + dc
                tile = current_map.get_tile(adj_world_row, adj_world_col)
                if tile and tile.terrain == ' ':
                    current_map.mark_visited(adj_world_row, adj_world_col)
        
        result["player_world_pos"] = [player_world_row, player_world_col]
        return result
    
    def _update_map_from_screen_odom(
        self,
        screen_lines: list[str],
        player_row: int,
        player_col: int,
        world_row: float,
        world_col: float
    ) -> dict[str, Any]:
        """
        Update map using odometry-derived world position instead of screen-based calculation.
        
        Args:
            screen_lines: Current screen state
            player_row, player_col: Player position on screen
            world_row, world_col: Position from visual odometry
        """
        result = {
            "player_world_pos": None,
            "map_updated": False,
            "player_moved_world": False,
            "screen_shifted": False,
        }
        
        # Get or create map for current level
        if self.current_level not in self.maps:
            self.maps[self.current_level] = DungeonMap(level=self.current_level)
        
        current_map = self.maps[self.current_level]
        
        # Extract terrain from screen
        game_start = 0
        game_end = 23
        
        # Use odometry-derived world position directly
        current_map.player_world_pos = (int(world_row), int(world_col))
        result["player_world_pos"] = [int(world_row), int(world_col)]
        result["player_moved_world"] = True
        result["map_updated"] = True
        
        # Update map with terrain from current screen using odometry position
        for row in range(game_start, min(game_end + 1, len(screen_lines))):
            line = screen_lines[row]
            for col, char in enumerate(line):
                if char in DungeonMap.PASSABLE_TERRAIN or char in DungeonMap.IMPASSABLE_TERRAIN:
                    # Convert screen coords to world coords using odometry position
                    # world_row = (row - player_row) + player_world_row
                    world_r = (row - player_row) + current_map.player_world_pos[0]
                    world_c = (col - player_col) + current_map.player_world_pos[1]
                    
                    existing_tile = current_map.get_tile(world_r, world_c)
                    if existing_tile and existing_tile.visited:
                        current_map.mark_visited(world_r, world_c)
                    else:
                        current_map.set_tile(world_r, world_c, char, seen=True)
        
        # Mark player position as visited
        current_map.mark_visited(current_map.player_world_pos[0], current_map.player_world_pos[1])
        
        # Mark adjacent spaces as visited
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if dr == 0 and dc == 0:
                    continue
                adj_world_row = current_map.player_world_pos[0] + dr
                adj_world_col = current_map.player_world_pos[1] + dc
                tile = current_map.get_tile(adj_world_row, adj_world_col)
                if tile and tile.terrain == ' ':
                    current_map.mark_visited(adj_world_row, adj_world_col)
        
        return result

    def start_game(self, character: str = "random") -> dict[str, Any]:
        """Start a new NetHack game."""
        session_name = self._get_session_name()
        state_file = self._get_state_file()

        # Clean up any existing session and ALL state files to ensure fresh start
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

        current_state = self.get_state()
        full_output = current_state.get("full_output", "")
        self.previous_screen = full_output
        self.previous_stripped_screen = full_output.split("\n")

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
                
                # Extended commands (starting with #) need Enter to execute
                if command.startswith("#"):
                    subprocess.run(
                        ["tmux", "send-keys", "-t", self.session_name, "Enter"],
                        check=True,
                        capture_output=True,
                    )

            # Wait for NetHack to process
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
                except Exception:
                    pass
            
            # Update state with diff if available and add it to the returned state
            if hasattr(self, 'previous_stripped_screen') and self.previous_stripped_screen is not None:
                current_stripped = state.get("full_output", "").split("\n")
                if len(current_stripped) > 0 and current_stripped[0] != "":
                    diff = self._create_diff(current_stripped, self.previous_stripped_screen)
                    state["diff"] = diff
            
            return state

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
            # Load previous screen from state file if available (only if not already set)
            if self.state_file and os.path.exists(self.state_file):
                try:
                    with open(self.state_file, "r") as f:
                        saved_state = json.load(f)
                    if not hasattr(self, 'previous_screen') or self.previous_screen is None:
                        self.previous_screen = saved_state.get("previous_screen")
                    if not hasattr(self, 'previous_stripped_screen') or self.previous_stripped_screen is None:
                        self.previous_stripped_screen = saved_state.get("previous_stripped_screen", [])
                except Exception:
                    pass

            # Get exactly what's visible on screen
            import time
            time.sleep(0.2)  # Wait for screen to update
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
            
            # Check for level change
            new_level = self._extract_level_from_status(screen_lines)
            if new_level is not None and new_level != self.current_level:
                self.current_level = new_level
            
            # Update map from screen (only if player was found)
            if player_row >= 0 and player_col >= 0:
                map_result = self._update_map_from_screen(screen_lines, player_row, player_col)
            else:
                map_result = {"error": "Player position not found on screen"}
            
            # Store previous screen for next diff
            self.previous_stripped_screen = screen_lines.copy()

            # Build state with optional hints - use colored text for screen, stripped for analysis
            state = {
                "status": "playing",
                "screen": colored_text,
                "full_output": colored_text,
                "session_name": self.session_name,
                "map": {
                    "level": self.current_level,
                    "player_world_pos": self.maps.get(self.current_level, DungeonMap(self.current_level)).player_world_pos,
                    "explored_tiles": self.maps.get(self.current_level, DungeonMap(self.current_level)).get_explored_count(),
                    "seen_tiles": self.maps.get(self.current_level, DungeonMap(self.current_level)).get_seen_count(),
                    "bounds": self.maps.get(self.current_level, DungeonMap(self.current_level)).bounds,
                    
                } if self.maps else {
                    "level": self.current_level,
                    "player_world_pos": None,
                    "explored_tiles": 0,
                    "seen_tiles": 0,
                    "bounds": {"min_row": 0, "max_row": 0, "min_col": 0, "max_col": 0},
                    "odometry_confidence": None,
                },
            }
            
            # Add diff if previous screen available
            if self.previous_stripped_screen is not None:
                diff = self._create_diff(screen_lines, self.previous_stripped_screen)
                state["diff"] = diff
            
            # Add goal at top
            if self.current_goal:
                state["screen"] = f"Goal: {self.current_goal}\n\n{colored_text}"
            
            # Add hints if player position found (use stripped text for tile detection)
            if player_row >= 0:
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
                    f"Level: {self.current_level}",
                    f"Player (world): {self.maps.get(self.current_level, DungeonMap(self.current_level)).player_world_pos}",
                    f"Downstairs: {self.maps.get(self.current_level, DungeonMap(self.current_level)).downstairs_pos}",
                    f"Explored: {self.maps.get(self.current_level, DungeonMap(self.current_level)).get_explored_count()} tiles",
                    f"Seen: {self.maps.get(self.current_level, DungeonMap(self.current_level)).get_seen_count()} tiles",
                    "",
                ]
                
                direction_tiles = self._get_direction_tiles(screen_lines, player_row, player_col)
                
                # Screen center is (0,0) in world coordinates
                screen_center_row = len(screen_lines) // 2
                screen_center_col = max(len(line) for line in screen_lines) // 2 if screen_lines else 40
                
                for key in "hjklynub":
                    tile = direction_tiles.get(key, " ")
                    
                    # Check if we have map info for this direction
                    map_info = ""
                    current_map = self.maps.get(self.current_level)
                    if current_map and current_map.player_world_pos:
                        # Get direction offset from player
                        row_offset, col_offset = self._get_direction_offset(key)
                        direction_row = player_row + row_offset
                        direction_col = player_col + col_offset
                        
                        # Convert screen position to world coordinates using dungeon_offset
                        world_row = current_map.dungeon_offset[0] + direction_row
                        world_col = current_map.dungeon_offset[1] + direction_col
                        tile_obj = current_map.get_tile(world_row, world_col)
                        if tile_obj:
                            visited_str = " (VISITED)" if tile_obj.visited else " (UNVISITED)"
                            map_info = visited_str
                            # Check if terrain is actually passable (for doors)
                            is_passable = current_map._is_terrain_passable(world_row, world_col)
                            if tile in ['|', '-'] and is_passable:
                                tile = '+'  # Show as door instead of wall
                    hints_lines.append(f"`{key}`:'{tile}'{map_info}")
                
                blocked_tiles = {' ', '<'}
                allowed_keys = []
                for key, tile in direction_tiles.items():
                    # Check actual passability via map
                    current_map = self.maps.get(self.current_level)
                    if current_map and current_map.player_world_pos:
                        row_offset, col_offset = self._get_direction_offset(key)
                        direction_row = player_row + row_offset
                        direction_col = player_col + col_offset
                        # Convert screen coordinates to world coordinates using dungeon_offset
                        world_row = current_map.dungeon_offset[0] + direction_row
                        world_col = current_map.dungeon_offset[1] + direction_col
                        is_passable = current_map._is_terrain_passable(world_row, world_col)
                        # Debug: print(f"DIR {key}: tile='{tile}' world=({world_row},{world_col}) passable={is_passable}")
                        if is_passable:
                            allowed_keys.append(key)
                    elif tile not in blocked_tiles and tile != '-':
                        allowed_keys.append(key)
                
                hints_lines.extend([
                    "",
                    f"SUGGESTIONS: {' '.join(allowed_keys)}",
                ])

                if self.current_goal:
                    hints_lines.append("")
                    hints_lines.append(f"Current goal: {self.current_goal}")
                
                state["hints"] = "\n".join(hints_lines)
                state["direction_tiles"] = direction_tiles
                
                # Add map after hints with big header
                if player_row >= 0:
                    map_result = self._show_map()
                    if map_result.get("status") == "success":
                        map_header = "\n\n###### MIND MAP ######\n"
                        state["hints"] = state["hints"] + map_header + map_result.get("map", "")

            # Update state file
            if self.state_file:
                try:
                    # Save maps to state
                    maps_data = {
                        str(level): map_obj.to_dict()
                        for level, map_obj in self.maps.items()
                    }
                    
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
                    state["player_chunks_4x4"] = self._player_chunks_4x4
                    if self._previous_player_screen_pos:
                        state["previous_player_screen_pos"] = list(self._previous_player_screen_pos)
                    
                    # Save visual odometry state
                    if self._simple_odom is not None:
                        state["simple_odometry_state"] = {
                            "row": self._simple_odom.state.row,
                            "col": self._simple_odom.state.col,
                            "heading": self._simple_odom.state.heading,
                            "keyframe_counter": self._simple_odom._keyframe_counter
                        }
                    
                    with open(self.state_file, "w") as f:
                        json.dump(state, f)
                except Exception:
                    pass

            self.game_state = state
            return state

        except subprocess.CalledProcessError as e:
            return {"error": f"Failed to get state: {e}", "status": "error"}
    
    def _get_direction_offset(self, key: str) -> tuple[int, int]:
        """Get row/col offset for a direction key."""
        offsets = {
            "h": (0, -1),
            "j": (1, 0),
            "k": (-1, 0),
            "l": (0, 1),
            "y": (-1, -1),
            "u": (-1, 1),
            "b": (1, -1),
            "n": (1, 1),
        }
        return offsets.get(key, (0, 0))
    
    def _show_map(self) -> dict[str, Any]:
        """Generate a string representation of the current map."""
        if self.current_level not in self.maps:
            return {"error": f"No map data for level {self.current_level}", "status": "error"}
        
        current_map = self.maps[self.current_level]
        map_string = current_map.get_map_string()
        
        result = {
            "map": map_string,
            "level": self.current_level,
            "bounds": current_map.bounds,
            "explored_tiles": current_map.get_explored_count(),
            "seen_tiles": current_map.get_seen_count(),
            "player_world_pos": current_map.player_world_pos,
            "downstairs_pos": current_map.downstairs_pos,
            "status": "success",
        }
        
        return result
    


    def stop_game(self) -> dict[str, Any]:
        """Stop the current game."""
        if self.session_name is None:
            return {"status": "stopped", "message": "No game running"}
        
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, ":quit"],
                check=True,
                capture_output=True,
            )
            time.sleep(1)
            
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, "#quit"],
                check=True,
                capture_output=True,
            )
            time.sleep(1)
            
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, "Enter"],
                check=True,
                capture_output=True,
            )
            time.sleep(1)
            
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

        # Clean up any existing state files from previous runs
        state_files = self._find_all_state_files()
        for sf in state_files:
            try:
                if os.path.exists(sf):
                    os.remove(sf)
            except Exception:
                pass
        
        # Clear NetHack's default save directory to prevent auto-restore
        home = os.path.expanduser("~")
        nethack_dirs = [
            os.path.join(home, ".nethack"),  # Default location
            os.path.join(home, ".local", "share", "nethack"),  # XDG base directory
            "/var/lib/nethack",  # System-wide
        ]
        for nethack_dir in nethack_dirs:
            try:
                if os.path.isdir(nethack_dir):
                    import shutil
                    shutil.rmtree(nethack_dir)
            except Exception:
                pass

        self.session_name = None
        self.state_file = None
        self.current_goal = "Explore"
        self.goal_counter = 0
        self.goal_locked_until_counter = -1
        self.current_level = 1
        self.maps = {}
        self._player_chunks_4x4 = None
        self._previous_player_screen_pos = None
        self._previous_player_world_pos = None
        self._simple_odom = None
        self.previous_screen = None
        self.previous_stripped_screen = None
        self.game_state = {}

        return {"status": "stopped", "message": "Game stopped successfully"}


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
            diff = result.get("diff", "")
            if screen_output:
                print(f"=== SCREEN OUTPUT ===\n{screen_output}\n=== END SCREEN ===", file=sys.stderr, flush=True)
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
                except Exception as e:
                    import traceback
                    print(f"[ERROR saving goal]: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            else:
                print("[WARNING] No state_file available for persisting goal", file=sys.stderr, flush=True)
            print(f"[AI CALL RESULT] {name}: Goal set to '{goal}'", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\nGoal set: {goal}\n```")]

        elif name == "nethack_diff":
            state = await asyncio.to_thread(client.get_state)
            diff = state.get("diff", "No diff available")
            print(f"[AI CALL RESULT] {name}: {diff}", file=sys.stderr, flush=True)
            return [types.TextContent(type="text", text=f"```\n{diff}\n```")]

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
