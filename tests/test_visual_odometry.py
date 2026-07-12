"""Comprehensive tests for SimpleOdometry visual odometry system.

Tests are organized by feature:
- Initialization and state management
- Player movement detection
- Dungeon movement/shift detection
- Terrain tracking and matching
- Edge cases and robustness
- Integration with DungeonMap
"""

import unittest
import numpy as np

from nethackmcp.simple_odometry import (
    SimpleOdometry,
    compute_player_shift,
    _find_player_position,
    STATIC_TERRAIN,
)
from nethackmcp.map import DungeonMap


class TestSimpleOdometryInitialization(unittest.TestCase):
    """Tests for odometry initialization and state management."""

    def test_initialization_default_values(self):
        """Test that odometry initializes with correct default values."""
        odom = SimpleOdometry()
        
        self.assertEqual(odom._world_row, 0)
        self.assertEqual(odom._world_col, 0)
        self.assertEqual(odom.state.confidence, 0.0)
        self.assertTrue(odom._first_frame)

    def test_initialization_custom_parameters(self):
        """Test that custom initialization parameters are applied."""
        odom = SimpleOdometry(window_size=8, search_radius=5)
        
        self.assertEqual(odom._window_size, 8)
        self.assertEqual(odom._search_radius, 5)

    def test_reset_clears_state(self):
        """Test that reset clears all odometry state."""
        odom = SimpleOdometry()
        
        # Simulate some movement
        screen = ["@..."]
        odom.update(screen, 0, 0)
        self.assertFalse(odom._first_frame)
        
        # Reset
        odom.reset()
        
        self.assertTrue(odom._first_frame)
        self.assertEqual(odom._world_row, 0)
        self.assertEqual(odom._world_col, 0)
        self.assertEqual(odom._terrain_memory, {})


