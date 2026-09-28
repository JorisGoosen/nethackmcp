"""Mapping system for NetHack dungeon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Tuple


@dataclass
class Tile:
    """Represents a single tile in the dungeon map."""
    terrain: str
    visited: bool = False
    seen: bool = False
    passable: bool = True
    
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
        self.dungeon_offset: tuple[int, int] = (0, 0)
        self.downstairs_pos: tuple[int, int] | None = None
        self._cached_rooms: dict[int, dict] | None = None
    
    def _get_neighbor(self, row: int, col: int) -> str | None:
        tile = self.get_tile(row, col)
        return tile.terrain if tile else None
    
    def _is_terrain_passable(self, row: int, col: int) -> bool:
        tile = self.get_tile(row, col)
        if not tile:
            return False
        
        terrain = tile.terrain
        
        if terrain in self.PASSABLE_TERRAIN:
            return True
        
        if terrain == '-':
            above = self._get_neighbor(row - 1, col)
            below = self._get_neighbor(row + 1, col)
            left = self._get_neighbor(row, col - 1)
            right = self._get_neighbor(row, col + 1)
            # Passable if has | above/below AND no - connecting left/right
            has_vertical = above == '|' or below == '|'
            not_connected_horiz = left != '-' and right != '-'
            return has_vertical and not_connected_horiz
        
        if terrain == '|':
            left = self._get_neighbor(row, col - 1)
            right = self._get_neighbor(row, col + 1)
            above = self._get_neighbor(row - 1, col)
            below = self._get_neighbor(row + 1, col)
            # Passable if has - left/right AND no | connecting above/below
            has_horizontal = left == '-' or right == '-'
            not_connected_vert = above != '|' and below != '|'
            return has_horizontal and not_connected_vert
        
        return False
    
    def _flood_fill_room(self, start_row: int, start_col: int, visited: set) -> list[tuple[int, int]]:
        """Flood fill passable tiles (.), stopping at walls or # tunnels."""
        from collections import deque
        
        room_tiles = []
        queue = deque([(start_row, start_col)])
        room_visited = {(start_row, start_col)}
        
        while queue:
            row, col = queue.popleft()
            room_tiles.append((row, col))
            
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = row + dr, col + dc
                if (nr, nc) in room_visited:
                    continue
                
                tile = self.get_tile(nr, nc)
                if not tile:
                    continue
                
                if tile.terrain == '.':
                    room_visited.add((nr, nc))
                    queue.append((nr, nc))
        
        return room_tiles
    
    def _detect_rooms(self) -> dict[int, dict]:
        """Detect all rooms using flood-fill on passable tiles."""
        rooms: dict[int, dict] = {}
        all_visited: set[tuple[int, int]] = set()
        room_id = 0
        
        potential_starts = [(pos) for pos, tile in self.tiles.items() if tile.terrain == '.']
        
        for row, col in potential_starts:
            if (row, col) in all_visited:
                continue
            
            room_tiles = self._flood_fill_room(row, col, all_visited)
            
            if room_tiles:
                tiles_set = set(room_tiles)
                all_visited.update(tiles_set)
                
                rows = [r for r, c in room_tiles]
                cols = [c for r, c in room_tiles]
                
                rooms[room_id] = {
                    "tiles": room_tiles,
                    "bounds": {
                        "min_row": min(rows),
                        "max_row": max(rows),
                        "min_col": min(cols),
                        "max_col": max(cols),
                    },
                    "name": f"Room {room_id}",
                }
                room_id += 1
        
        return rooms
    
    def _get_room_at(self, row: int, col: int) -> int | None:
        """Get room ID at given position. Returns None if not in a room."""
        if self._cached_rooms is None:
            self._cached_rooms = self._detect_rooms()
        
        for room_id, room_data in self._cached_rooms.items():
            if (row, col) in room_data["tiles"]:
                return room_id
        return None
    
    def _get_room_relative_pos(self, row: int, col: int, room_id: int) -> tuple[float, float]:
        """Get position within room as percentage from top-left."""
        if self._cached_rooms is None:
            self._cached_rooms = self._detect_rooms()
        
        room = self._cached_rooms.get(room_id)
        if not room:
            return (0.0, 0.0)
        
        bounds = room["bounds"]
        
        rel_row = row - bounds["min_row"]
        rel_col = col - bounds["min_col"]
        
        room_height = bounds["max_row"] - bounds["min_row"] + 1
        room_width = bounds["max_col"] - bounds["min_col"] + 1
        
        pct_row = rel_row / room_height if room_height > 0 else 0.0
        pct_col = rel_col / room_width if room_width > 0 else 0.0
        
        return (pct_row, pct_col)
    
    def _get_dynamic_room_label_pos(self, room_id: int, player_row: int, player_col: int) -> tuple[int, int]:
        """Get optimal position for room label (avoiding player and stairs)."""
        if self._cached_rooms is None:
            self._cached_rooms = self._detect_rooms()
        
        room = self._cached_rooms.get(room_id)
        if not room:
            return (0, 0)
        
        bounds = room["bounds"]
        min_r, max_r = bounds["min_row"], bounds["max_row"]
        min_c, max_c = bounds["min_col"], bounds["max_col"]
        
        corners = [
            (min_r + 1, min_c + 1, "top-left"),
            (min_r + 1, max_c - 1, "top-right"),
            (max_r - 1, min_c + 1, "bottom-left"),
            (max_r - 1, max_c - 1, "bottom-right"),
        ]
        
        corners_with_stairs = []
        for r, c, name in corners:
            tile = self.get_tile(r, c)
            has_stairs = tile and tile.terrain in ('<', '>')
            is_player = (r, c) == (player_row, player_col)
            if not has_stairs and not is_player:
                corners_with_stairs.append((r, c, name))
        
        if not corners_with_stairs:
            return (min_r + 1, min_c + 1)
        
        farthest_corner = max(corners_with_stairs, 
                             key=lambda x: abs(x[0] - player_row) + abs(x[1] - player_col))
        
        return (farthest_corner[0], farthest_corner[1])
    
    def _is_adjacent_to_passable(self, row: int, col: int) -> bool:
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if dr == 0 and dc == 0:
                    continue
                neighbor_terrain = self._get_neighbor(row + dr, col + dc)
                if neighbor_terrain and self._is_terrain_passable(row + dr, col + dc):
                    return True
        return False
    
    def _update_bounds(self, row: int, col: int):
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
        return self.tiles.get((row, col))
    
    def set_tile(self, row: int, col: int, terrain: str, seen: bool = False, visited: bool = False):
        existing_tile = self.tiles.get((row, col))
        
        if terrain == '>':
            self.downstairs_pos = (row, col)
        
        if existing_tile:
            passable = terrain in self.PASSABLE_TERRAIN
            self.tiles[(row, col)] = Tile(
                terrain=terrain,
                visited=visited or existing_tile.visited,
                seen=seen or existing_tile.seen,
                passable=passable,
            )
        else:
            passable = terrain in self.PASSABLE_TERRAIN
            self.tiles[(row, col)] = Tile(
                terrain=terrain,
                visited=visited,
                seen=seen,
                passable=passable,
            )
        tile = self.tiles[(row, col)]
        self._cached_rooms = None
        if not (terrain == ' ' and not tile.visited):
            self._update_bounds(row, col)
    
    def mark_visited(self, row: int, col: int):
        if not isinstance(row, int) or not isinstance(col, int):
            raise ValueError(f"Coordinates must be integers, got ({type(row).__name__}, {type(col).__name__})")
        
        if (row, col) in self.tiles:
            tile = self.tiles[(row, col)]
            tile.visited = True
            tile.seen = True
            if tile.terrain == ' ':
                tile.terrain = 'X'
            self._update_bounds(row, col)
        else:
            self.tiles[(row, col)] = Tile(terrain='.', visited=True, seen=True, passable=True)
    
    def mark_seen(self, row: int, col: int):
        if not isinstance(row, int) or not isinstance(col, int):
            raise ValueError(f"Coordinates must be integers, got ({type(row).__name__}, {type(col).__name__})")
        
        if (row, col) in self.tiles:
            self.tiles[(row, col)].seen = True
        else:
            self.tiles[(row, col)] = Tile(terrain='.', visited=False, seen=True, passable=True)
    
    def set_player_position(self, world_row: int, world_col: int, terrain: str = '.'):
        self.player_world_pos = (world_row, world_col)
        if (world_row, world_col) not in self.tiles:
            self.set_tile(world_row, world_col, terrain)
        self.tiles[(world_row, world_col)].visited = True
        self.tiles[(world_row, world_col)].seen = True
    
    def get_explored_count(self) -> int:
        return sum(1 for t in self.tiles.values() if t.visited)
    
    def get_seen_count(self) -> int:
        return sum(1 for t in self.tiles.values() if t.seen)
    
    def get_unvisited_tiles(self) -> list[tuple[int, int]]:
        return [
            (row, col) for (row, col), tile in self.tiles.items()
            if tile.visited == False and tile.passable
        ]
    
    def to_dict(self) -> dict[str, Any]:
        bounds = self.bounds.copy()
        if bounds["min_row"] is None:
            bounds = {"min_row": 0, "max_row": 0, "min_col": 0, "max_col": 0}
        return {
            "level": self.level,
            "player_world_pos": self.player_world_pos,
            "bounds": bounds,
            "downstairs_pos": self.downstairs_pos,
            "tiles": {
                f"{row},{col}": tile.to_dict()
                for (row, col), tile in self.tiles.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DungeonMap":
        map_obj = cls(level=data["level"])
        map_obj.player_world_pos = tuple(data["player_world_pos"]) if data["player_world_pos"] else None
        bounds_data = data["bounds"]
        map_obj.bounds = {
            "min_row": bounds_data.get("min_row"),
            "max_row": bounds_data.get("max_row"),
            "min_col": bounds_data.get("min_col"),
            "max_col": bounds_data.get("max_col"),
        }
        map_obj.downstairs_pos = tuple(data["downstairs_pos"]) if data.get("downstairs_pos") else None
        
        for (xy_str, tile_data) in data["tiles"].items():
            x, y = map(int, xy_str.split(","))
            map_obj.tiles[(x, y)] = Tile.from_dict(tile_data)
        
        return map_obj
    
    def get_map_string(self) -> str:
        bounds = self.bounds
        min_row = bounds.get("min_row", 0) if bounds.get("min_row") is not None else 0
        max_row = bounds.get("max_row", 0) if bounds.get("max_row") is not None else 0
        min_col = bounds.get("min_col", 0) if bounds.get("min_col") is not None else 0
        max_col = bounds.get("max_col", 0) if bounds.get("max_col") is not None else 0
        
        padding = 5
        min_row -= padding
        max_row += padding
        min_col -= padding
        max_col += padding
        
        visited_tiles = [(row, col) for (row, col), t in self.tiles.items() if t.visited]
        if visited_tiles:
            min_row = min(min_row, min(r for r, c in visited_tiles) - padding)
            max_row = max(max_row, max(r for r, c in visited_tiles) + padding)
            min_col = min(min_col, min(c for r, c in visited_tiles) - padding)
            max_col = max(max_col, max(c for r, c in visited_tiles) + padding)
        
        map_lines = [""]
        rooms = self._detect_rooms()
        self._cached_rooms = rooms
        
        room_labels: dict[tuple[int, int], str] = {}
        if rooms and self.player_world_pos:
            for room_id, room_data in rooms.items():
                if room_data["tiles"]:
                    label_row, label_col = self._get_dynamic_room_label_pos(
                        room_id, self.player_world_pos[0], self.player_world_pos[1]
                    )
                    room_labels[(label_row, label_col)] = str(room_id)
        
        for row in range(min_row, max_row + 1):
            line = ""
            for col in range(min_col, max_col + 1):
                if (row, col) in room_labels:
                    terrain = room_labels[(row, col)]
                else:
                    tile = self.get_tile(row, col)
                    
                    if tile and (tile.visited or tile.seen):
                        terrain = tile.terrain
                    elif tile:
                        terrain = tile.terrain
                    else:
                        terrain = ' '
                    
                    if terrain is None or len(terrain) != 1:
                        terrain = ' '
                    
                    if terrain == ' ' and tile and tile.visited:
                        terrain = 'X'
                    elif terrain == ' ' and self._is_adjacent_to_passable(row, col):
                        terrain = '?'

                    if self.player_world_pos and (row, col) == self.player_world_pos:
                        terrain = '@'

                line += terrain
            
            map_lines.append(line)
        
        result = "\n".join(map_lines) + "\n"
        
        if rooms:
            room_header = "\n### ROOMS ###\n"
            for room_id, room_data in rooms.items():
                bounds = room_data["bounds"]
                room_header += f"Room {room_id}: rows {bounds['min_row']}-{bounds['max_row']}, cols {bounds['min_col']}-{bounds['max_col']}\n"
            result += room_header
        
        return result
    
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
        
        # Find player position anywhere on screen
        player_pos = None
        for row in range(len(screen_lines)):
            col = screen_lines[row].find('@')
            if col >= 0:
                player_pos = (row, col)
                break
        
        if player_pos is None:
            raise ValueError("No player position (@) found on screen")
        
        player_row, player_col = player_pos
        
        # WHITELIST of terrain characters to include in map (geometry only)
        TERRAIN_WHITELIST = {'#', '.', '+', '-', '|', '<', '>'}
        
        for row in range(len(screen_lines)):
            line = screen_lines[row]
            for col, char in enumerate(line):
                if char == '@':
                    continue
                if char not in TERRAIN_WHITELIST:
                    continue
                
                # Calculate world coordinates: tile is offset from player position
                # Player is at screen (player_row, player_col) and world (0, 0)
                world_row = row - player_row
                world_col = col - player_col
                tile_char = char
                map_obj.set_tile(world_row, world_col, tile_char, seen=True)
        
        # Set player position at world origin (0, 0)
        map_obj.player_world_pos = (0, 0)
        
        # Mark player position as visited (player is at world (0, 0) in from_screen)
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
                        map_obj.set_tile(world_row, world_col, ' ', seen=True, visited=True)
        
        return map_obj
    
    def find_best_dungeon_offset(
        self, 
        screen_lines: list[str], 
        search_range: int = 10
    ) -> tuple[int, int]:
        """
        Find the best dungeon offset by matching static terrain tiles.
        
        Searches around the current dungeon_offset for the best match.
        
        For each possible offset, calculate world positions of all static
        terrain on screen and count how many match existing map tiles.
        Return offset with highest score.
        """
        center_row, center_col = self.dungeon_offset
        best_offset = self.dungeon_offset
        best_score = -1
        
        TERRAIN_WHITELIST = {'#', '.', '+', '-', '|', '<', '>'}
        
        for delta_row in range(-search_range, search_range + 1):
            for delta_col in range(-search_range, search_range + 1):
                offset_row = center_row + delta_row
                offset_col = center_col + delta_col
                
                score = 0
                
                for row in range(len(screen_lines)):
                    line = screen_lines[row]
                    for col, char in enumerate(line):
                        if char not in TERRAIN_WHITELIST:
                            continue
                        
                        world_row = offset_row + row
                        world_col = offset_col + col
                        
                        existing = self.get_tile(world_row, world_col)
                        
                        if existing and existing.terrain == char:
                            score += 1
                        elif existing and existing.terrain in ['.', '#'] and char in ['.', '#']:
                            score += 0.5
                        elif char in ['.', '#'] and not existing:
                            score += 0.1
                
                if score > best_score:
                    best_score = score
                    best_offset = (offset_row, offset_col)
        
        return best_offset
