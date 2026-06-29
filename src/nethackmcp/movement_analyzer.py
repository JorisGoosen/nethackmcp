"""Movement analyzer for detecting player moves vs screen shifts."""

from __future__ import annotations

from typing import Any


class MovementAnalyzer:
    """Analyzes screen changes to determine what moved."""
    
    def __init__(self):
        self._previous_player_pos: tuple[int, int] | None = None
    
    def _find_player(self, screen_lines: list[str]) -> tuple[int, int] | None:
        """Find player @ position in screen lines."""
        for row, line in enumerate(screen_lines):
            col = line.find('@')
            if col >= 0:
                return (row, col)
        return None
    
    def _get_diff_changes(self, current: list[str], previous: list[str]) -> list[dict[str, Any]]:
        """Get list of changed characters between screens."""
        max_lines = max(len(current), len(previous))
        max_width = max(
            max(len(line) for line in current) if current else 0,
            max(len(line) for line in previous) if previous else 0
        )
        
        changes = []
        for row in range(max_lines):
            current_line = current[row] if row < len(current) else ""
            previous_line = previous[row] if row < len(previous) else ""
            
            for col in range(max_width):
                curr_char = current_line[col] if col < len(current_line) else " "
                prev_char = previous_line[col] if col < len(previous_line) else " "
                
                if curr_char != prev_char:
                    changes.append({
                        "row": row,
                        "col": col,
                        "from": prev_char,
                        "to": curr_char,
                    })
        
        return changes
    
    def _detect_screen_shift(self, changes: list[dict[str, Any]], 
                            prev_player_pos: tuple[int, int] | None,
                            curr_player_pos: tuple[int, int] | None) -> bool:
        """Detect if the screen (dungeon) shifted."""
        if not changes:
            return False
        
        # If player position changed, screen didn't shift (player moved)
        if prev_player_pos and curr_player_pos:
            if prev_player_pos != curr_player_pos:
                return False
        
        # Screen shift pattern: widespread changes across many rows
        rows_with_changes = set(c["row"] for c in changes)
        cols_with_changes = set(c["col"] for c in changes)
        
        change_density = len(changes) / (24 * 80)  # 24 rows, 80 cols typical
        
        # If many rows changed and density is high, screen likely shifted
        if len(rows_with_changes) > 15 and change_density > 0.05:
            return True
        
        # If player position same but many changes, check for systematic pattern
        if prev_player_pos is None and len(changes) > 50:
            return True
        
        return False
    
    def _get_shift_direction(self, changes: list[dict[str, Any]]) -> tuple[int, int]:
        """Estimate screen shift direction (dy, dx)."""
        if not changes:
            return (0, 0)
        
        # Group changes by row to detect vertical shift
        rows_changed = {}
        for c in changes:
            if c["to"] != " ":
                if c["row"] not in rows_changed:
                    rows_changed[c["row"]] = []
                rows_changed[c["row"]].append(c)
        
        # Simple heuristic: if changes are concentrated in certain rows
        # This is a rough estimate
        if rows_changed:
            avg_row = sum(rows_changed.keys()) / len(rows_changed)
            # If changes are at bottom, screen likely scrolled up (dy = -1)
            # If changes are at top, screen likely scrolled down (dy = 1)
            if avg_row > 15:
                return (-1, 0)
            elif avg_row < 8:
                return (1, 0)
        
        return (0, 0)
    
    def analyze(self, current_lines: list[str], previous_lines: list[str] | None = None
               ) -> dict[str, Any]:
        """Analyze what changed between screens."""
        result = {
            "player_moved": False,
            "player_delta": (0, 0),
            "screen_shifted": False,
            "shift_direction": (0, 0),
            "player_moved_world": False,
            "terrain_changes": [],
        }
        
        curr_player = self._find_player(current_lines)
        
        # If no previous screen, just record current state
        if previous_lines is None or self._previous_player_pos is None:
            result["player_screen_pos"] = curr_player
            self._previous_player_pos = curr_player
            return result
        
        prev_player = self._previous_player_pos
        curr_player = self._find_player(current_lines)
        
        # Find all changes
        changes = self._get_diff_changes(current_lines, previous_lines)
        
        # Determine if player moved (screen position changed)
        player_moved_screen = curr_player is not None and curr_player != prev_player
        
        # Determine if screen shifted
        screen_shifted = self._detect_screen_shift(
            changes, self._previous_player_pos, curr_player
        )
        
        # Calculate player delta
        player_delta = (0, 0)
        if curr_player and prev_player:
            player_delta = (
                curr_player[0] - prev_player[0],
                curr_player[1] - prev_player[1],
            )
        
        # Screen didn't shift but player moved in screen coords
        if player_moved_screen and not screen_shifted:
            result["player_moved"] = True
            result["player_moved_world"] = True
            result["player_delta"] = player_delta
        
        # Screen shifted
        if screen_shifted:
            result["screen_shifted"] = True
            result["shift_direction"] = self._get_shift_direction(changes)
        
        # Record terrain changes
        for c in changes:
            if c["to"] not in " @":  # Skip player and spaces
                result["terrain_changes"].append({
                    "row": c["row"],
                    "col": c["col"],
                    "from": c["from"],
                    "to": c["to"],
                })
        
        # Update internal state
        self._previous_player_pos = curr_player
        result["player_screen_pos"] = curr_player
        
        return result
