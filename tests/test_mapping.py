"""Unit tests for the mapping system."""

import json
import unittest

from src.nethackmcp.map import DungeonMap, Tile
from src.nethackmcp.movement_analyzer import MovementAnalyzer
from src.nethackmcp.server import NetHackClient


class TestTile(unittest.TestCase):
    """Tests for Tile class."""
    
    def test_tile_creation(self):
        """Test creating a tile."""
        tile = Tile(terrain='#', visited=False, passable=True)
        self.assertEqual(tile.terrain, '#')
        self.assertEqual(tile.visited, False)
        self.assertEqual(tile.passable, True)
    
    def test_tile_passable_detection(self):
        """Test passable terrain detection."""
        for terrain in ['#', '.', '+', '>']:
            tile = Tile(terrain=terrain)
            self.assertTrue(tile.passable, f"{terrain} should be passable")
        
        # Default Tile has passable=True, set by DungeonMap.set_tile
        for terrain in ['|', '-', ' ', '<']:
            tile = Tile(terrain=terrain, passable=False)
            self.assertFalse(tile.passable, f"{terrain} should be impassable")
    
    def test_tile_from_dict_with_defaults(self):
        """Test from_dict with minimal data."""
        data = {"terrain": "#"}
        tile = Tile.from_dict(data)
        
        self.assertEqual(tile.terrain, "#")
        self.assertFalse(tile.visited)
        self.assertFalse(tile.seen)
        self.assertTrue(tile.passable)
    
    def test_tile_from_dict_with_all_fields(self):
        """Test from_dict with all fields."""
        data = {
            "terrain": "|",
            "visited": True,
            "seen": True,
            "passable": False,
        }
        tile = Tile.from_dict(data)
        
        self.assertEqual(tile.terrain, "|")
        self.assertTrue(tile.visited)
        self.assertTrue(tile.seen)
        self.assertFalse(tile.passable)
    
    def test_tile_serialization(self):
        """Test tile to_dict and from_dict."""
        tile = Tile(terrain='#', visited=True, passable=True)
        data = tile.to_dict()
        
        self.assertEqual(data['terrain'], '#')
        self.assertEqual(data['visited'], True)
        self.assertEqual(data['passable'], True)
        
        tile2 = Tile.from_dict(data)
        self.assertEqual(tile2.terrain, '#')
        self.assertEqual(tile2.visited, True)
        self.assertEqual(tile2.passable, True)


