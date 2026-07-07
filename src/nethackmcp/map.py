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
        map_obj.downstairs_pos = tuple(data["downstairs_pos"]) if data.get("downstairs_pos") else None
        
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
        
        # Find player position in game area
        player_pos = None
        for row in range(game_start, game_end + 1):
            if row < len(screen_lines):
                col = screen_lines[row].find('@')
                if col >= 0:
                    player_pos = (row, col)
                    break
        
        # If no player found, try to find at center
        if player_pos is None:
            player_pos = ((game_start + game_end) // 2, 40)
        
        player_row, player_col = player_pos
        
        # First, collect all visible tiles from screen
        # We need to track which coordinates have terrain to handle empty spaces
        tiles_with_terrain = set()
        
        for row in range(game_start, game_end + 1):
            if row >= len(screen_lines):
                continue
            line = screen_lines[row]
            for col, char in enumerate(line):
                # Include all visible characters (not just passable/impassable)
                # But skip player position (@) and empty space
                if char != ' ' and char != '@':
                    # Calculate world coordinates
                    screen_row_offset = row - player_row
                    screen_col_offset = col - player_col
                    world_y = 0 - screen_row_offset
                    world_x = 0 - screen_col_offset
                    
                    tiles_with_terrain.add((world_x, world_y))
                    map_obj.set_tile(world_x, world_y, char, seen=True)
        
        # Mark player position as visited
        map_obj.player_world_pos = (0, 0)
        if (0, 0) in map_obj.tiles:
            map_obj.mark_visited(0, 0)
        else:
            # Place player on floor if no tile exists
            map_obj.set_tile(0, 0, '.')
            map_obj.mark_visited(0, 0)
        
        # Mark tiles adjacent to player as visited (for adjacency logic)
        adjacent_tiles = set()
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                adj_x, adj_y = dx, dy
                adjacent_tiles.add((adj_x, adj_y))
        
        # Mark ALL tiles that are on screen as visited (they're "seen" and part of visible dungeon)
        # But keep gold ($) as unvisited so it shows as ? in output
        for (x, y), tile in map_obj.tiles.items():
            if tile.terrain != '$':
                map_obj.mark_visited(x, y)
        
        # Now fill in empty spaces adjacent to passable tiles as seen (so they show ?)
        # Find all tiles that are adjacent to visited tiles but don't have terrain
        visited_tiles = [(x, y) for (x, y), t in map_obj.tiles.items() if t.visited]
        
        for (vx, vy) in visited_tiles:
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    tx, ty = vx + dx, vy + dy
                    if (tx, ty) not in map_obj.tiles:
                        # Empty space adjacent to visited tile
                        # Check if it's actually in the screen as empty space
                        # Calculate screen position
                        screen_row_offset = vy  # Since player_y = 0
                        screen_col_offset = vx
                        screen_row = player_row - screen_row_offset
                        screen_col = player_col - screen_col_offset
                        
                        # Check if this screen position has empty space
                        if 0 <= screen_row < len(screen_lines):
                            line = screen_lines[screen_row]
                            if screen_col < len(line) and line[screen_col] == ' ':
                                # Empty space - add with seen=True so it shows ?
                                map_obj.set_tile(tx, ty, ' ', seen=True)
        
        return map_obj
    
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
                if tile and tile.visited:
                    # Show actual terrain, but replace @, $, and player position with ?
                    terrain = tile.terrain
                    if terrain in '@$':
                        terrain = '?'
                    elif (x, y) == self.player_world_pos:
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
