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
            "min_x": None,
            "max_x": None,
            "min_y": None,
            "max_y": None,
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
    
    def _update_bounds(self, x: int, y: int):
        """Update the map bounds to include the given coordinates."""
        if self.bounds["min_x"] is None:
            self.bounds["min_x"] = x
            self.bounds["max_x"] = x
            self.bounds["min_y"] = y
            self.bounds["max_y"] = y
        else:
            self.bounds["min_x"] = min(self.bounds["min_x"], x)
            self.bounds["max_x"] = max(self.bounds["max_x"], x)
            self.bounds["min_y"] = min(self.bounds["min_y"], y)
            self.bounds["max_y"] = max(self.bounds["max_y"], y)
    
    def get_tile(self, x: int, y: int) -> Tile | None:
        """Get tile at given world coordinates."""
        return self.tiles.get((x, y))
    
    def set_tile(self, x: int, y: int, terrain: str, seen: bool = False):
        """Set tile at given coordinates."""
        if terrain == '>':
            self.downstairs_pos = (x, y)
        passable = terrain in self.PASSABLE_TERRAIN
        self.tiles[(x, y)] = Tile(
            terrain=terrain,
            visited=False,
            seen=seen,
            passable=passable,
        )
        self._update_bounds(x, y)
    
    def mark_visited(self, x: int, y: int):
        """Mark tile as visited (player has stood here)."""
        if (x, y) in self.tiles:
            self.tiles[(x, y)].visited = True
            self.tiles[(x, y)].seen = True
    
    def mark_seen(self, x: int, y: int):
        """Mark tile as seen (currently visible on screen)."""
        if (x, y) in self.tiles:
            self.tiles[(x, y)].seen = True
    
    def set_player_position(self, world_x: int, world_y: int, terrain: str = '.'):
        """Set player's world position and mark tile as visited."""
        self.player_world_pos = (world_x, world_y)
        if (world_x, world_y) not in self.tiles:
            self.set_tile(world_x, world_y, terrain)
        self.tiles[(world_x, world_y)].visited = True
        self.tiles[(world_x, world_y)].seen = True
    
    def get_explored_count(self) -> int:
        """Count tiles that have been visited."""
        return sum(1 for t in self.tiles.values() if t.visited)
    
    def get_seen_count(self) -> int:
        """Count tiles that have been seen."""
        return sum(1 for t in self.tiles.values() if t.seen)
    
    def get_unvisited_tiles(self) -> list[tuple[int, int]]:
        """Get list of unvisited tile coordinates."""
        return [
            (x, y) for (x, y), tile in self.tiles.items()
            if tile.visited == False and tile.passable
        ]
    
    def to_dict(self) -> dict[str, Any]:
        """Convert map to dictionary for serialization."""
        bounds = self.bounds.copy()
        if bounds["min_x"] is None:
            bounds = {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
        return {
            "level": self.level,
            "player_world_pos": self.player_world_pos,
            "player_screen_pos": self.player_screen_pos,
            "screen_offset": self._screen_offset,
            "bounds": bounds,
            "downstairs_pos": self.downstairs_pos,
            "tiles": {
                f"{x},{y}": tile.to_dict()
                for (x, y), tile in self.tiles.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DungeonMap":
        """Create map from dictionary."""
        map_obj = cls(level=data["level"])
        map_obj.player_world_pos = tuple(data["player_world_pos"]) if data["player_world_pos"] else None
        map_obj.player_screen_pos = tuple(data["player_screen_pos"]) if data["player_screen_pos"] else None
        map_obj._screen_offset = tuple(data["screen_offset"]) if data["screen_offset"] else None
        map_obj.bounds = data["bounds"]
        map_obj.downstairs_pos = tuple(data["downstairs_pos"]) if data["downstairs_pos"] else None
        
        for (xy_str, tile_data) in data["tiles"].items():
            x, y = map(int, xy_str.split(","))
            map_obj.tiles[(x, y)] = Tile.from_dict(tile_data)
        
        return map_obj
    
    def get_terrain_at_screen_pos(self, screen_lines: list[str], screen_row: int, screen_col: int) -> str | None:
        """Get terrain at screen position (converting from screen to world coords)."""
        # Screen center is (0,0) in world coordinates
        screen_center_row = len(screen_lines) // 2
        screen_center_col = max(len(line) for line in screen_lines) // 2 if screen_lines else 40
        
        world_x = screen_col - screen_center_col
        world_y = screen_row - screen_center_row
        
        tile = self.get_tile(world_x, world_y)
        if tile:
            return tile.terrain
        return None
    
    def get_map_string(self) -> str:
        """Generate a string representation of the map showing only seen terrain."""
        bounds = self.bounds
        min_x = bounds.get("min_x", 0) if bounds.get("min_x") is not None else 0
        max_x = bounds.get("max_x", 0) if bounds.get("max_x") is not None else 0
        min_y = bounds.get("min_y", 0) if bounds.get("min_y") is not None else 0
        max_y = bounds.get("max_y", 0) if bounds.get("max_y") is not None else 0
        
        # Add padding around the seen area
        padding = 2
        min_x -= padding
        max_x += padding
        min_y -= padding
        max_y += padding
        
        # Build map showing only seen tiles
        map_lines = []
        for y in range(max_y, min_y - 1, -1):  # Y goes from top (max) to bottom (min)
            line = ""
            for x in range(min_x, max_x + 1):
                tile = self.get_tile(x, y)
                if tile and tile.seen:
                    # Show actual terrain, but replace @ and $ with ?
                    terrain = tile.terrain
                    if terrain in '@$':
                        terrain = '?'
                    elif terrain == ' ':  # cave wall in game -> X in map
                        terrain = 'X'
                    else:
                        terrain = terrain
                    line += terrain
                elif tile:  # unseen but known tile - show ?
                    line += '?'
                else:
                    line += " "  # completely unknown
            map_lines.append(line)
        
        return "\n".join(map_lines)