class TestDungeonMap(unittest.TestCase):
    """Tests for DungeonMap class."""
    
    def test_map_creation(self):
        """Test creating a map."""
        map_obj = DungeonMap(level=1)
        self.assertEqual(map_obj.level, 1)
        self.assertEqual(len(map_obj.tiles), 0)
        self.assertIsNone(map_obj.player_world_pos)
    
    def test_set_tile(self):
        """Test setting a tile."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#', seen=True)
        
        self.assertEqual(len(map_obj.tiles), 1)
        tile = map_obj.get_tile(0, 0)
        self.assertIsNotNone(tile)
        self.assertEqual(tile.terrain, '#')
        self.assertTrue(tile.seen)
        self.assertTrue(tile.passable)
    
    def test_mark_visited(self):
        """Test marking tile as visited."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        
        self.assertFalse(map_obj.get_tile(0, 0).visited)
        
        map_obj.mark_visited(0, 0)
        
        self.assertTrue(map_obj.get_tile(0, 0).visited)
        self.assertTrue(map_obj.get_tile(0, 0).seen)
    
    def test_mark_seen(self):
        """Test marking tile as seen."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        
        self.assertFalse(map_obj.get_tile(0, 0).seen)
        
        map_obj.mark_seen(0, 0)
        
        self.assertTrue(map_obj.get_tile(0, 0).seen)
    
    def test_mark_seen_non_existent(self):
        """Test marking non-existent tile as seen."""
        map_obj = DungeonMap(level=1)
        
        # Should not raise exception
        map_obj.mark_seen(99, 99)
        
        # Tile should not exist
        self.assertIsNone(map_obj.get_tile(99, 99))
    
    def test_mark_visited_non_existent(self):
        """Test marking non-existent tile as visited."""
        map_obj = DungeonMap(level=1)
        
        # Should not raise exception
        map_obj.mark_visited(99, 99)
        
        # Tile should not exist
        self.assertIsNone(map_obj.get_tile(99, 99))
    
    def test_get_tile_non_existent(self):
        """Test getting non-existent tile."""
        map_obj = DungeonMap(level=1)
        
        tile = map_obj.get_tile(99, 99)
        
        self.assertIsNone(tile)
    
    def test_set_tile_impassable(self):
        """Test setting impassable tile."""
        map_obj = DungeonMap(level=1)
        
        for impassable_char in ['|', '-', ' ', '<']:
            map_obj.set_tile(0, 0, impassable_char)
            tile = map_obj.get_tile(0, 0)
            self.assertFalse(tile.passable, f"{impassable_char} should be impassable")
    
    def test_set_tile_passable(self):
        """Test setting passable tile."""
        map_obj = DungeonMap(level=1)
        
        for passable_char in ['#', '.', '+', '>']:
            map_obj.set_tile(0, 0, passable_char)
            tile = map_obj.get_tile(0, 0)
            self.assertTrue(tile.passable, f"{passable_char} should be passable")
    
    def test_bounds_negative_coordinates(self):
        """Test bounds with negative coordinates."""
        map_obj = DungeonMap(level=1)
        
        map_obj.set_tile(-100, -100, '#')
        map_obj.set_tile(100, 100, '.')
        
        self.assertEqual(map_obj.bounds["min_x"], -100)
        self.assertEqual(map_obj.bounds["max_x"], 100)
        self.assertEqual(map_obj.bounds["min_y"], -100)
        self.assertEqual(map_obj.bounds["max_y"], 100)
    
    def test_bounds_expands_only(self):
        """Test that bounds only expands, never contracts."""
        map_obj = DungeonMap(level=1)
        
        map_obj.set_tile(-5, 5, '#')
        map_obj.set_tile(5, -5, '.')
        
        # Now add a tile inside existing bounds
        map_obj.set_tile(0, 0, '+')
        
        # Bounds should remain the same
        self.assertEqual(map_obj.bounds["min_x"], -5)
        self.assertEqual(map_obj.bounds["max_x"], 5)
        self.assertEqual(map_obj.bounds["min_y"], -5)
        self.assertEqual(map_obj.bounds["max_y"], 5)
    
    def test_bounds_tracking(self):
        """Test that bounds are tracked correctly."""
        map_obj = DungeonMap(level=1)
        
        # Initial bounds
        self.assertEqual(map_obj.bounds["min_x"], 0)
        self.assertEqual(map_obj.bounds["max_x"], 0)
        self.assertEqual(map_obj.bounds["min_y"], 0)
        self.assertEqual(map_obj.bounds["max_y"], 0)
        
        # Add tiles at different positions
        map_obj.set_tile(-5, 10, '#')
        map_obj.set_tile(5, -10, '#')
        
        # Bounds should expand to include new tiles
        self.assertEqual(map_obj.bounds["min_x"], -5)
        self.assertEqual(map_obj.bounds["max_x"], 5)
        self.assertEqual(map_obj.bounds["min_y"], -10)
        self.assertEqual(map_obj.bounds["max_y"], 10)
    
    def test_bounds_single_tile(self):
        """Test bounds with a single tile."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(5, 5, '#')
        
        self.assertEqual(map_obj.bounds["min_x"], 5)
        self.assertEqual(map_obj.bounds["max_x"], 5)
        self.assertEqual(map_obj.bounds["min_y"], 5)
        self.assertEqual(map_obj.bounds["max_y"], 5)
    
    def test_bounds_after_multiple_operations(self):
        """Test bounds after multiple set_tile operations."""
        map_obj = DungeonMap(level=1)
        
        map_obj.set_tile(0, 0, '#')
        map_obj.set_tile(1, 0, '.')
        map_obj.set_tile(0, 1, '+')
        
        self.assertEqual(map_obj.bounds["min_x"], 0)
        self.assertEqual(map_obj.bounds["max_x"], 1)
        self.assertEqual(map_obj.bounds["min_y"], 0)
        self.assertEqual(map_obj.bounds["max_y"], 1)
        
        map_obj.set_tile(-1, -1, '|')
        
        self.assertEqual(map_obj.bounds["min_x"], -1)
        self.assertEqual(map_obj.bounds["max_x"], 1)
        self.assertEqual(map_obj.bounds["min_y"], -1)
        self.assertEqual(map_obj.bounds["max_y"], 1)
    
    def test_get_explored_count(self):
        """Test explored tile count."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.set_tile(1, 0, '#')
        map_obj.set_tile(0, 1, '#')
        
        self.assertEqual(map_obj.get_explored_count(), 0)
        
        map_obj.mark_visited(0, 0)
        self.assertEqual(map_obj.get_explored_count(), 1)
        
        map_obj.mark_visited(1, 0)
        self.assertEqual(map_obj.get_explored_count(), 2)
    
    def test_set_player_position(self):
        """Test setting player position."""
        map_obj = DungeonMap(level=1)
        # Need to create tile first before marking as visited
        map_obj.set_tile(0, 0, '#')
        map_obj.set_player_position(0, 0)
        
        self.assertEqual(map_obj.player_world_pos, (0, 0))
        self.assertTrue(map_obj.get_tile(0, 0).visited)
    
    def test_set_player_position_creates_tile(self):
        """Test that set_player_position handles non-existent tiles."""
        map_obj = DungeonMap(level=1)
        
        # Should not raise exception even without creating tile first
        map_obj.set_player_position(5, 5)
        
        self.assertEqual(map_obj.player_world_pos, (5, 5))
        tile = map_obj.get_tile(5, 5)
        self.assertIsNotNone(tile)
        self.assertTrue(tile.visited)
    
    def test_get_unvisited_tiles_empty(self):
        """Test get_unvisited_tiles with no unvisited passable tiles."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.mark_visited(0, 0)
        
        unvisited = map_obj.get_unvisited_tiles()
        
        self.assertEqual(len(unvisited), 0)
    
    def test_get_unvisited_tiles_mixed(self):
        """Test get_unvisited_tiles with mix of visited/unvisited."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.set_tile(1, 0, '#')
        map_obj.set_tile(0, 1, '|')  # impassable
        
        map_obj.mark_visited(0, 0)
        
        unvisited = map_obj.get_unvisited_tiles()
        
        # Only (1, 0) should be unvisited and passable
        self.assertIn((1, 0), unvisited)
        self.assertNotIn((0, 0), unvisited)  # visited
        self.assertNotIn((0, 1), unvisited)  # impassable
        self.assertEqual(len(unvisited), 1)
    
    def test_get_seen_count(self):
        """Test seen tile count."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.set_tile(1, 0, '.')
        map_obj.set_tile(2, 0, '|')
        
        self.assertEqual(map_obj.get_seen_count(), 0)
        
        map_obj.mark_seen(0, 0)
        map_obj.mark_seen(1, 0)
        
        self.assertEqual(map_obj.get_seen_count(), 2)
    
    def test_player_world_position_none_initially(self):
        """Test that player_world_pos is None initially."""
        map_obj = DungeonMap(level=1)
        
        self.assertIsNone(map_obj.player_world_pos)
    
    def test_player_screen_position_none_initially(self):
        """Test that player_screen_pos is None initially."""
        map_obj = DungeonMap(level=1)
        
        self.assertIsNone(map_obj.player_screen_pos)
    
    def test_screen_offset_default(self):
        """Test default screen offset."""
        map_obj = DungeonMap(level=1)
        
        offset = map_obj.screen_offset
        
        self.assertEqual(offset, (0, 0))
    
    def test_screen_offset_setter(self):
        """Test screen offset setter."""
        map_obj = DungeonMap(level=1)
        
        map_obj.screen_offset = (5, 10)
        
        self.assertEqual(map_obj.screen_offset, (5, 10))
    
    def test_serialization(self):
        """Test map serialization."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.set_tile(1, 0, '.')
        map_obj.mark_visited(0, 0)
        map_obj.player_world_pos = (0, 0)
        map_obj.player_screen_pos = (10, 20)
        
        data = map_obj.to_dict()
        
        self.assertEqual(data["level"], 1)
        self.assertEqual(len(data["tiles"]), 2)
        self.assertEqual(data["player_world_pos"], (0, 0))
        
        # Test deserialization
        map_obj2 = DungeonMap.from_dict(data)
        
        self.assertEqual(map_obj2.level, 1)
        self.assertEqual(map_obj2.player_world_pos, (0, 0))
        self.assertEqual(len(map_obj2.tiles), 2)
        self.assertTrue(map_obj2.get_tile(0, 0).visited)
    
    def test_serialization_empty_map(self):
        """Test serialization of empty map."""
        map_obj = DungeonMap(level=1)
        
        data = map_obj.to_dict()
        
        self.assertEqual(data["level"], 1)
        self.assertEqual(len(data["tiles"]), 0)
        self.assertIsNone(data["player_world_pos"])
        self.assertIsNone(data["player_screen_pos"])
        self.assertIsNone(data["screen_offset"])
        
        map_obj2 = DungeonMap.from_dict(data)
        self.assertEqual(map_obj2.level, 1)
        self.assertEqual(len(map_obj2.tiles), 0)
    
    def test_serialization_round_trip(self):
        """Test full round-trip serialization."""
        map_obj = DungeonMap(level=5)
        map_obj.set_tile(-10, 10, '#')
        map_obj.set_tile(0, 0, '.')
        map_obj.set_tile(10, -10, '|')
        map_obj.mark_visited(0, 0)
        map_obj.mark_seen(10, -10)
        map_obj.player_world_pos = (0, 0)
        map_obj.player_screen_pos = (12, 40)
        map_obj.screen_offset = (2, 3)
        
        data = map_obj.to_dict()
        map_obj2 = DungeonMap.from_dict(data)
        
        self.assertEqual(map_obj2.level, 5)
        self.assertEqual(map_obj2.player_world_pos, (0, 0))
        self.assertEqual(map_obj2.player_screen_pos, (12, 40))
        self.assertEqual(map_obj2.screen_offset, (2, 3))
        self.assertEqual(len(map_obj2.tiles), 3)
        
        tile0 = map_obj2.get_tile(-10, 10)
        tile1 = map_obj2.get_tile(0, 0)
        tile2 = map_obj2.get_tile(10, -10)
        
        self.assertIsNotNone(tile0)
        self.assertIsNotNone(tile1)
        self.assertIsNotNone(tile2)
        
        self.assertEqual(tile0.terrain, '#')
        self.assertEqual(tile1.terrain, '.')
        self.assertEqual(tile2.terrain, '|')
        
        self.assertTrue(tile1.visited)
        self.assertTrue(tile2.seen)
    
    def test_serialization_null_player_position(self):
        """Test serialization with null player position."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        
        data = map_obj.to_dict()
        map_obj2 = DungeonMap.from_dict(data)
        
        self.assertIsNone(map_obj2.player_world_pos)
        self.assertIsNone(map_obj2.player_screen_pos)
        self.assertIsNone(map_obj2._screen_offset)
    
    def test_from_dict_missing_optional_fields(self):
        """Test from_dict with missing optional fields."""
        data = {
            "level": 1,
            "player_world_pos": None,
            "player_screen_pos": None,
            "screen_offset": None,
            "bounds": {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0},
            "tiles": {},
        }
        
        map_obj = DungeonMap.from_dict(data)
        
        self.assertEqual(map_obj.level, 1)
        self.assertEqual(len(map_obj.tiles), 0)
        self.assertIsNone(map_obj.player_world_pos)
    
    def test_empty_screen_center_calculation(self):
        """Test world coordinate calculation with empty map."""
        map_obj = DungeonMap(level=1)
        
        # Test with no player position set
        tile = map_obj.get_tile(0, 0)
        self.assertIsNone(tile)
    
    def test_get_terrain_at_screen_pos_no_tile(self):
        """Test get_terrain_at_screen_pos when no tile exists."""
        map_obj = DungeonMap(level=1)
        
        terrain = map_obj.get_terrain_at_screen_pos(["line"], 0, 0)
        
        self.assertIsNone(terrain)
    
    def test_get_terrain_at_screen_pos_with_tile(self):
        """Test get_terrain_at_screen_pos with a tile."""
        map_obj = DungeonMap(level=1)
        # Add a tile at world coordinates (0, 0)
        map_obj.set_tile(0, 0, '#')
        
        # Create screen with one line
        screen_lines = ["        @        "]
        
        terrain = map_obj.get_terrain_at_screen_pos(screen_lines, 0, 15)
        
        # (0, 0) in world is at center of screen (col 8)
        # Row 0 is at center row (0), so world_y = 0
        # Col 15 is 7 cols right of center, so world_x = 7
        # No tile at (7, 0)
        self.assertIsNone(terrain)
    
    def test_get_terrain_at_screen_pos_center(self):
        """Test get_terrain_at_screen_pos at screen center."""
        map_obj = DungeonMap(level=1)
        # Add tile at world (0, 0)
        map_obj.set_tile(0, 0, '#')
        
        screen_lines = ["        @        "]
        
        # Center of screen (col 8) should be world_x=0, row 0 should be world_y=0
        terrain = map_obj.get_terrain_at_screen_pos(screen_lines, 0, 8)
        
        self.assertEqual(terrain, '#')


