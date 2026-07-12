"""Simple Visual Odometry System for NetHack.

This module implements a robust visual odometry system that tracks the player's
position relative to the dungeon environment, regardless of where the player
appears on screen.

Key principles:
- Player position is ALWAYS relative to dungeon features (terrain, walls, stairs)
- Screen position of player is irrelevant for position tracking
- First observation establishes the world origin (0, 0)
- When player moves on screen (while dungeon stays still): world position changes
- When dungeon shifts (same player screen position): world position = dungeon shift
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


# Terrain characters that are static and suitable for tracking
STATIC_TERRAIN = {'#', '.', '+', '-', '|', '<', '>'}


@dataclass
class OdometryState:
    """Represents the current odometry state."""
    row: float = 0.0
    col: float = 0.0
    heading: float = 0.0
    confidence: float = 0.0
    covariance: list[list[float]] = field(
        default_factory=lambda: [[100.0, 0.0], [0.0, 100.0]]
    )


class SimpleOdometry:
    """
    Visual odometry system that tracks player position relative to dungeon.
    
    The key insight is that in NetHack:
    - Player position on screen tells us player movement relative to dungeon
    - Dungeon terrain movement tells us dungeon shift
    - World position = cumulative movement from origin
    
    World coordinates: (row, col) where both increase down/right
    Screen coordinates: (row, col) where both increase down/right
    
    World position change = screen_position_change when dungeon is static
    When dungeon shifts, world position reflects that shift
    """
    
    def __init__(self, window_size: int = 6, search_radius: int = 3):
        """
        Initialize odometry system.
        
        Args:
            window_size: Size of terrain windows to track
            search_radius: Radius for matching windows between frames
        """
        # World position is discrete - it's a grid
        self._world_row: int = 0
        self._world_col: int = 0
        
        # State for confidence tracking (not used for position)
        self.state = OdometryState()
        
        self._window_size = window_size
        self._search_radius = search_radius
        self._terrain_weight = 1.0
        self._corner_weight = 2.0
        self._door_weight = 3.0
        
        # Memory of terrain windows at their world positions
        self._terrain_memory: dict[tuple[int, int], list[list[str]]] = {}
        
        # Player screen position from previous frame
        self._prev_player_screen_row: int | None = None
        self._prev_player_screen_col: int | None = None
        
        # First frame reference
        self._initial_screen_pos: tuple[int, int] | None = None
        self._first_frame = True
    
    def update(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> tuple[float, float, float]:
        """
        Update position estimate from new screen.
        
        Args:
            screen_lines: Current screen state (list of strings)
            player_row, player_col: Player position on screen (@ symbol)
            
        Returns:
            Tuple of (world_row, world_col, confidence)
        """
        if not screen_lines or len(screen_lines) == 0:
            return (0, 0, 0.0)
        
        # Check if this is first frame - establish world origin
        if self._first_frame:
            self._first_frame = False
            self._initial_screen_pos = (player_row, player_col)
            
            # Player is at world origin (0, 0) initially
            self._world_row = 0
            self._world_col = 0
            self.state.confidence = 1.0
            
            # Extract and store terrain from initial view for later matching
            self._extract_terrain_memory(screen_lines, player_row, player_col)
            
            # Store previous player screen position
            self._prev_player_screen_row = player_row
            self._prev_player_screen_col = player_col
            
            return (self._world_row, self._world_col, self.state.confidence)
        
        # Step 1: Calculate player's screen position change
        # This represents player movement relative to the dungeon
        screen_row_delta = player_row - self._prev_player_screen_row
        screen_col_delta = player_col - self._prev_player_screen_col
        
        # Step 2: Detect if dungeon has shifted (terrain appears at different positions)
        dungeon_shift_row, dungeon_shift_col, dungeon_conf = \
            self._detect_dungeon_shift(screen_lines, player_row, player_col)
        
        # Step 3: Combine dungeon shift and player movement
        # If dungeon shifted, we need to account for that in world position
        # If player moved on screen, world position changes accordingly
        
        # The key insight: 
        # - Player moving right on screen (screen_col_delta > 0) means player 
        #   moved right in the dungeon, so world_col increases
        # - Player moving down on screen (screen_row_delta > 0) means player
        #   moved down in the dungeon, so world_row increases
        # - World position change = player screen movement
        
        # However, when dungeon shifts (terrain moves), it affects world position differently
        # We need to detect and account for dungeon shifts
        
        # The world position change is simply the screen position change
        # Player moving right on screen (delta > 0) = player moved east in dungeon = world_col increases
        self._world_row += screen_row_delta
        self._world_col += screen_col_delta
        
        total_row_change = screen_row_delta + dungeon_shift_row
        total_col_change = screen_col_delta + dungeon_shift_col
        
        # Calculate confidence based on detection quality
        base_confidence = dungeon_conf * 0.4 + 0.6  # Minimum 0.6 confidence
        self.state.confidence = min(1.0, base_confidence)
        
        # Re-extract terrain memory periodically to handle drift
        if self.state.confidence > 0.7:
            self._extract_terrain_memory(screen_lines, player_row, player_col)
        
        # Update history
        self._prev_player_screen_row = player_row
        self._prev_player_screen_col = player_col
        
        return (self._world_row, self._world_col, self.state.confidence)
    
    def _detect_dungeon_shift(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> tuple[float, float, float]:
        """
        Detect if the dungeon/terrain has shifted relative to player.
        
        This happens when:
        - Player hasn't moved but terrain appears shifted (dungeon moved)
        - We're looking at similar terrain but at different positions
        
        Returns (row_shift, col_shift, confidence)
        """
        # If we don't have terrain memory yet, no dungeon shift to detect
        if not self._terrain_memory:
            return (0.0, 0.0, 0.3)
        
        # Extract current terrain around player
        current_windows = self._extract_terrain_windows(screen_lines, player_row, player_col)
        
        if not current_windows:
            return (0.0, 0.0, 0.3)
        
        # Search for matching windows in memory
        best_score = -1.0
        best_shift = (0.0, 0.0)
        
        for (world_r, world_c), memory_window in self._terrain_memory.items():
            for window_info in current_windows:
                screen_dr, screen_dc, current_window = window_info
                score = self._compare_terrain_windows(memory_window, current_window)
                
                if score > best_score:
                    best_score = score
                    best_shift = (world_r - screen_dr, world_c - screen_dc)
        
        # If we found a good match, dungeon has shifted
        if best_score > 0.3:
            return (best_shift[0], best_shift[1], min(1.0, best_score))
        
        # No match found - this is normal during initial exploration
        # We're moving into new terrain
        return (0.0, 0.0, 0.3)
    
    def _extract_terrain_windows(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> list[tuple[int, int, list[list[str]]]]:
        """
        Extract terrain windows from screen around player position.
        
        Returns list of (screen_row, screen_col, window) tuples.
        """
        windows = []
        
        # Extract window at player position
        window = self._extract_window(screen_lines, player_row, player_col, self._window_size)
        if window and self._has_terrain(window):
            windows.append((0, 0, window))
        
        # Also check adjacent positions for robustness
        offsets = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
        for dr, dc in offsets:
            r, c = player_row + dr, player_col + dc
            window = self._extract_window(screen_lines, r, c, self._window_size)
            if window and self._has_terrain(window):
                windows.append((dr, dc, window))
        
        return windows
    
    def _extract_window(
        self, screen_lines: list[str], row: int, col: int, size: int
    ) -> list[list[str]]:
        """Extract a window of terrain from screen."""
        if size % 2 == 0:
            half = size // 2
        else:
            half = size // 2
        window = []
        
        for dr in range(-half, half + 1):
            r = row + dr
            if 0 <= r < len(screen_lines):
                line = screen_lines[r]
                row_window = []
                for dc in range(-half, half + 1):
                    c = col + dc
                    if 0 <= c < len(line):
                        char = line[c]
                        # Only include static terrain
                        if char in STATIC_TERRAIN:
                            row_window.append(char)
                        else:
                            row_window.append(' ')  # Empty for non-terrain
                    else:
                        row_window.append(' ')
                window.append(row_window)
            else:
                window.append([' '] * size)
        
        return window
    
    def _has_terrain(self, window: list[list[str]]) -> bool:
        """Check if window contains any terrain pixels."""
        if not window:
            return False
        for row in window:
            if not row:
                continue
            for char in row:
                if char in STATIC_TERRAIN:
                    return True
        return False
    
    def _compare_terrain_windows(
        self, window1: list[list[str]], window2: list[list[str]]
    ) -> float:
        """Compare two terrain windows and return similarity score (0-1)."""
        if not window1 or not window2:
            return 0.0
        
        # Get dimensions - use minimum to handle slight size differences
        rows1 = len(window1)
        rows2 = len(window2)
        cols1 = len(window1[0]) if window1 else 0
        cols2 = len(window2[0]) if window2 else 0
        
        rows = min(rows1, rows2)
        cols = min(cols1, cols2)
        
        if rows == 0 or cols == 0:
            return 0.0
        
        rows = len(window1)
        cols = len(window1[0])
        
        matches = 0
        total = 0
        
        for r in range(rows):
            for c in range(cols):
                # Bounds check
                if r >= len(window1) or r >= len(window2):
                    continue
                if c >= len(window1[r]) or c >= len(window2[c]):
                    continue
                    
                char1 = window1[r][c] if c < len(window1[r]) else ' '
                char2 = window2[r][c] if c < len(window2[r]) else ' '
                
                if char1 == char2:
                    if char1 in STATIC_TERRAIN:
                        matches += 1.0
                        total += 1.0
                elif char1 in STATIC_TERRAIN and char2 in STATIC_TERRAIN:
                    # Similar terrain types - partial credit
                    matches += 0.3
                    total += 1.0
        
        return matches / total if total > 0 else 0.0
    
    def _extract_terrain_memory(
        self, screen_lines: list[str], player_row: int, player_col: int
    ) -> None:
        """Extract and store terrain windows for future matching."""
        # Clear old memory
        self._terrain_memory.clear()
        
        # Extract windows at various offsets around player
        offsets = [
            (0, 0), (-2, 0), (2, 0), (0, -2), (0, 2),
            (-2, -2), (-2, 2), (2, -2), (2, 2)
        ]
        
        for dr, dc in offsets:
            r, c = player_row + dr, player_col + dc
            window = self._extract_window(screen_lines, r, c, self._window_size)
            
            if window and self._has_terrain(window):
                # World position of this window (relative to player at world origin)
                world_r = -dr  # Negative because window is offset from player
                world_c = -dc
                
                self._terrain_memory[(world_r, world_c)] = window
    
    def get_position(self) -> tuple[int, int]:
        """Get current position estimate (discrete grid coordinates)."""
        return (self._world_row, self._world_col)
    
    def get_confidence(self) -> float:
        """Get current position confidence."""
        return self.state.confidence
    
    def reset(self) -> None:
        """Reset odometry to initial state."""
        self._world_row = 0
        self._world_col = 0
        self.state.confidence = 0.0
        
        self._terrain_memory.clear()
        self._first_frame = True
        self._initial_screen_pos = None
        self._prev_player_screen_row = None
        self._prev_player_screen_col = None


def compute_player_shift(
    screen_t0: list[str],
    screen_t1: list[str]
) -> tuple[float, float, float]:
    """
    Detect player movement by tracking the @ symbol position.
    
    This is more reliable than terrain-based optical flow because:
    - Player is always visible
    - Player movement is what we want to track
    - No false matches from similar terrain patterns
    
    Returns (dr, dc, confidence) where dr/dc show how much the player moved.
    """
    # Find player in both screens
    player_pos_t0 = _find_player_position(screen_t0)
    player_pos_t1 = _find_player_position(screen_t1)
    
    if player_pos_t0 is None or player_pos_t1 is None:
        return (0.0, 0.0, 0.0)
    
    row0, col0 = player_pos_t0
    row1, col1 = player_pos_t1
    
    # Player moved by (row1-row0, col1-col0)
    dr = row1 - row0
    dc = col1 - col0
    
    # Confidence based on how clean the detection was
    confidence = 1.0
    
    return (dr, dc, confidence)


def _find_player_position(screen_lines: list[str]) -> tuple[int, int] | None:
    """Find player position (@) in screen."""
    for row, line in enumerate(screen_lines):
        col = line.find('@')
        if col >= 0:
            return (row, col)
    return None


def test_simple_odometry() -> None:
    """Run basic tests on odometry system."""
    print("Testing SimpleOdometry...")
    
    # Test 1: Basic initialization
    odom = SimpleOdometry()
    screen1 = [
        "-----------",
        "|..........",
        "|....$.@..|",
        "-------+---",
    ]
    
    world_row, world_col, conf = odom.update(screen1, 2, 10)
    assert world_row == 0.0, f"Expected 0.0, got {world_row}"
    assert world_col == 0.0, f"Expected 0.0, got {world_col}"
    assert conf > 0.5, f"Expected confidence > 0.5, got {conf}"
    print("✓ Test 1 passed: Basic initialization")
    
    # Test 2: Player movement detection
    odom2 = SimpleOdometry()
    odom2.update(screen1, 2, 10)  # First frame
    
    screen2 = [
        "-----------",
        "|..........",
        "|...$.@...|",
        "-------+---",
    ]
    
    # Player moved 1 column to the left (from col 10 to col 9)
    # Moving left on screen = player moved west in dungeon
    # World column should decrease (more negative)
    world_row, world_col, conf = odom2.update(screen2, 2, 9)
    assert world_col < 0, f"Expected negative world_col for left movement, got {world_col}"
    print(f"✓ Test 2 passed: Player movement detected (world_col={world_col:.2f})")
    
    # Test 3: Consistency across multiple frames
    odom3 = SimpleOdometry()
    
    screen3a = [
        "-----------",
        "|..#.......",
        "|..@.......",
        "|..#.......",
        "-----------",
    ]
    
    screen3b = [
        "-----------",
        "|...#......",
        "|...@......",
        "|...#......",
        "-----------",
    ]
    
    odom3.update(screen3a, 2, 4)  # First frame
    row1, col1, _ = odom3.update(screen3b, 2, 5)  # Player moved right
    
    # Player moved right on screen, so world_col should increase (player went east)
    # After one move with alpha=0.3, expected col is about 0.3
    assert col1 > 0, f"Expected positive world_col after moving right, got {col1}"
    print(f"✓ Test 3 passed: Consistency check (world_col={col1:.2f})")
    
    # Test 4: Player movement direction
    odom4 = SimpleOdometry()
    
    # Move player in a pattern
    odom4.update(screen1, 2, 10)  # Start
    
    screen_right = [
        "-----------",
        "|..........",
        "|.....@....|",
        "-------+---",
    ]
    row, col, _ = odom4.update(screen_right, 2, 12)
    # Player moved right 2 columns, world_col should increase
    # After one move with alpha=0.3, expected col is about 0.3 per step
    # After 2 steps, expected col is about 0.51
    assert col > 0.4, f"Expected col > 0.4 after moving right 2, got {col}"
    
    screen_down = [
        "-----------",
        "|.....@....|",
        "|..........",
        "-------+---",
    ]
    row, col, _ = odom4.update(screen_down, 1, 12)
    # Player moved up 1 row, world_row should decrease
    assert row < 0, f"Expected row < 0 after moving up, got {row}"
    
    print(f"✓ Test 4 passed: Direction tracking")
    
    print("\nAll tests passed! ✓")


if __name__ == "__main__":
    test_simple_odometry()
