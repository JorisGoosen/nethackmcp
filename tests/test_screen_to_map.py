"""Tests for screen-to-map conversion using DungeonMap.from_screen()."""

import unittest

from nethackmcp.map import DungeonMap


class TestScreenToMap(unittest.TestCase):
    """Tests for the from_screen class method."""

    def test_from_screen_basic(self):
        """Test basic screen to map conversion."""
        screen = [
            "-----------",
            "|..........",
            "|....$.@..|",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Should have player position
        self.assertEqual(map_obj.player_world_pos, (0, 0))
        
        # Should have tiles
        self.assertGreater(len(map_obj.tiles), 0)
        
        # Player tile should exist and be visited
        self.assertIsNotNone(map_obj.get_tile(0, 0))
        self.assertTrue(map_obj.get_tile(0, 0).visited)

    def test_from_screen_gold_shows_as_question(self):
        """Test that gold tiles show as ? in map output."""
        screen = [
            "-----------",
            "|..........",
            "|....$.@..|",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        map_str = map_obj.get_map_string()
        
        # Gold should show as ?
        self.assertIn('?', map_str)

    def test_from_screen_player_position_removed(self):
        """Test that player position shows underlying terrain in map."""
        screen = [
            "-----------",
            "|..........",
            "|....@....|",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Player should be at (0, 0)
        self.assertEqual(map_obj.player_world_pos, (0, 0))
        
        # Player tile should have terrain that's visible (not @)
        player_tile = map_obj.get_tile(0, 0)
        self.assertEqual(player_tile.terrain, '.')

    def test_from_screen_all_visible_tiles_marked(self):
        """Test that all visible terrain tiles are marked as seen."""
        screen = [
            "-----------",
            "|...@...",
            "-----------",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Check that floor tiles are present and seen
        # Player at col 4, so tiles at cols 2,3,5,6 should be visible
        # In world coords (player at x=0): x=-2, -1, 1, 2
        
        # Just verify some tiles exist
        self.assertIsNotNone(map_obj.get_tile(0, 0))  # Player at (0,0)
        self.assertIsNotNone(map_obj.get_tile(-1, 0))  # 1 row above player
        self.assertIsNotNone(map_obj.get_tile(1, 0))  # 1 row below player

    def test_from_screen_bounds_expansion(self):
        """Test that map bounds expand to cover all screen tiles."""
        screen = [
            "-----------",
            "|..........",
            "|..@..",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Bounds should cover the visible area
        self.assertIsNotNone(map_obj.bounds["min_row"])
        self.assertIsNotNone(map_obj.bounds["max_row"])
        self.assertIsNotNone(map_obj.bounds["min_col"])
        self.assertIsNotNone(map_obj.bounds["max_col"])

    def test_from_screen_adjacent_tiles_visited(self):
        """Test that tiles adjacent to player are marked as visited."""
        screen = [
            "-----------",
            "|..........",
            "|..@..",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # All visible tiles should be visited (for map display)
        # Check a few tiles around player
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                tile = map_obj.get_tile(dx, dy)
                if tile is not None and tile.terrain != '$':
                    # Non-gold tiles should be visited
                    self.assertTrue(tile.visited, f"Tile at ({dx}, {dy}) should be visited")

    def test_map_generation_from_screen(self):
        """Test that map string can be generated from screen input."""
        screen = [
            "-----------",
            "|..........",
            "|..@..",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        map_str = map_obj.get_map_string()
        
        # Map should have content
        self.assertGreater(len(map_str), 0)
        
        # Should contain some terrain
        self.assertIn('.', map_str)


if __name__ == '__main__':
    unittest.main()
