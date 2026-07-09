"""Mapping system for NetHack dungeon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tile:
    """Represents a single tile in the dungeon map."""
    terrain: str
    visited: bool = False
    seen: bool = False
    passable: bool = True  # Will be recalculated by DungeonMap.set_tile
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "terrain": self.terrain,
            "visited": self.visited,
            "seen": self.seen,
            "passable": self.passable,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tile":
        return cls(
            terrain=data["terrain"],
            visited=data.get("visited", False),
            seen=data.get("seen", False),
            passable=data.get("passable", True),
        )


class DungeonMap:
    """Tracks the dungeon map for a single level."""
    
    # Terrain mappings
    PASSABLE_TERRAIN = {'#', '.', '+', '>'}
    IMPASSABLE_TERRAIN = {'|', '-', ' ', '<'}
    
    def __init__(self, level: int = 1):
        self.level = level
        self.tiles: dict[tuple[int, int], Tile] = {}
        self.bounds = {
            "min_row": None,
            "max_row": None,
            "min_col": None,
            "max_col": None,
        }
        self.player_world_pos: tuple[int, int] | None = None
        self.player_screen_pos: tuple[int, int] | None = None  # (row, col) on screen
        self._screen_offset: tuple[int, int] | None = None  # (row_offset, col_offset)
        self.downstairs_pos: tuple[int, int] | None = None  # World coordinates of downstairs
    
    @property
    def screen_offset(self) -> tuple[int, int]:
        """Get screen offset, defaulting to (0, 0)."""
        if self._screen_offset is None:
            return (0, 0)
        return self._screen_offset
    
    @screen_offset.setter
    def screen_offset(self, value: tuple[int, int]):
        self._screen_offset = value
    
    def _update_bounds(self, row: int, col: int):
        """Update the map bounds to include the given coordinates."""
        if self.bounds["min_row"] is None:
            self.bounds["min_row"] = row
            self.bounds["max_row"] = row
            self.bounds["min_col"] = col
            self.bounds["max_col"] = col
        else:
            self.bounds["min_row"] = min(self.bounds["min_row"], row)
            self.bounds["max_row"] = max(self.bounds["max_row"], row)
            self.bounds["min_col"] = min(self.bounds["min_col"], col)
            self.bounds["max_col"] = max(self.bounds["max_col"], col)
    
    def get_tile(self, row: int, col: int) -> Tile | None:
        """Get tile at given world coordinates."""
        return self.tiles.get((row, col))
    
    def set_tile(self, row: int, col: int, terrain: str, seen: bool = False, visited: bool = False):
        """Set tile at given coordinates."""
        if terrain == '>':
            self.downstairs_pos = (row, col)
        passable = terrain in self.PASSABLE_TERRAIN
        self.tiles[(row, col)] = Tile(
            terrain=terrain,
            visited=visited,
            seen=seen,
            passable=passable,
        )
        self._update_bounds(row, col)
    
    def mark_visited(self, row: int, col: int):
        """Mark tile as visited (player has stood here)."""
        if not isinstance(row, int) or not isinstance(col, int):
            raise ValueError(f"Coordinates must be integers, got ({type(row).__name__}, {type(col).__name__})")
        
        if (row, col) in self.tiles:
            self.tiles[(row, col)].visited = True
            self.tiles[(row, col)].seen = True
        else:
            self.tiles[(row, col)] = Tile(terrain='.', visited=True, seen=True, passable=True)
    
    def mark_seen(self, row: int, col: int):
        """Mark tile as seen (currently visible on screen)."""
        if not isinstance(row, int) or not isinstance(col, int):
            raise ValueError(f"Coordinates must be integers, got ({type(row).__name__}, {type(col).__name__})")
        
        if (row, col) in self.tiles:
            self.tiles[(row, col)].seen = True
        else:
            self.tiles[(row, col)] = Tile(terrain='.', visited=False, seen=True, passable=True)
    
    def set_player_position(self, world_row: int, world_col: int, terrain: str = '.'):
        """Set player's world position and mark tile as visited."""
        self.player_world_pos = (world_row, world_col)
        if (world_row, world_col) not in self.tiles:
            self.set_tile(world_row, world_col, terrain)
        self.tiles[(world_row, world_col)].visited = True
        self.tiles[(world_row, world_col)].seen = True
    
    def get_explored_count(self) -> int:
        """Count tiles that have been visited."""
        return sum(1 for t in self.tiles.values() if t.visited)
    
    def get_seen_count(self) -> int:
        """Count tiles that have been seen."""
        return sum(1 for t in self.tiles.values() if t.seen)
    
    def get_unvisited_tiles(self) -> list[tuple[int, int]]:
        """Get list of unvisited tile coordinates."""
        return [
            (row, col) for (row, col), tile in self.tiles.items()
            if tile.visited == False and tile.passable
        ]
    
    def to_dict(self) -> dict[str, Any]:
        """Convert map to dictionary for serialization."""
        bounds = self.bounds.copy()
        if bounds["min_row"] is None:
            bounds = {"min_row": 0, "max_row": 0, "min_col": 0, "max_col": 0}
        return {
            "level": self.level,
            "player_world_pos": self.player_world_pos,
            "player_screen_pos": self.player_screen_pos,
            "screen_offset": self._screen_offset,
            "bounds": bounds,
            "downstairs_pos": self.downstairs_pos,
            "tiles": {
                f"{row},{col}": tile.to_dict()
                for (row, col), tile in self.tiles.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DungeonMap":
        """Create map from dictionary."""
        map_obj = cls(level=data["level"])
        map_obj.player_world_pos = tuple(data["player_world_pos"]) if data["player_world_pos"] else None
        map_obj.player_screen_pos = tuple(data["player_screen_pos"]) if data["player_screen_pos"] else None
        map_obj._screen_offset = tuple(data["screen_offset"]) if data["screen_offset"] else None
        bounds_data = data["bounds"]
        map_obj.bounds = {
            "min_row": bounds_data.get("min_row", bounds_data.get("min_x")),
            "max_row": bounds_data.get("max_row", bounds_data.get("max_x")),
            "min_col": bounds_data.get("min_col", bounds_data.get("min_y")),
            "max_col": bounds_data.get("max_col", bounds_data.get("max_y")),
        }
        map_obj.downstairs_pos = tuple(data["downstairs_pos"]) if data.get("downstairs_pos") else None
        
        for (xy_str, tile_data) in data["tiles"].items():
            x, y = map(int, xy_str.split(","))
            map_obj.tiles[(x, y)] = Tile.from_dict(tile_data)
        
        return map_obj
    
    def get_terrain_at_screen_pos(self, screen_lines: list[str], screen_row: int, screen_col: int) -> str | None:
        """Get terrain at screen position (converting from screen to world coords).
        
        World coordinates use (row, col) where both increase down/right, consistent with screen.
        Screen center is world origin (0, 0).
        """
        screen_center_row = len(screen_lines) // 2
        screen_center_col = max(len(line) for line in screen_lines) // 2 if screen_lines else 40
        
        # World coords: (row, col) where + = down/right (same as screen)
        world_row = screen_row - screen_center_row
        world_col = screen_col - screen_center_col
        
        tile = self.get_tile(world_row, world_col)
        if tile:
            return tile.terrain
        return None
    
    @classmethod
    def from_screen(cls, screen_lines: list[str], game_start: int = 6, game_end: int = 15) -> "DungeonMap":
        """Create a map from a screen screenshot.
        
        Args:
            screen_lines: List of screen row strings
            game_start: First row of game area (default 6)
            game_end: Last row of game area (default 15)
            
        Returns:
            DungeonMap with tiles populated from the screen
        """
        map_obj = cls(level=1)
        
        # Find player position anywhere on screen (not limited to game_start/game_end)
        player_pos = None
        for row in range(len(screen_lines)):
            col = screen_lines[row].find('@')
            if col >= 0:
                player_pos = (row, col)
                break
        
        # If no player found, raise an error (player must be visible)
        if player_pos is None:
            raise ValueError("No player position (@) found on screen")
        
        player_row, player_col = player_pos
        
        # Player is ALWAYS at world origin (0, 0) in from_screen
        # All tiles are offset relative to player position
        
        # WHITELIST of terrain characters to include in map (geometry only)
        TERRAIN_WHITELIST = {'#', '.', '+', '-', '|', '<', '>'}
        
        for row in range(len(screen_lines)):
            line = screen_lines[row]
            for col, char in enumerate(line):
                if char == '@':
                    continue  # Player not terrain
                if char not in TERRAIN_WHITELIST:
                    continue  # Items ($, u, x, etc.) not terrain
                # Calculate world coordinates: tile is offset from player position
                # Player is at screen (player_row, player_col) and world (0, 0)
                world_row = row - player_row
                world_col = col - player_col
                map_obj.set_tile(world_row, world_col, char, seen=True)
        
        # Set player position at world origin (0, 0)
        map_obj.player_world_pos = (0, 0)
        
        # Mark player position tile (always at world origin in from_screen)
        if (0, 0) not in map_obj.tiles:
            map_obj.set_tile(0, 0, '.')
        
        # Mark player position as visited
        map_obj.mark_visited(0, 0)
        
        # Mark adjacent tiles as visited
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if dr == 0 and dc == 0:
                    continue
                adj_player_row, adj_player_col = player_row + dr, player_col + dc
                if 0 <= adj_player_row < len(screen_lines) and 0 <= adj_player_col < len(screen_lines[adj_player_row]):
                    char = screen_lines[adj_player_row][adj_player_col]
                    world_row = adj_player_row - player_row
                    world_col = adj_player_col - player_col
                    if char == ' ':
                        # Empty space (cave wall) adjacent to player - mark as seen AND visited
                        map_obj.set_tile(world_row, world_col, ' ', seen=True, visited=True)
                    elif char != '@' and (world_row, world_col) in map_obj.tiles:
                        # Non-empty terrain adjacent to player - mark as visited
                        map_obj.mark_visited(world_row, world_col)
        
        return map_obj
    
    def get_map_string(self) -> str:
        """Generate a string representation of the map showing only seen terrain."""
        bounds = self.bounds
        min_row = bounds.get("min_row", 0) if bounds.get("min_row") is not None else 0
        max_row = bounds.get("max_row", 0) if bounds.get("max_row") is not None else 0
        min_col = bounds.get("min_col", 0) if bounds.get("min_col") is not None else 0
        max_col = bounds.get("max_col", 0) if bounds.get("max_col") is not None else 0
        
        # Add padding around the seen area
        padding = 2
        min_row -= padding
        max_row += padding
        min_col -= padding
        max_col += padding
        
        # Find all visited tiles to determine the explored region
        visited_tiles = [(row, col) for (row, col), t in self.tiles.items() if t.visited]
        if visited_tiles:
            min_row = min(min_row, min(r for r, col in visited_tiles) - padding)
            max_row = max(max_row, max(r for r, col in visited_tiles) + padding)
            min_col = min(min_col, min(c for r, c in visited_tiles) - padding)
            max_col = max(max_col, max(c for r, c in visited_tiles) + padding)
        
        # Build map showing all seen tiles with actual terrain
        # Only show ? for tiles that are neither seen nor visited (completely unknown)
        map_lines = []
        for row in range(max_row, min_row - 1, -1):  # Row goes from top (max) to bottom (min)
            line = ""
            for col in range(min_col, max_col + 1):
                tile = self.get_tile(row, col)
                if tile and (tile.visited or tile.seen):
                    terrain = tile.terrain
                    if terrain == ' ' and tile.visited:
                        terrain = 'X'
                    elif terrain == ' ' and not tile.visited:
                        terrain = ' '
                    elif (row, col) == self.player_world_pos:
                        terrain = terrain
                    line += terrain
                else:
                    line += '?'  # completely unknown - show ?
            map_lines.append(line)
        
        return "\n".join(map_lines)
