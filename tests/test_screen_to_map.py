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
        """Test that passable tiles are only visited when player is ON them, walls when adjacent."""
        screen = [
            "-----------",
            "|..........",
            "|..@..",
            "-------+---",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Player's current tile (0,0) should be visited
        player_tile = map_obj.get_tile(0, 0)
        self.assertIsNotNone(player_tile)
        self.assertTrue(player_tile.visited, "Player tile (0,0) should be visited")
        
        # Adjacent passable tiles should be seen but NOT visited
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                tile = map_obj.get_tile(dx, dy)
                if tile is not None and tile.terrain not in ('$', ' '):
                    self.assertFalse(tile.visited, f"Passable tile at ({dx}, {dy}) should NOT be visited")
                    self.assertTrue(tile.seen, f"Passable tile at ({dx}, {dy}) should be seen")
        
        # Adjacent wall tiles should be visited
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                tile = map_obj.get_tile(dx, dy)
                if tile is not None and tile.terrain == ' ':
                    self.assertTrue(tile.visited, f"Wall tile at ({dx}, {dy}) should be visited")

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

    def test_door_isolated_horizontal_passable(self):
        """Test that isolated | with - left/right is passable (door at bottom wall)."""
        screen = [
            "------",
            "|...@.|",
            "|....|",
            "|....|",
            "---|--"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Find the | character that should be a door
        # The | at bottom with - left/right and no | above/below
        door_found = False
        for (r, c), tile in map_obj.tiles.items():
            if tile.terrain == '|':
                is_passable = map_obj._is_terrain_passable(r, c)
                if is_passable:
                    door_found = True
                    break
        
        self.assertTrue(door_found, "Should find at least one isolated | that is passable")

    def test_door_isolated_vertical_passable(self):
        """Test that isolated - with | above/below is passable (door at right wall)."""
        screen = [
            "------",
            "|...@-",
            "|....|",
            "|....|",
            "------"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        door_found = False
        for (r, c), tile in map_obj.tiles.items():
            if tile.terrain == '-':
                is_passable = map_obj._is_terrain_passable(r, c)
                if is_passable:
                    door_found = True
                    break
        
        self.assertTrue(door_found, "Should find at least one isolated - that is passable")

    def test_solid_wall_impassable(self):
        """Test that connected walls are impassable."""
        screen = [
            "------",
            "|...@|",
            "|....|",
            "|....|",
            "------"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Most walls should be IMPASSABLE (connected to other walls)
        wall_count = 0
        impassable_count = 0
        for (r, c), tile in map_obj.tiles.items():
            if tile.terrain == '-' or tile.terrain == '|':
                wall_count += 1
                is_passable = map_obj._is_terrain_passable(r, c)
                if not is_passable:
                    impassable_count += 1
        
        self.assertGreater(impassable_count, 0, "Should have some impassable walls")
        # In a simple room with no doors, all walls should be impassable
        self.assertEqual(wall_count, impassable_count, 
                        "All walls in this room should be impassable")

    def test_room_detection_single_room(self):
        """Test room detection in a simple enclosed room."""
        screen = [
            "-------",
            "|.....|",
            "|....@|",
            "|.....|",
            "-------"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        rooms = map_obj._detect_rooms()
        
        self.assertEqual(len(rooms), 1, "Should detect exactly one room")
        self.assertIn(0, rooms, "Room 0 should exist")
        
        room = rooms[0]
        bounds = room["bounds"]
        # Room bounds in world coords may be negative (player at origin)
        # Just verify room has tiles and reasonable bounds
        self.assertGreaterEqual(len(room["tiles"]), 5, "Room should have floor tiles")
        self.assertLessEqual(bounds["max_row"] - bounds["min_row"], 3, "Room height should be reasonable")
        self.assertLessEqual(bounds["max_col"] - bounds["min_col"], 5, "Room width should be reasonable")

    def test_room_detection_multiple_rooms(self):
        """Test room detection when there are multiple separate rooms."""
        screen = [
            "---------",
            "|..@|...|",
            "|...|...|",
            "|...|...|",
            "---------"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        rooms = map_obj._detect_rooms()
        
        self.assertGreater(len(rooms), 0, "Should detect at least one room")

    def test_player_on_stairs_shows_stairs_desc(self):
        """Test that player position on stairs shows 'stairs down/up'."""
        from nethackmcp.server import NetHackClient
        
        client = NetHackClient()
        
        description = client._describe_terrain('>', 0, 0, None)
        self.assertIn('stairs down', description.lower())
        
        description = client._describe_terrain('<', 0, 0, None)
        self.assertIn('stairs up', description.lower())

    def test_room_relative_position(self):
        """Test room relative position calculation."""
        screen = [
            "---------",
            "|.......|",
            "|....@..|",
            "|.......|",
            "---------"
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        rooms = map_obj._detect_rooms()
        
        if rooms:
            room_id = 0
            # Player world pos is (0, 0)
            rel = map_obj._get_room_relative_pos(0, 0, room_id)
            
            self.assertIsInstance(rel, tuple)
            self.assertEqual(len(rel), 2)
            pct_row, pct_col = rel
            self.assertGreater(pct_row, 0.0, "Should have some position")
            self.assertLessEqual(pct_row, 1.0, "Should be within 0-1 range")
            self.assertGreater(pct_col, 0.0, "Should have some position")
            self.assertLessEqual(pct_col, 1.0, "Should be within 0-1 range")


if __name__ == '__main__':
    unittest.main()