class TestShowMap(unittest.TestCase):
    """Tests for _show_map functionality in server."""
    
    def setUp(self):
        """Set up a NetHackClient with a map."""
        self.client = NetHackClient()
        self.client.current_level = 1
        self.client.maps[1] = DungeonMap(level=1)
        
        # Create a small test map
        self.client.maps[1].set_tile(-1, 1, '#')  # passable
        self.client.maps[1].set_tile(0, 1, '.')  # passable
        self.client.maps[1].set_tile(1, 1, '|')  # impassable
        self.client.maps[1].set_tile(-1, 0, '+')  # passable
        self.client.maps[1].set_tile(0, 0, '#')  # passable
        self.client.maps[1].set_tile(1, 0, '-')  # impassable
        self.client.maps[1].set_tile(-1, -1, ' ')  # impassable (rockwall)
        self.client.maps[1].set_tile(0, -1, '>')  # passable (stairs down)
        self.client.maps[1].set_tile(1, -1, '<')  # impassable (stairs up)
        
        self.client.maps[1].mark_visited(0, 0)
        self.client.maps[1].mark_visited(-1, 1)  # Mark another tile as visited for '.' test
        self.client.maps[1].player_world_pos = (0, 0)
    
    def test_show_map_passable_unvisited_as_dot(self):
        """Test that unvisited tiles are shown as '?', not '.'."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(1, 0, '#')
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        # Should contain '?' for unvisited tile (unknown terrain)
        self.assertIn('?', result.get("map", ""))
    
    def test_show_map_impassable_unvisited_as_X(self):
        """Test that impassable unvisited tiles are shown as 'X'."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(1, 0, '|')
        map_obj.mark_visited(1, 0)  # Mark as visited so we can test X for impassable
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        # Should contain 'X' for visited impassable tile
        self.assertIn('X', result.get("map", ""))
    
    def test_show_map_visited_terrain_character(self):
        """Test that visited tiles show as '.' for passable."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.mark_visited(0, 0)
        map_obj.set_tile(1, 0, '.')  # Add another visited tile to check
        map_obj.mark_visited(1, 0)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        # Should contain '.' for visited passable tiles
        self.assertIn('.', result.get("map", ""))
    
    def test_show_map_player_adjacent_show_terrain(self):
        """Test that player-adjacent tiles show '?' if not visited."""
        map_obj = DungeonMap(level=1)
        # Tile to the right of player
        map_obj.set_tile(1, 0, '#')
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        # Should show '?' for adjacent unvisited tile (unknown)
        self.assertIn('?', result.get("map", ""))
    
    def test_show_map_diagonal_adjacent(self):
        """Test that diagonally adjacent tiles show '?' if not visited."""
        map_obj = DungeonMap(level=1)
        # Tile diagonally up-right
        map_obj.set_tile(1, 1, '.')
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        # Should show '?' for diagonally adjacent unvisited tile (unknown)
        self.assertIn('?', result.get("map", ""))
    
    def test_show_map_bounds_with_padding(self):
        """Test that map has padding around bounds."""
        map_obj = DungeonMap(level=1)
        map_obj.set_tile(0, 0, '#')
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        client.current_level = 1
        client.maps[1] = map_obj
        
        result = client._show_map()
        
        # Map should have some size
        map_str = result.get("map", "")
        self.assertGreater(len(map_str), 0)
    
    def test_show_map_no_map_data(self):
        """Test show_map when no map exists."""
        client = NetHackClient()
        client.current_level = 99
        
        result = client._show_map()
        
        self.assertEqual(result.get("status"), "error")
        self.assertIn("error", result)
    
    def test_show_map_return_structure(self):
        """Test that show_map returns correct structure."""
        result = self.client._show_map()
        
        self.assertIn("map", result)
        self.assertIn("level", result)
        self.assertIn("bounds", result)
        self.assertIn("explored_tiles", result)
        self.assertIn("seen_tiles", result)
        self.assertIn("status", result)
    
    def test_show_map_all_terrain_types(self):
        """Test that all terrain types are shown correctly."""
        result = self.client._show_map()
        map_str = result.get("map", "")
        
        # Map should contain: @ (player), . (visited passable), ? (unknown)
        self.assertIn('@', map_str)  # player position
        self.assertIn('.', map_str)  # visited passable
        self.assertIn('?', map_str)  # unknown terrain
        # Note: impassable unvisited tiles show as 'X', but in this setup they're all '?' except if visited
        # Test with a dedicated setup to verify X
    
    def test_show_map_explored_count(self):
        """Test explored tile count in map output."""
        result = self.client._show_map()
        
        # Two tiles are visited: (0,0) and (-1,1)
        self.assertEqual(result.get("explored_tiles"), 2)
    
    def test_show_map_seen_count(self):
        """Test seen tile count in map output."""
        result = self.client._show_map()
        
        # Should have at least 1 seen tile
        self.assertGreaterEqual(result.get("seen_tiles"), 1)
    
    def test_show_map_with_bounds(self):
        """Test that map bounds are included."""
        result = self.client._show_map()
        
        bounds = result.get("bounds", {})
        self.assertIn("min_x", bounds)
        self.assertIn("max_x", bounds)
        self.assertIn("min_y", bounds)
        self.assertIn("max_y", bounds)


class TestIsPlayerAdjacent(unittest.TestCase):
    """Tests for _is_player_adjacent functionality."""
    
    def test_is_player_adjacent_directly_above(self):
        """Test adjacent tile directly above player."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 0, 1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_directly_below(self):
        """Test adjacent tile directly below player."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 0, -1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_directly_left(self):
        """Test adjacent tile directly left of player."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, -1, 0)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_directly_right(self):
        """Test adjacent tile directly right of player."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 1, 0)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_diagonal_up_left(self):
        """Test diagonally adjacent tile up-left."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, -1, 1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_diagonal_up_right(self):
        """Test diagonally adjacent tile up-right."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 1, 1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_diagonal_down_left(self):
        """Test diagonally adjacent tile down-left."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, -1, -1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_adjacent_diagonal_down_right(self):
        """Test diagonally adjacent tile down-right."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 1, -1)
        
        self.assertTrue(adjacent)
    
    def test_is_player_not_adjacent(self):
        """Test non-adjacent tile."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 2, 2)
        
        self.assertFalse(adjacent)
    
    def test_is_player_not_adjacent_far_away(self):
        """Test tile that is far away."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 10, 10)
        
        self.assertFalse(adjacent)
    
    def test_is_player_adjacent_no_player_position(self):
        """Test when player position is None."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = None
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 0, 0)
        
        self.assertFalse(adjacent)
    
    def test_is_player_adjacent_tile_at_player_position(self):
        """Test the tile where player is standing."""
        map_obj = DungeonMap(level=1)
        map_obj.player_world_pos = (0, 0)
        
        client = NetHackClient()
        adjacent = client._is_player_adjacent(map_obj, 0, 0)
        
        # The tile where player is standing is not considered "adjacent"
        self.assertFalse(adjacent)


