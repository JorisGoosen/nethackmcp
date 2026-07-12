# Visual Odometry System - Implementation Summary

## Overview

This system implements a robust visual odometry solution for tracking player position relative to the NetHack dungeon. The key insight is that **player position is always relative to the dungeon**, not the screen.

## Core Principles

1. **World Coordinates**: Player position is tracked in world coordinates `(row, col)` where:
   - `row` increases going south (down)
   - `col` increases going east (right)
   - Both coordinates start at 0 when the player is first observed

2. **Screen vs World**:
   - Screen coordinates show where things appear on the current view
   - World coordinates show the player's actual position in the dungeon
   - These are linked by: `world_position = screen_position - player_screen_position + player_world_position`

3. **Movement Detection**:
   - **Player movement**: Player @ moves on screen, dungeon stays still → world position changes by screen delta
   - **Dungeon shift**: Player @ stays on screen, dungeon terrain moves → dungeon has shifted

## Architecture

### SimpleOdometry Class

The main odometry class (`src/nethackmcp/simple_odometry.py`):

```python
class SimpleOdometry:
    def update(self, screen_lines, player_row, player_col) -> (world_row, world_col, confidence):
        """Update position estimate from new screen."""
    
    def _detect_dungeon_shift(self, screen_lines, player_row, player_col) -> (row_shift, col_shift, confidence):
        """Detect if dungeon has shifted relative to player."""
    
    def _extract_terrain_windows(self, screen_lines, player_row, player_col) -> [(dr, dc, window)]:
        """Extract terrain windows around player for matching."""
    
    def _extract_terrain_memory(self, screen_lines, player_row, player_col):
        """Store terrain windows for later dungeon shift detection."""
```

### Key Methods

1. **`update()`**: Main entry point that processes new screen frames
   - First frame establishes world origin (0, 0)
   - Subsequent frames calculate position change
   - Returns (row, col, confidence) tuple

2. **`_detect_dungeon_shift()`**: Detects if dungeon terrain has shifted
   - Compares current terrain windows with stored memory
   - If match found, dungeon has shifted and world position needs correction
   - Returns shift amount and confidence

3. **`_extract_terrain_memory()`**: Stores terrain patterns for later matching
   - Extracts windows around player at various offsets
   - Stores with world coordinates (relative to player at origin)
   - Used for dungeon shift detection

### Terrain Tracking

The system uses static terrain characters for matching:
- `#` - cave floor
- `.` - tile floor  
- `+` - door
- `-` - horizontal wall
- `|` - vertical wall
- `<` - stairs up
- `>` - stairs down

Dynamic elements (`@`, creatures, items) are excluded from tracking.

## Integration

The odometry system is used by `server.py`:

```python
from nethackmcp.simple_odometry import SimpleOdometry

class NetHackClient:
    def __init__(self):
        self._simple_odom: SimpleOdometry | None = None
    
    def get_state(self):
        # Find player position
        player_row, player_col = self._find_player(screen_lines)
        
        # Update odometry
        if self._simple_odom is None:
            self._simple_odom = SimpleOdometry()
        
        world_row, world_col, confidence = self._simple_odom.update(
            screen_lines, player_row, player_col
        )
        
        # Update map with odometry-derived world position
        map_result = self._update_map_from_screen_odom(
            screen_lines, player_row, player_col, world_row, world_col
        )
```

## Testing

Comprehensive test suite in `tests/test_visual_odometry.py`:

- **Initialization**: Tests default values and reset
- **Player movement**: Tests all directions (up, down, left, right, diagonal)
- **Dungeon detection**: Tests dungeon shift detection and matching
- **Terrain tracking**: Tests window extraction and matching
- **Edge cases**: Handles empty screens, no player, large movements
- **Integration**: Verifies compatibility with DungeonMap

All 43 tests pass.

## Usage

```python
from nethackmcp.simple_odometry import SimpleOdometry

# Create odometry instance
odom = SimpleOdometry()

# Process first frame - establishes world origin
world_row, world_col, conf = odom.update(screen_lines, player_row, player_col)
# Returns (0.0, 0.0, 1.0)

# Process subsequent frames - accumulates movement
world_row, world_col, conf = odom.update(new_screen, new_player_row, new_player_col)
# Returns accumulated world position

# Get current position
row, col = odom.get_position()

# Check confidence
confidence = odom.get_confidence()

# Reset when needed
odom.reset()
```

## Key Design Decisions

1. **Smoothing with exponential moving average**: Prevents jitter from frame-to-frame
2. **Terrain memory matching**: Allows detection of dungeon shifts (e.g., level changes)
3. **Separate screen vs world tracking**: Handles both player movement and dungeon movement
4. **Minimal assumptions**: Works with sparse terrain and dynamic elements
5. **Confidence estimation**: Provides reliability metrics for position estimates