class TestPlayerMovementDetection(unittest.TestCase):
    """Tests for detecting player movement relative to screen."""

    def test_find_player_basic(self):
        """Test basic player position detection."""
        screen = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        pos = _find_player_position(screen)
        self.assertIsNotNone(pos)
        self.assertEqual(pos, (1, 5))

    def test_find_player_not_found(self):
        """Test that None is returned when player not found."""
        screen = [
            "-----------",
            "|........>|",
            "-----------",
        ]
        
        pos = _find_player_position(screen)
        self.assertIsNone(pos)

    def test_compute_player_shift_movement(self):
        """Test player shift computation when player moves."""
        screen1 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        screen2 = [
            "-----------",
            "|.....@...|",
            "-----------",
        ]
        
        dr, dc, conf = compute_player_shift(screen1, screen2)
        
        self.assertEqual(dr, 0)  # No vertical movement
        self.assertEqual(dc, 1)  # 1 column right
        self.assertEqual(conf, 1.0)

    def test_compute_player_shift_no_movement(self):
        """Test player shift when player doesn't move."""
        screen1 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        screen2 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        dr, dc, conf = compute_player_shift(screen1, screen2)
        
        self.assertEqual(dr, 0)
        self.assertEqual(dc, 0)
        self.assertEqual(conf, 1.0)

    def test_compute_player_shift_player_missing(self):
        """Test player shift when player is missing in one frame."""
        screen1 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        screen2 = [
            "-----------",
            "|........>|",
            "-----------",
        ]
        
        dr, dc, conf = compute_player_shift(screen1, screen2)
        
        self.assertEqual(dr, 0)
        self.assertEqual(dc, 0)
        self.assertEqual(conf, 0.0)

    def test_odometry_first_frame_sets_origin(self):
        """Test that first frame establishes world origin (0, 0)."""
        odom = SimpleOdometry()
        
        screen = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen, 2, 5)
        
        self.assertEqual(row, 0)
        self.assertEqual(col, 0)
        self.assertGreater(conf, 0.5)
        self.assertFalse(odom._first_frame)

    def test_odometry_player_moves_right(self):
        """Test odometry when player moves right on screen."""
        odom = SimpleOdometry()
        
        # First frame - player at column 5
        screen1 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        odom.update(screen1, 1, 5)
        
        # Second frame - player at column 6 (moved right 1)
        screen2 = [
            "-----------",
            "|.....@...|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 6)
        
        # Moving right on screen means player moved east in dungeon
        # So world column should increase by exactly 1 (discrete grid)
        self.assertEqual(col, 1, f"Expected col == 1 for right movement, got {col}")
        self.assertGreater(conf, 0.3)

    def test_odometry_player_moves_left(self):
        """Test odometry when player moves left on screen."""
        odom = SimpleOdometry()
        
        # First frame
        screen1 = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        odom.update(screen1, 1, 5)
        
        # Second frame - player at column 4 (moved left 1)
        screen2 = [
            "-----------",
            "|...@.....|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 4)
        
        # Moving left on screen means player moved west in dungeon
        # So world column should decrease by exactly 1 (discrete grid)
        self.assertEqual(col, -1, f"Expected col == -1 for left movement, got {col}")

    def test_odometry_player_moves_down(self):
        """Test odometry when player moves down on screen."""
        odom = SimpleOdometry()
        
        # First frame
        screen1 = [
            "-----------",
            "|....@....|",
            "|........>|",
            "-----------",
        ]
        odom.update(screen1, 1, 5)
        
        # Second frame - player at row 2 (moved down 1)
        screen2 = [
            "-----------",
            "|........>|",
            "|....@....|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 2, 5)
        
        # Moving down on screen means player moved south in dungeon
        # So world row should increase by exactly 1 (discrete grid)
        self.assertEqual(row, 1, f"Expected row == 1 for down movement, got {row}")

    def test_odometry_player_moves_up(self):
        """Test odometry when player moves up on screen."""
        odom = SimpleOdometry()
        
        # First frame
        screen1 = [
            "-----------",
            "|........>|",
            "|....@....|",
            "-----------",
        ]
        odom.update(screen1, 2, 5)
        
        # Second frame - player at row 1 (moved up 1)
        screen2 = [
            "-----------",
            "|....@....|",
            "|........>|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 5)
        
        # Moving up on screen means player moved north in dungeon
        # So world row should decrease by exactly 1 (discrete grid)
        self.assertEqual(row, -1, f"Expected row == -1 for up movement, got {row}")

    def test_odometry_player_diagonal_movement(self):
        """Test odometry when player moves diagonally."""
        odom = SimpleOdometry()
        
        # First frame - player at (1, 5)
        screen1 = [
            "-----------",
            "|....@....|",
            "|........>|",
            "-----------",
        ]
        odom.update(screen1, 1, 5)
        
        # Second frame - player at (2, 6) (down-right)
        screen2 = [
            "-----------",
            "|........>|",
            "|.....@...|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 2, 6)
        
        # Player moved down 1 row and right 1 column
        # World: row increases by 1 (south), col increases by 1 (east)
        self.assertEqual(row, 1, f"Expected row == 1 for down movement, got {row}")
        self.assertEqual(col, 1, f"Expected col == 1 for right movement, got {col}")


class TestDungeonMovementDetection(unittest.TestCase):
    """Tests for detecting dungeon shifts and environment movement."""

    def test_dungeon_shift_detected(self):
        """Test that dungeon shift is detected when terrain moves."""
        odom = SimpleOdometry()
        
        # First frame with distinctive terrain
        screen1 = [
            "-----------",
            "|..#..@...|",
            "|..#......|",
            "-----------",
        ]
        odom.update(screen1, 1, 7)
        
        # Second frame - terrain shifted but player at same screen position
        # This represents dungeon moving (or looking at different area)
        screen2 = [
            "-----------",
            "|..@......|",
            "|..#......|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 5)
        
        # Should detect some dungeon shift
        self.assertGreater(conf, 0.1, "Should have some confidence in shift detection")

    def test_dungeon_shift_with_terrain_matching(self):
        """Test dungeon shift detection using terrain memory matching."""
        odom = SimpleOdometry()
        
        # First frame - player at screen (2, 10)
        screen1 = [
            "-----------",
            "|..........",
            "|....$.@..|",
            "-------+---",
        ]
        odom.update(screen1, 2, 10)
        
        # Store some terrain memory
        odom._terrain_memory[(0, 0)] = [
            ['.', '.', '$', ' ', ' ', ' '],
            ['.', '.', '.', ' ', ' ', ' '],
            ['@', '.', '.', ' ', ' ', ' '],
            [' ', ' ', ' ', ' ', ' ', ' '],
            [' ', ' ', ' ', ' ', ' ', ' '],
            [' ', ' ', ' ', ' ', ' ', ' '],
        ]
        
        # Second frame - similar terrain at different screen position
        screen2 = [
            "-----------",
            "|..........",
            "|...$.@...|",
            "-------+---",
        ]
        
        # Player moved from col 10 to col 9
        row, col, conf = odom.update(screen2, 2, 9)
        
        # Should handle the movement
        self.assertIsInstance(row, int)
        self.assertIsInstance(col, int)

    def test_dungeon_level_change_detection(self):
        """Test detection of dungeon level changes."""
        odom = SimpleOdometry()
        
        # First frame on level 1
        screen1 = [
            "-----------",
            "|....@....|",
            "|Dlvl:  1   |",
            "-----------",
        ]
        odom.update(screen1, 1, 5)
        
        # Second frame on level 2 (simulated)
        screen2 = [
            "-----------",
            "|....@....|",
            "|Dlvl:  2   |",
            "-----------",
        ]
        
        # Level change should be tracked
        row, col, conf = odom.update(screen2, 1, 5)
        
        # Position should be updated (world stays same since player didn't move)
        self.assertIsInstance(row, int)
        self.assertIsInstance(conf, float)


class TestTerrainTracking(unittest.TestCase):
    """Tests for terrain extraction and matching."""

    def test_extract_terrain_window(self):
        """Test extraction of terrain window from screen."""
        odom = SimpleOdometry(window_size=5)
    
        screen = [
            "-----------",
            "|..#..@...|",
            "|..#......|",
            "-----------",
        ]
    
        # Extract window at a terrain position (1, 2 - at the #)
        window = odom._extract_window(screen, 1, 2, 5)
    
        self.assertEqual(len(window), 5)
        self.assertEqual(len(window[0]), 5)
        # Check that static terrain is preserved
        self.assertIn('#', str(window))

    def test_extract_terrain_window_out_of_bounds(self):
        """Test window extraction at screen boundaries."""
        odom = SimpleOdometry(window_size=5)
        
        screen = [
            "@..",
        ]
        
        # Extract at corner (0, 0)
        window = odom._extract_window(screen, 0, 0, 5)
        
        self.assertEqual(len(window), 5)
        self.assertEqual(len(window[0]), 5)

    def test_has_terrain_empty_window(self):
        """Test has_terrain with empty window."""
        odom = SimpleOdometry()
        empty = [[' ', ' '], [' ', ' ']]
        
        self.assertFalse(odom._has_terrain(empty))

    def test_has_terrain_with_terrain(self):
        """Test has_terrain with window containing terrain."""
        odom = SimpleOdometry()
        window = [['#', '.'], ['+', '|']]
        
        self.assertTrue(odom._has_terrain(window))

    def test_compare_terrain_windows_identical(self):
        """Test comparison of identical windows."""
        odom = SimpleOdometry()
        
        window1 = [['#', '.'], ['+', '|']]
        window2 = [['#', '.'], ['+', '|']]
        
        score = odom._compare_terrain_windows(window1, window2)
        
        self.assertEqual(score, 1.0, "Identical windows should have score 1.0")

    def test_compare_terrain_windows_different(self):
        """Test comparison of different windows."""
        odom = SimpleOdometry()
        
        window1 = [['#', '#'], ['#', '#']]
        window2 = [['.', '.'], ['.', '.']]
        
        score = odom._compare_terrain_windows(window1, window2)
        
        self.assertLess(score, 1.0, "Different windows should have score < 1.0")

    def test_extract_terrain_windows(self):
        """Test extraction of multiple terrain windows."""
        odom = SimpleOdometry(window_size=5)
    
        screen = [
            "-----------",
            "|..#..@...|",
            "|..#......|",
            "-----------",
        ]
    
        windows = odom._extract_terrain_windows(screen, 1, 2)  # At terrain position
    
        # Should extract multiple windows
        self.assertGreater(len(windows), 0)
    
        # Each window should have correct format
        for screen_dr, screen_dc, window in windows:
            self.assertIsInstance(screen_dr, int)
            self.assertIsInstance(screen_dc, int)
            self.assertEqual(len(window), 5)

    def test_extract_terrain_memory(self):
        """Test terrain memory extraction and storage."""
        odom = SimpleOdometry()
        
        screen = [
            "-----------",
            "|..#..@...|",
            "|..#......|",
            "-----------",
        ]
        
        odom._extract_terrain_memory(screen, 1, 7)
        
        # Should have extracted memory
        self.assertGreater(len(odom._terrain_memory), 0)
        
        # Memory should have world positions as keys
        for pos, window in odom._terrain_memory.items():
            self.assertIsInstance(pos, tuple)
            self.assertEqual(len(pos), 2)


class TestEdgeCases(unittest.TestCase):
    """Tests for edge cases and robustness."""

    def test_empty_screen(self):
        """Test odometry with empty screen."""
        odom = SimpleOdometry()
        
        row, col, conf = odom.update([], 0, 0)
        
        # Should handle gracefully
        self.assertIsInstance(row, int)
        self.assertIsInstance(col, int)

    def test_none_screen(self):
        """Test odometry with None screen."""
        odom = SimpleOdometry()
        
        # Should handle gracefully (no crash)
        try:
            row, col, conf = odom.update(None, 0, 0)
        except (TypeError, AttributeError):
            pass  # Expected error

    def test_screen_without_player(self):
        """Test odometry when player not found."""
        odom = SimpleOdometry()
        
        screen = [
            "-----------",
            "|........>|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen, -1, -1)
        
        # Should handle gracefully
        self.assertIsInstance(row, int)

    def test_small_screen(self):
        """Test odometry with very small screen."""
        odom = SimpleOdometry()
        
        screen = ["@."]
        
        row, col, conf = odom.update(screen, 0, 0)
        
        self.assertIsInstance(row, int)
        self.assertIsInstance(conf, float)

    def test_large_movement(self):
        """Test odometry with large player movement."""
        odom = SimpleOdometry()
        
        # First frame
        screen1 = [
            "------------------",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|........@.......|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "------------------",
        ]
        odom.update(screen1, 7, 16)
        
        # Second frame - player moved 8 columns
        screen2 = [
            "------------------",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|................|",
            "|...............@|",
            "|................|",
            "|................|",
            "------------------",
        ]
        
        row, col, conf = odom.update(screen2, 21, 16)
        
        # Should handle large movement
        self.assertIsInstance(row, int)
        self.assertIsInstance(col, int)
        # Player moved down 14 rows (from row 7 to 21), same column
        # Due to smoothing (alpha=0.3), we get about 30% of the movement per frame
        # After first frame, row should be around 4-5
        self.assertGreater(row, 0)

    def test_terrain_only_no_player(self):
        """Test odometry with terrain but no player in history."""
        odom = SimpleOdometry()
        
        # First frame
        screen1 = [
            "-----------",
            "|..#..@...|",
            "|..#......|",
            "-----------",
        ]
        odom.update(screen1, 1, 7)
        
        # Second frame - terrain only
        screen2 = [
            "-----------",
            "|..#......|",
            "|..#......|",
            "-----------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 6)
        
        # Should still produce output
        self.assertIsInstance(row, int)


class TestIntegrationWithMap(unittest.TestCase):
    """Tests for integration with DungeonMap system."""

    def test_odometry_world_coordinates(self):
        """Test that odometry produces world coordinates consistent with map."""
        from nethackmcp.map import DungeonMap
        
        # Create map from screen
        screen = [
            "-----------",
            "|....@....|",
            "-----------",
        ]
        
        map_obj = DungeonMap.from_screen(screen)
        
        # Player should be at world (0, 0)
        self.assertEqual(map_obj.player_world_pos, (0, 0))
        
        # Create odometry and verify same
        odom = SimpleOdometry()
        row, col, _ = odom.update(screen, 1, 5)
        
        # World coordinates should be (0, 0) - discrete grid
        self.assertEqual(row, 0)
        self.assertEqual(col, 0)

    def test_odometry_after_player_move(self):
        """Test odometry world coordinates after player movement."""
        from nethackmcp.map import DungeonMap
        
        # First frame - player at center
        screen1 = [
            "-------------",
            "|...@........|",
            "-------------",
        ]
        
        odom = SimpleOdometry()
        odom.update(screen1, 1, 5)
        
        # Second frame - player moved right
        screen2 = [
            "-------------",
            "|....@.......|",
            "-------------",
        ]
        
        row, col, conf = odom.update(screen2, 1, 6)
        
        # Player moved right on screen, meaning player moved east in dungeon
        # So world column should increase by exactly 1
        self.assertEqual(col, 1, 
            f"Player moving right should increase world column by 1, got {col}")


class TestOdometryAccuracy(unittest.TestCase):
    """Tests for odometry accuracy and precision."""

    def test_position_consistency(self):
        """Test that position is consistent across multiple frames."""
        odom = SimpleOdometry()
        
        # Create a screen with clear terrain features
        screen = [
            "-------------------",
            "|.#.#.#.#.#.#.#.#.|",
            "|.#.@...........#.|",
            "|.#.#.#.#.#.#.#.#.|",
            "|..................|",
            "|..................|",
            "|..................|",
            "-------------------",
        ]
        
        # Process same frame multiple times
        results = []
        for _ in range(5):
            row, col, conf = odom.update(screen, 1, 5)
            results.append((row, col, conf))
        
        # Results should be consistent (within tolerance)
        rows = [r[0] for r in results]
        cols = [r[1] for r in results]
        
        row_std = np.std(rows)
        col_std = np.std(cols)
        
        self.assertLess(row_std, 0.1, "Row should be stable")
        self.assertLess(col_std, 0.1, "Col should be stable")

    def test_confidence_boundaries(self):
        """Test that confidence stays within valid bounds."""
        odom = SimpleOdometry()
        
        screen = ["@..."]
        row, col, conf = odom.update(screen, 0, 0)
        
        self.assertIsInstance(conf, float)
        self.assertGreaterEqual(conf, 0.0)
        self.assertLessEqual(conf, 1.0)

    def test_movement_accumulation(self):
        """Test that movement accumulates correctly over time."""
        odom = SimpleOdometry()
        
        # Simulate 5 steps of continuous movement (all right)
        # Starting position: col 4
        # Each step moves right by 1 column
        
        # Step 1: 4 -> 5
        odom.update([
            "-------------",
            "|...@........|",
            "-------------",
        ], 1, 4)
        row, col, _ = odom.update([
            "-------------",
            "|....@.......|",
            "-------------",
        ], 1, 5)
        
        # Step 2: 5 -> 6
        row, col, _ = odom.update([
            "-------------",
            "|.....@......|",
            "-------------",
        ], 1, 6)
        
        # Step 3: 6 -> 7
        row, col, _ = odom.update([
            "-------------",
            "|......@.....|",
            "-------------",
        ], 1, 7)
        
        # Step 4: 7 -> 8
        row, col, _ = odom.update([
            "-------------",
            "|.......@....|",
            "-------------",
        ], 1, 8)
        
        # Step 5: 8 -> 9
        row, col, _ = odom.update([
            "-------------",
            "|........@...|",
            "-------------",
        ], 1, 9)
        
        # After moving right 5 times (5 steps × 1 column each)
        # World column should be exactly 5 (discrete grid)
        self.assertEqual(col, 5, 
            f"Expected col == 5 after moving right 5 times, got {col}")


class TestDungeonOffset(unittest.TestCase):
    """Tests for find_best_dungeon_offset terrain matching."""

    def _make_screen(self, player_row, player_col, terrain_grid):
        """Build a screen string from a terrain grid. Replaces 'P' with '@'."""
        screen = []
        for r, row_str in enumerate(terrain_grid):
            line = row_str.replace('P', '@') if r == player_row else row_str
            screen.append(line)
        return screen

    def _build_map_from_screen(self, screen, player_row, player_col, dungeon_offset):
        """Build a DungeonMap with tiles at world positions using the given offset."""
        game_map = DungeonMap(level=1)
        game_map.dungeon_offset = dungeon_offset
        game_map.player_world_pos = (0, 0)
        TERRAIN_WHITELIST = {'#', '.', '+', '-', '|', '<', '>'}

        for row in range(len(screen)):
            for col, char in enumerate(screen[row]):
                if char == '@':
                    continue
                if char not in TERRAIN_WHITELIST:
                    continue
                world_row = dungeon_offset[0] + row
                world_col = dungeon_offset[1] + col
                game_map.set_tile(world_row, world_col, char, seen=True, visited=True)

        return game_map

    def test_same_screen_same_offset(self):
        """After first frame, find_best_dungeon_offset must return same offset."""
        screen = [
            "  -----  ",
            "  +.P|  ",
            "  |...  ",
            "  -----  ",
        ]

        player_row, player_col = None, None
        for r, line in enumerate(screen):
            c = line.find('P')
            if c >= 0:
                player_row, player_col = r, c
                break

        # Replace P with @ for the actual screen
        for r in range(len(screen)):
            screen[r] = screen[r].replace('P', '@')

        expected_offset = (-player_row, -player_col)

        game_map = self._build_map_from_screen(screen, player_row, player_col, expected_offset)

        found_offset = game_map.find_best_dungeon_offset(screen, search_range=10)

        self.assertEqual(
            found_offset, expected_offset,
            f"find_best_dungeon_offset returned {found_offset}, expected {expected_offset} "
            f"(player at screen ({player_row}, {player_col}))"
        )

    def test_same_screen_same_offset_from_screen(self):
        """Verify from_screen + find_best_dungeon_offset consistency."""
        screen = [
            "  -----  ",
            "  +.@|  ",
            "  |...  ",
            "  -----  ",
        ]

        player_row, player_col = None, None
        for r, line in enumerate(screen):
            c = line.find('@')
            if c >= 0:
                player_row, player_col = r, c
                break

        expected_offset = (-player_row, -player_col)

        # Use DungeonMap.from_screen which stores tiles correctly
        game_map = DungeonMap.from_screen(screen)

        found_offset = game_map.find_best_dungeon_offset(screen, search_range=10)

        self.assertEqual(
            found_offset, expected_offset,
            f"from_screen + find_best_dungeon_offset returned {found_offset}, "
            f"expected {expected_offset} (player at screen ({player_row}, {player_col}))"
        )

    def test_player_moved_down_one_offset_unchanged(self):
        """Player moves down by 1 within screen. Dungeon doesn't shift, offset stays same."""
        screen1 = [
            "  -----  ",
            "  +.@|  ",
            "  |...  ",
            "  -----  ",
        ]

        screen2 = [
            "  -----  ",
            "  +..|  ",
            "  |.@.  ",
            "  -----  ",
        ]

        p1_row, p1_col = 1, 4
        p2_row, p2_col = 2, 4

        offset1 = (-p1_row, -p1_col)  # (-1, -4)

        game_map = self._build_map_from_screen(screen1, p1_row, p1_col, offset1)

        found_offset2 = game_map.find_best_dungeon_offset(screen2, search_range=10)

        # When player moves within screen, dungeon DOESN'T shift.
        # Offset stays the same. Only player world position changes.
        self.assertEqual(
            found_offset2, offset1,
            f"After moving down within screen: got {found_offset2}, expected {offset1} "
            f"(dungeon didn't move, only player moved within it)"
        )

        # Player world position should update: world = offset + screen_pos
        player_world_row = found_offset2[0] + p2_row
        player_world_col = found_offset2[1] + p2_col
        self.assertEqual(
            (player_world_row, player_world_col), (1, 0),
            f"Player world position wrong: {(player_world_row, player_world_col)}"
        )

    def test_viewport_scrolled_down_player_moved_down(self):
        """Screen 1 -> Screen 2: dungeon scrolled DOWN, player moved DOWN."""
        # Screen 1: 4 rows, player at (1,2)
        screen1 = [
            "-----",
            "|.@.|",
            "|...|",
            "--.--",
        ]
        # Screen 2: 5 rows (first empty), dungeon shifted down, player at (3,2)
        screen2 = [
            "",
            "-----",
            "|...|",
            "|.@.|",
            "--.--",
        ]

        p1_row, p1_col = 1, 2
        p2_row, p2_col = 3, 2

        offset1 = (-p1_row, -p1_col)  # (-1, -2)
        game_map = self._build_map_from_screen(screen1, p1_row, p1_col, offset1)

        found_offset2 = game_map.find_best_dungeon_offset(screen2, search_range=10)
        expected_offset2 = (-2, -2)  # dungeon scrolled down by 1

        self.assertEqual(found_offset2, expected_offset2,
                         f"Scroll down: got {found_offset2}, expected {expected_offset2}")

        player_world_row = found_offset2[0] + p2_row
        player_world_col = found_offset2[1] + p2_col
        self.assertEqual((player_world_row, player_world_col), (1, 0),
                         f"Player world: ({player_world_row}, {player_world_col}), expected (1, 0)")

    def test_viewport_scrolled_right_player_moved_left(self):
        """Screen 2 -> Screen 3: dungeon scrolled RIGHT, player moved LEFT."""
        screen2 = [
            "",
            "-----",
            "|...|",
            "|.@.|",
            "--.--",
        ]
        screen3 = [
            " ",
            " -----",
            " |...|",
            " |@..|",
            " --.--",
        ]

        p2_row, p2_col = 3, 2
        p3_row, p3_col = 3, 2

        offset2 = (-2, -2)  # from previous frame
        game_map = self._build_map_from_screen(screen2, p2_row, p2_col, offset2)

        found_offset3 = game_map.find_best_dungeon_offset(screen3, search_range=10)
        expected_offset3 = (-2, -3)  # dungeon scrolled right by 1

        self.assertEqual(found_offset3, expected_offset3,
                         f"Scroll right: got {found_offset3}, expected {expected_offset3}")

        player_world_row = found_offset3[0] + p3_row
        player_world_col = found_offset3[1] + p3_col
        self.assertEqual((player_world_row, player_world_col), (1, -1),
                         f"Player world: ({player_world_row}, {player_world_col}), expected (1, -1)")


if __name__ == '__main__':
    unittest.main()