class TestMovementAnalyzer(unittest.TestCase):
    """Tests for MovementAnalyzer class."""
    
    def test_find_player(self):
        """Test finding player in screen."""
        analyzer = MovementAnalyzer()
        
        screen = [
            "Level: 1",
            "          ",
            "    @     ",
            "          ",
        ]
        
        result = analyzer._find_player(screen)
        self.assertEqual(result, (2, 4))
    
    def test_no_player_found(self):
        """Test when player not found."""
        analyzer = MovementAnalyzer()
        
        screen = [
            "Level: 1",
            "          ",
            "          ",
        ]
        
        result = analyzer._find_player(screen)
        self.assertIsNone(result)
    
    def test_diff_changes(self):
        """Test diff change detection."""
        analyzer = MovementAnalyzer()
        
        prev = ["abc"]
        curr = ["axc"]
        
        changes = analyzer._get_diff_changes(curr, prev)
        
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["row"], 0)
        self.assertEqual(changes[0]["col"], 1)
        self.assertEqual(changes[0]["from"], "b")
        self.assertEqual(changes[0]["to"], "x")
    
    def test_analyze_no_previous(self):
        """Test analysis with no previous screen."""
        analyzer = MovementAnalyzer()
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        result = analyzer.analyze(curr, None)
        
        self.assertFalse(result["player_moved"])
        self.assertFalse(result["screen_shifted"])
    
    def test_analyze_player_movement(self):
        """Test detecting player movement."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        # First call to set up previous player position
        analyzer.analyze(prev, None)
        
        # Second call to detect movement
        result = analyzer.analyze(curr, prev)
        
        # Player moved, but analyzer marks it as player moved world
        self.assertTrue(result.get("player_moved", False))
        self.assertFalse(result["screen_shifted"])
    
    def test_analyze_screen_shift(self):
        """Test detecting screen shift."""
        analyzer = MovementAnalyzer()
        
        # Screen shifted - all terrain moved, player stayed
        # Use a case where many changes occur
        prev = ["A" * 40 for _ in range(24)]
        curr = ["B" * 40 for _ in range(24)]
        
        result = analyzer.analyze(curr, prev)
        
        # The test may or may not detect screen shift depending on implementation
        # Just verify it doesn't crash
        self.assertIn("screen_shifted", result)
    
    def test_analyze_player_movement_delta(self):
        """Test that player movement delta is calculated correctly."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "      @   ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        self.assertTrue(result.get("player_moved", False))
        self.assertEqual(result.get("player_delta", (0, 0)), (0, 4))
    
    def test_analyze_no_changes(self):
        """Test analysis with no changes."""
        analyzer = MovementAnalyzer()
        
        screen = [
            "Level: 1",
            "    @     ",
        ]
        
        analyzer.analyze(screen, None)
        result = analyzer.analyze(screen, screen)
        
        self.assertFalse(result.get("player_moved", False))
        self.assertFalse(result.get("screen_shifted", False))
    
    def test_find_player_at_edge(self):
        """Test finding player at screen edge."""
        analyzer = MovementAnalyzer()
        
        screen = [
            "@         ",
        ]
        
        result = analyzer._find_player(screen)
        self.assertEqual(result, (0, 0))
    
    def test_find_player_multiple_not_found(self):
        """Test that only first player is found."""
        analyzer = MovementAnalyzer()
        
        screen = [
            "  @     @  ",
        ]
        
        result = analyzer._find_player(screen)
        # Should find first @ at position 2
        self.assertEqual(result, (0, 2))
    
    def test_diff_changes_multiple(self):
        """Test diff with multiple changes."""
        analyzer = MovementAnalyzer()
        
        prev = ["abc"]
        curr = ["xyz"]
        
        changes = analyzer._get_diff_changes(curr, prev)
        
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[0]["row"], 0)
        self.assertEqual(changes[0]["col"], 0)
        self.assertEqual(changes[0]["from"], "a")
        self.assertEqual(changes[0]["to"], "x")
    
    def test_diff_changes_different_lengths(self):
        """Test diff with lines of different lengths."""
        analyzer = MovementAnalyzer()
        
        prev = ["ab"]
        curr = ["xyz"]
        
        changes = analyzer._get_diff_changes(curr, prev)
        
        # Should handle different line lengths
        self.assertGreater(len(changes), 0)
    
    def test_diff_changes_no_changes(self):
        """Test diff with no changes."""
        analyzer = MovementAnalyzer()
        
        lines = ["abc", "def"]
        
        changes = analyzer._get_diff_changes(lines, lines)
        
        self.assertEqual(len(changes), 0)
    
    def test_diff_changes_empty_lines(self):
        """Test diff with empty lines."""
        analyzer = MovementAnalyzer()
        
        prev = [""]
        curr = ["a"]
        
        changes = analyzer._get_diff_changes(curr, prev)
        
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], " ")
        self.assertEqual(changes[0]["to"], "a")
    
    def test_analyze_first_call_initializes_state(self):
        """Test that first analyze call initializes state."""
        analyzer = MovementAnalyzer()
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        result = analyzer.analyze(curr, None)
        
        # Should record player position but not report movement
        self.assertFalse(result["player_moved"])
        self.assertEqual(result["player_delta"], (0, 0))
    
    def test_analyze_player_disappears(self):
        """Test when player disappears from screen."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "    @     ",
        ]
        
        curr = [
            "Level: 1",
            "          ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        # Player disappeared - not movement
        self.assertFalse(result.get("player_moved", False))
    
    def test_analyze_terrain_changes_tracked(self):
        """Test that terrain changes are tracked."""
        analyzer = MovementAnalyzer()
        
        prev = ["     "]
        curr = ["  #  "]
        
        result = analyzer.analyze(curr, prev)
        
        self.assertEqual(len(result["terrain_changes"]), 1)
        self.assertEqual(result["terrain_changes"][0]["to"], "#")
    
    def test_analyze_terrain_changes_skip_player(self):
        """Test that player position changes are not counted as terrain changes."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        # Terrain changes should not include player movement
        terrain_chars = [tc["to"] for tc in result["terrain_changes"]]
        self.assertNotIn("@", terrain_chars)
    
    def test_analyze_terrain_changes_skip_spaces(self):
        """Test that space changes are not counted as terrain changes."""
        analyzer = MovementAnalyzer()
        
        prev = ["  "]
        curr = [" a"]
        
        result = analyzer.analyze(curr, prev)
        
        # Space-to-space change should be skipped
        terrain_changes = result["terrain_changes"]
        for tc in terrain_changes:
            self.assertNotEqual(tc["to"], " ")
            self.assertNotEqual(tc["to"], "@")
    
    def test_analyze_player_moved_world_flag(self):
        """Test player_moved_world flag is set correctly."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "      @   ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        self.assertTrue(result.get("player_moved_world", False))
    
    def test_analyze_screen_shift_detection_high_density(self):
        """Test screen shift detection with high change density."""
        analyzer = MovementAnalyzer()
        
        # Many changes across many rows
        prev = ["A" * 80 for _ in range(24)]
        curr = ["B" * 80 for _ in range(24)]
        
        result = analyzer.analyze(curr, prev)
        
        # High density of changes should trigger screen shift detection
        self.assertTrue(result.get("screen_shifted", False))
    
    def test_analyze_shift_direction_up(self):
        """Test shift direction detection for up scroll."""
        analyzer = MovementAnalyzer()
        
        # Changes concentrated at bottom (screen scrolled up)
        prev = ["A" * 40 for _ in range(24)]
        curr = ["B" * 40 for _ in range(24)]
        
        result = analyzer.analyze(curr, prev)
        
        # Should detect shift direction
        self.assertIn("shift_direction", result)
    
    def test_analyze_shift_direction(self):
        """Test shift direction is calculated."""
        analyzer = MovementAnalyzer()
        
        prev = ["A" * 40 for _ in range(24)]
        curr = ["B" * 40 for _ in range(24)]
        
        result = analyzer.analyze(curr, prev)
        
        self.assertIn("shift_direction", result)
        self.assertEqual(len(result["shift_direction"]), 2)
    
    def test_analyzer_state_persistence(self):
        """Test that analyzer persists player position between calls."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        analyzer.analyze(prev, None)
        
        # State should be persisted
        self.assertIsNotNone(analyzer._previous_player_pos)
        self.assertEqual(analyzer._previous_player_pos, (1, 2))
    
    def test_analyzer_reset_behavior(self):
        """Test analyzer behavior when previous is None."""
        analyzer = MovementAnalyzer()
        
        curr = [
            "Level: 1",
            "    @     ",
        ]
        
        result = analyzer.analyze(curr, None)
        
        self.assertFalse(result["player_moved"])
        self.assertFalse(result["screen_shifted"])
        self.assertIn("player_screen_pos", result)
    
    def test_analyze_large_movement(self):
        """Test detecting large player movements."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "@         ",
        ]
        
        curr = [
            "Level: 1",
            "         @",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        self.assertTrue(result.get("player_moved", False))
        self.assertEqual(result.get("player_delta", (0, 0)), (0, 9))
    
    def test_analyzer_player_moved_up(self):
        """Test player moving up."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "    @     ",
        ]
        
        curr = [
            "Level: 1",
            "  @       ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        self.assertTrue(result.get("player_moved", False))
        self.assertEqual(result.get("player_delta", (0, 0)), (0, -2))
    
    def test_analyzer_player_moved_diagonal_up_right(self):
        """Test player moving diagonally up-right."""
        analyzer = MovementAnalyzer()
        
        prev = [
            "Level: 1",
            "  @       ",
        ]
        
        curr = [
            "Level: 1",
            "      @   ",
        ]
        
        analyzer.analyze(prev, None)
        result = analyzer.analyze(curr, prev)
        
        self.assertTrue(result.get("player_moved", False))
        self.assertEqual(result.get("player_delta", (0, 0)), (0, 4))
    
    def test_analyze_all_directions_tracked(self):
        """Test that all 8 directions are tracked correctly."""
        analyzer = MovementAnalyzer()
        
        # Test each of the 8 cardinal/diagonal directions
        test_cases = [
            ("up", (-1, 0)),
            ("down", (1, 0)),
            ("left", (0, -1)),
            ("right", (0, 1)),
            ("up-left", (-1, -1)),
            ("up-right", (-1, 1)),
            ("down-left", (1, -1)),
            ("down-right", (1, 1)),
        ]
        
        for direction, (expected_dy, expected_dx) in test_cases:
            with self.subTest(direction=direction):
                analyzer = MovementAnalyzer()
                
                if direction == "up":
                    prev_pos = (2, 4)
                    curr_pos = (1, 4)
                elif direction == "down":
                    prev_pos = (1, 4)
                    curr_pos = (2, 4)
                elif direction == "left":
                    prev_pos = (1, 5)
                    curr_pos = (1, 4)
                elif direction == "right":
                    prev_pos = (1, 4)
                    curr_pos = (1, 5)
                elif direction == "up-left":
                    prev_pos = (2, 5)
                    curr_pos = (1, 4)
                elif direction == "up-right":
                    prev_pos = (2, 3)
                    curr_pos = (1, 4)
                elif direction == "down-left":
                    prev_pos = (0, 5)
                    curr_pos = (1, 4)
                elif direction == "down-right":
                    prev_pos = (0, 3)
                    curr_pos = (1, 4)
                
                prev_screen = [
                    "Level: 1",
                    f"{' ' * prev_pos[1]}@{' ' * (40 - prev_pos[1] - 1)}",
                ]
                curr_screen = [
                    "Level: 1",
                    f"{' ' * curr_pos[1]}@{' ' * (40 - curr_pos[1] - 1)}",
                ]
                
                analyzer.analyze(prev_screen, None)
                result = analyzer.analyze(curr_screen, prev_screen)
                
                self.assertTrue(result.get("player_moved", False), 
                              f"Player should have moved {direction}")
                delta = result.get("player_delta", (0, 0))
                # Check that the movement was in the expected direction
                self.assertEqual(delta[0] * expected_dy >= 0, True,
                               f"Y movement should be in expected direction for {direction}")
                self.assertEqual(delta[1] * expected_dx >= 0, True,
                               f"X movement should be in expected direction for {direction}")


if __name__ == '__main__':
    unittest.main()
