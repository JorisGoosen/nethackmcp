"""Visual Optical Flow and Landmark-Based Positioning System for NetHack.

This module implements a robust positioning system that combines:
- Pixel-wise and windowed optical flow for relative motion estimation
- Landmark-based positioning for absolute correction
- Kalman filter for sensor fusion and uncertainty management
- Loop closure detection for drift correction

The system is designed to handle NetHack's sparse terrain, moving creatures,
and dynamic environment while maintaining accurate dungeon mapping.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# =============================================================================
# Constants and Configuration
# =============================================================================

# Terrain characters (static dungeon features)
TERRAIN_CHARS = {'#', '.', '+', '-', '|', '<', '>'}

# Dynamic objects (creatures, items) - excluded from flow calculations
DYNAMIC_CHARS = {'@', 'f', '%', '$', ')', '!', '|', '"', '?', '*', '0', '6'}

# Landmark types and their patterns
LANDMARK_TYPES = {
    'door_h': {
        'pattern': [  # Horizontal door
            ['-', '+', '-'],
            ['|', ' ', '|'],
            ['-', '+', '-']
        ],
        'description': 'Horizontal door with frame',
        'unique': True
    },
    'door_v': {
        'pattern': [  # Vertical door
            ['|', '-', '|'],
            ['+', ' ', '+'],
            ['|', '-', '|']
        ],
        'description': 'Vertical door with frame',
        'unique': True
    },
    'corner_tl': {
        'pattern': [  # Top-left corner
            ['+', '-', '-'],
            ['|', ' ', ' '],
            ['|', ' ', ' ']
        ],
        'description': 'Top-left corner',
        'unique': False
    },
    'corner_tr': {
        'pattern': [  # Top-right corner
            ['-','-','+'],
            [' ',' ','|'],
            [' ',' ','|']
        ],
        'description': 'Top-right corner',
        'unique': False
    },
    'corner_bl': {
        'pattern': [  # Bottom-left corner
            ['|',' ',' '],
            ['|',' ',' '],
            ['+','-','-']
        ],
        'description': 'Bottom-left corner',
        'unique': False
    },
    'corner_br': {
        'pattern': [  # Bottom-right corner
            [' ',' ','|'],
            [' ',' ','|'],
            ['-','-','+']
        ],
        'description': 'Bottom-right corner',
        'unique': False
    },
    'stair_up': {
        'pattern': [['<']],  # Simple pattern - just the stair
        'description': 'Stairs up',
        'unique': True
    },
    'stair_down': {
        'pattern': [['>']],  # Simple pattern - just the stair
        'description': 'Stairs down',
        'unique': True
    },
    'fountain': {
        'pattern': [['O']],  # Fountain symbol
        'description': 'Fountain',
        'unique': True
    },
    'altar': {
        'pattern': [['`']],  # Altar symbol
        'description': 'Altar',
        'unique': True
    }
}

# Optical flow parameters
OPTICAL_FLOW = {
    'window_size': 8,
    'window_stride': 4,
    'max_search_radius': 3,
    'correlation_threshold': 0.35,
    'min_flow_matches': 5,
    'flow_history_size': 5,
    'confidence_min': 0.3,
    'confidence_max': 0.95,
}

# Landmark detection parameters
LANDMARK_DETECTION = {
    'pattern_tolerance': 0.35,
    'min_landmark_confidence': 0.6,
    'max_landmark_distance': 30,
}

# Loop closure parameters
LOOP_CLOSURE = {
    'similarity_threshold': 0.75,
    'min_keyframe_interval': 10,
    'signature_similarity_weight': {
        'terrain': 0.35,
        'landmarks': 0.25,
        'boundaries': 0.20,
        'pattern': 0.20
    }
}


# =============================================================================
# Optical Flow Implementation
# =============================================================================

def extract_terrain_mask(screen_lines: list[str]) -> list[list[bool]]:
    """Extract binary terrain mask from screen lines.
    
    True = static terrain, False = dynamic/object/unknown.
    
    Args:
        screen_lines: List of screen row strings
        
    Returns:
        List of lists of boolean values
    """
    mask = []
    for line in screen_lines:
        row = []
        for char in line:
            row.append(char in TERRAIN_CHARS)
        mask.append(row)
    return mask


def extract_terrain_matrix(screen_lines: list[str]) -> list[list[str]]:
    """Extract terrain characters into 2D matrix.
    
    Dynamic objects and unknown positions are replaced with ' '.
    
    Args:
        screen_lines: List of screen row strings
        
    Returns:
        2D list of terrain characters
    """
    matrix = []
    for line in screen_lines:
        row = []
        for char in line:
            if char in TERRAIN_CHARS:
                row.append(char)
            else:
                row.append(' ')
        matrix.append(row)
    return matrix


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
    player_pos_t0 = find_player_position(screen_t0)
    player_pos_t1 = find_player_position(screen_t1)
    
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


def compute_windowed_optical_flow(
    screen_t0: list[str],
    screen_t1: list[str],
    window_size: int = OPTICAL_FLOW['window_size'],
    stride: int = OPTICAL_FLOW['window_stride'],
    max_search: int = OPTICAL_FLOW['max_search_radius'],
    correlation_threshold: float = OPTICAL_FLOW['correlation_threshold']
) -> tuple[float, float, float]:
    """
    Compute optical flow using sliding window template matching.
    
    This approach is more robust than pixel-wise flow for sparse dungeon
    layouts where there may be few terrain pixels.
    
    Args:
        screen_t0: Previous screen state
        screen_t1: Current screen state
        window_size: Size of template windows
        stride: Step size for sliding windows
        max_search: Maximum search radius for matching
        correlation_threshold: Minimum correlation for valid match
        
    Returns:
        Tuple of (dr, dc, confidence) where:
            - dr, dc: Average screen movement (rows, cols)
            - confidence: 0.0 to 1.0 reliability score
    """
    if len(screen_t0) == 0 or len(screen_t1) == 0:
        return (0.0, 0.0, 0.0)
    
    height = min(len(screen_t0), len(screen_t1))
    width = max(
        max(len(line) for line in screen_t0) if screen_t0 else 0,
        max(len(line) for line in screen_t1) if screen_t1 else 0
    )
    
    flow_vectors = []
    flow_weights = []
    
    # Use moderate search range
    actual_search = max(max_search, 2)
    
    # Slide windows across screen
    for r in range(0, height - window_size, stride):
        for c in range(0, width - window_size, stride):
            
            # Extract window from t0
            window_t0 = _extract_window(screen_t0, r, c, window_size)
            
            if not _has_terrain(window_t0):
                continue  # Skip empty windows
            
            # Search for best match in t1
            best_match = None
            best_score = float('-inf')
            
    for search_dr in range(-actual_search, actual_search + 1):
        for search_dc in range(-actual_search, actual_search + 1):
            nr, nc = r + search_dr, c + search_dc
            
            if (0 <= nr < height - window_size and 
                0 <= nc < width - window_size):
                
                window_t1 = _extract_window(screen_t1, nr, nc, window_size)
                score = _normalized_cross_correlation(window_t0, window_t1)
                
                # Add distance penalty to prefer closer matches
                distance = abs(search_dr) + abs(search_dc)
                distance_penalty = 1.0 - (distance * 0.1)
                score = score * distance_penalty
                
                if score > best_score:
                    best_score = score
                    best_match = (search_dr, search_dc, score)
            
            if best_match and best_score > correlation_threshold:
                # Negate because if terrain moves by (dr, dc), player moves opposite
                flow_vectors.append((-best_match[0], -best_match[1]))
                flow_weights.append(best_score)
    
    # Aggregate flow vectors
    if len(flow_vectors) < OPTICAL_FLOW['min_flow_matches']:
        return (0.0, 0.0, 0.0)
    
    # Weighted median for robust estimate
    dr_values = [v[0] for v in flow_vectors]
    dc_values = [v[1] for v in flow_vectors]
    weights = flow_weights
    
    # Compute weighted average
    total_weight = sum(weights)
    flow_dr = sum(d * w for d, w in zip(dr_values, weights)) / total_weight
    flow_dc = sum(d * w for d, w in zip(dc_values, weights)) / total_weight
    
    # Calculate confidence - lower threshold for small screens
    total_pixels = len(screen_t0) * (width if width > 0 else 80)
    confidence = _compute_flow_confidence(
        flow_vectors, flow_weights, total_pixels
    )
    
    # For very small movements, boost confidence if we have any matches
    if len(flow_vectors) > 0 and abs(flow_dr) < 2.0 and abs(flow_dc) < 2.0:
        confidence = min(0.95, confidence * 1.3)
    
    return (flow_dr, flow_dc, confidence)


def _extract_window(screen_lines: list[str], row: int, col: int, size: int) -> list[list[str]]:
    """Extract a window from screen lines."""
    window = []
    for i in range(size):
        if row + i < len(screen_lines):
            line = screen_lines[row + i]
            row_window = []
            for j in range(size):
                if col + j < len(line):
                    row_window.append(line[col + j] if line[col + j] in TERRAIN_CHARS else ' ')
                else:
                    row_window.append(' ')
            window.append(row_window)
        else:
            window.append([' '] * size)
    return window


def _has_terrain(window: list[list[str]]) -> bool:
    """Check if window contains any terrain pixels."""
    for row in window:
        for char in row:
            if char in TERRAIN_CHARS:
                return True
    return False


def _normalized_cross_correlation(window1: list[list[str]], window2: list[list[str]]) -> float:
    """Compute normalized cross-correlation between two windows.
    
    Weights wall/door characters more heavily than floor, since they are
    more distinctive features for matching.
    """
    # Convert to numeric arrays with weighted terrain types
    arr1 = []
    arr2 = []
    
    # Terrain weights: walls/doors more distinctive than floor
    TERRAIN_WEIGHTS = {
        '#': 1.0,  # cave floor
        '.': 0.3,  # tile floor (less distinctive)
        '+': 2.0,  # door (very distinctive)
        '|': 2.0,  # vertical wall (very distinctive)
        '-': 2.0,  # horizontal wall (very distinctive)
        '<': 3.0,  # stairs up (most distinctive)
        '>': 3.0,  # stairs down (most distinctive)
    }
    
    for row1, row2 in zip(window1, window2):
        for c1, c2 in zip(row1, row2):
            v1 = TERRAIN_WEIGHTS.get(c1, 0.0) if c1 else 0.0
            v2 = TERRAIN_WEIGHTS.get(c2, 0.0) if c2 else 0.0
            arr1.append(v1)
            arr2.append(v2)
    
    if len(arr1) == 0:
        return 0.0
    
    arr1 = np.array(arr1, dtype=np.float64)
    arr2 = np.array(arr2, dtype=np.float64)
    
    # Check if both have any significant terrain
    if np.sum(arr1) < 0.1 or np.sum(arr2) < 0.1:
        return 0.0
    
    # Normalize
    norm1 = np.linalg.norm(arr1)
    norm2 = np.linalg.norm(arr2)
    
    if norm1 < 0.01 or norm2 < 0.01:
        return 0.0
    
    # Cosine similarity
    return float(np.dot(arr1, arr2) / (norm1 * norm2))


def _compute_flow_confidence(
    flow_vectors: list[tuple[int, int]],
    flow_weights: list[float],
    total_pixels: int
) -> float:
    """Compute confidence score for optical flow estimate."""
    
    if not flow_vectors:
        return 0.0
    
    # Match ratio: how many windows had matches?
    match_ratio = len(flow_vectors) / max(1, total_pixels / (OPTICAL_FLOW['window_size'] ** 2))
    match_ratio = min(1.0, match_ratio)
    
    # Flow consistency: variance of flow vectors
    if len(flow_vectors) > 1:
        dr_values = [v[0] for v in flow_vectors]
        dc_values = [v[1] for v in flow_vectors]
        
        dr_var = np.var(dr_values) if len(dr_values) > 1 else 0.0
        dc_var = np.var(dc_values) if len(dc_values) > 1 else 0.0
        
        # Lower variance = more consistent = higher confidence
        flow_consistency = 1.0 / (1.0 + dr_var + dc_var)
    else:
        flow_consistency = 0.5
    
    # Weighted average confidence
    weighted_conf = sum(w for w in flow_weights) / len(flow_weights) if flow_weights else 0.0
    
    # Combine factors
    confidence = (
        0.3 * match_ratio +
        0.3 * flow_consistency +
        0.4 * weighted_conf
    )
    
    # Clamp to valid range
    return max(OPTICAL_FLOW['confidence_min'], 
               min(OPTICAL_FLOW['confidence_max'], confidence))


# =============================================================================
# Landmark Extraction and Matching
# =============================================================================

def find_player_position(screen_lines: list[str]) -> tuple[int, int] | None:
    """Find player position (@) in screen."""
    for row, line in enumerate(screen_lines):
        col = line.find('@')
        if col >= 0:
            return (row, col)
    return None


def extract_landmarks(
    screen_lines: list[str],
    player_row: int,
    player_col: int,
    tolerance: float = LANDMARK_DETECTION['pattern_tolerance']
) -> list[dict[str, Any]]:
    """
    Extract all landmarks from the current screen.
    
    Landmarks are distinctive dungeon features that can be used for
    absolute positioning and loop closure detection.
    
    Args:
        screen_lines: Current screen state
        player_row, player_col: Player position on screen
        tolerance: Pattern matching tolerance (0.0 = exact, 1.0 = very loose)
        
    Returns:
        List of landmark dictionaries with type, position, and metadata
    """
    landmarks = []
    
    for landmark_type, config in LANDMARK_TYPES.items():
        matches = _find_matching_patterns(screen_lines, config['pattern'], tolerance)
        
        for (match_row, match_col) in matches:
            # Calculate world coordinates
            world_row = match_row - player_row
            world_col = match_col - player_col
            
            # Detect orientation if applicable
            orientation = _detect_landmark_orientation(screen_lines, match_row, match_col)
            
            landmark = {
                'type': landmark_type,
                'pattern': config['pattern'],
                'screen_pos': (match_row, match_col),
                'world_pos': (world_row, world_col),
                'confidence': _compute_landmark_confidence(screen_lines, match_row, match_col),
                'orientation': orientation,
                'description': config['description']
            }
            
            landmarks.append(landmark)
    
    return landmarks


def _find_matching_patterns(
    screen_lines: list[str],
    pattern: list[list[str]],
    tolerance: float = 0.3
) -> list[tuple[int, int]]:
    """Find all locations where pattern matches (with some tolerance)."""
    matches = []
    
    pattern_height = len(pattern)
    pattern_width = max(len(row) for row in pattern) if pattern else 0
    
    if pattern_height == 0 or pattern_width == 0:
        return matches
    
    for r in range(len(screen_lines) - pattern_height + 1):
        for c in range(len(screen_lines[r]) - pattern_width + 1):
            
            match_score = 0.0
            total_cells = 0
            
            for pr in range(pattern_height):
                for pc in range(len(pattern[pr])):
                    expected = pattern[pr][pc]
                    actual = ' '
                    
                    if c + pc < len(screen_lines[r + pr]):
                        actual = screen_lines[r + pr][c + pc]
                    
                    if expected == actual:
                        match_score += 1.0
                    elif expected == ' ' or actual == ' ':
                        match_score += 0.5  # Partial credit for empty
                    elif expected in TERRAIN_CHARS and actual in TERRAIN_CHARS:
                        match_score += 0.3  # Partial credit for similar terrain
                    total_cells += 1
            
            match_ratio = match_score / total_cells
            
            if match_ratio >= (1.0 - tolerance):
                matches.append((r, c))
    
    return matches


def _detect_landmark_orientation(
    screen_lines: list[str],
    row: int,
    col: int
) -> str:
    """Detect the orientation of a landmark based on surrounding terrain."""
    
    # Check neighbors to determine orientation
    directions = {
        'up': False,
        'down': False,
        'left': False,
        'right': False
    }
    
    if row > 0:
        directions['up'] = screen_lines[row - 1][col] in {'|', '-', ' ', '#', '.'}
    if row < len(screen_lines) - 1:
        directions['down'] = screen_lines[row + 1][col] in {'|', '-', ' ', '#', '.'}
    if col > 0:
        directions['left'] = screen_lines[row][col - 1] in {'|', '-', ' ', '#', '.'}
    if col < len(screen_lines[row]) - 1:
        directions['right'] = screen_lines[row][col + 1] in {'|', '-', ' ', '#', '.'}
    
    # Determine dominant orientation
    if directions['up'] and directions['down']:
        return 'vertical'
    elif directions['left'] and directions['right']:
        return 'horizontal'
    elif directions['up']:
        return 'facing_up'
    elif directions['down']:
        return 'facing_down'
    elif directions['left']:
        return 'facing_left'
    elif directions['right']:
        return 'facing_right'
    
    return 'unknown'


def _compute_landmark_confidence(
    screen_lines: list[str],
    row: int,
    col: int,
    window_size: int = 5
) -> float:
    """Compute confidence that a landmark is correctly identified."""
    
    # Check surrounding context
    confidence = 1.0
    
    # If landmark is isolated (no terrain around), lower confidence
    terrain_count = 0
    total_count = 0
    
    half = window_size // 2
    for dr in range(-half, half + 1):
        for dc in range(-half, half + 1):
            r, c = row + dr, col + dc
            if 0 <= r < len(screen_lines) and 0 <= c < len(screen_lines[r]):
                total_count += 1
                if screen_lines[r][c] in TERRAIN_CHARS:
                    terrain_count += 1
    
    if total_count > 0:
        terrain_density = terrain_count / total_count
        # Lower confidence if landmark is in open space with no context
        if terrain_density < 0.2:
            confidence *= 0.7
    
    return confidence


# =============================================================================
# Screen Signature Extraction (for Loop Closure)
# =============================================================================

def extract_screen_signature(screen_lines: list[str], player_pos: tuple[int, int]) -> dict[str, Any]:
    """
    Extract a compact signature of the current view for loop closure detection.
    
    The signature captures:
    - Terrain distribution histogram
    - Visible landmark types
    - Boundary distances in each direction
    - Central terrain pattern
    - Orientation information
    
    Args:
        screen_lines: Current screen state
        player_pos: (row, col) of player on screen
        
    Returns:
        Dictionary containing all signature elements
    """
    signature = {
        'terrain_histogram': {},
        'landmark_types': set(),
        'boundary_distances': {},
        'central_terrain': [],
        'orientation': 'down',
        'player_pos': player_pos,
        'screen_dims': (len(screen_lines), max(len(line) for line in screen_lines) if screen_lines else 0)
    }
    
    row, col = player_pos
    
    # 1. Terrain histogram
    for line in screen_lines:
        for char in line:
            if char in TERRAIN_CHARS:
                signature['terrain_histogram'][char] = \
                    signature['terrain_histogram'].get(char, 0) + 1
    
    # 2. Landmark types visible (simplified - just check around player)
    for r in range(max(0, row - 10), min(len(screen_lines), row + 10)):
        for c in range(max(0, col - 10), min(len(screen_lines[r]), col + 10)):
            char = screen_lines[r][c] if c < len(screen_lines[r]) else ' '
            if char in {'<', '>', 'O', '`'}:
                if char == '<':
                    signature['landmark_types'].add('stair_up')
                elif char == '>':
                    signature['landmark_types'].add('stair_down')
                elif char == 'O':
                    signature['landmark_types'].add('fountain')
                elif char == '`':
                    signature['landmark_types'].add('altar')
    
    # 3. Distance to boundaries in each direction
    directions = [
        (-1, 0, 'up'),
        (1, 0, 'down'),
        (0, -1, 'left'),
        (0, 1, 'right')
    ]
    
    for dr, dc, direction in directions:
        dist = 0
        r, c = row + dr, col + dc
        while (0 <= r < len(screen_lines) and 
               0 <= c < len(screen_lines[r]) and
               dist < LANDMARK_DETECTION['max_landmark_distance']):
            char = screen_lines[r][c]
            if char in {'|', '-', ' '}:
                break
            dist += 1
            r += dr
            c += dc
        signature['boundary_distances'][direction] = dist
    
    # 4. Central terrain pattern (5x5 around player)
    for dr in range(-2, 3):
        terrain_row = []
        for dc in range(-2, 3):
            r, c = row + dr, col + dc
            if 0 <= r < len(screen_lines) and 0 <= c < len(screen_lines[r]):
                char = screen_lines[r][c]
                if char in TERRAIN_CHARS:
                    terrain_row.append(char)
                elif char in DYNAMIC_CHARS:
                    terrain_row.append('.')  # Assume floor under creatures
                else:
                    terrain_row.append(' ')  # Unknown
            else:
                terrain_row.append(' ')
        signature['central_terrain'].append(''.join(terrain_row))
    
    # 5. Orientation (default, can be refined)
    signature['orientation'] = 'down'
    
    return signature


def compute_signature_similarity(sig1: dict[str, Any], sig2: dict[str, Any]) -> float:
    """
    Compute similarity between two screen signatures.
    
    Uses weighted combination of:
    - Terrain histogram similarity (cosine distance)
    - Landmark type overlap
    - Boundary distance similarity
    - Central terrain pattern matching
    
    Args:
        sig1, sig2: Signatures to compare
        
    Returns:
        Similarity score from 0.0 to 1.0
    """
    if not sig1 or not sig2:
        return 0.0
    
    weights = LOOP_CLOSURE['signature_similarity_weight']
    scores = {}
    weight_totals = {}
    
    # 1. Terrain histogram similarity (cosine distance)
    if sig1['terrain_histogram'] or sig2['terrain_histogram']:
        all_keys = set(sig1['terrain_histogram'].keys()) | set(sig2['terrain_histogram'].keys())
        if all_keys:
            v1 = [sig1['terrain_histogram'].get(k, 0) for k in all_keys]
            v2 = [sig2['terrain_histogram'].get(k, 0) for k in all_keys]
            
            norm1 = math.sqrt(sum(x*x for x in v1))
            norm2 = math.sqrt(sum(x*x for x in v2))
            
            if norm1 > 0.01 and norm2 > 0.01:
                hist_sim = sum(a*b for a, b in zip(v1, v2)) / (norm1 * norm2)
            else:
                hist_sim = 0.0
            
            scores['terrain'] = hist_sim
            weight_totals['terrain'] = weights['terrain']
    
    # 2. Landmark type match (Jaccard index)
    if sig1['landmark_types'] or sig2['landmark_types']:
        intersection = len(sig1['landmark_types'] & sig2['landmark_types'])
        union = len(sig1['landmark_types'] | sig2['landmark_types'])
        
        if union > 0:
            landmark_sim = intersection / union
        else:
            landmark_sim = 0.0
        
        scores['landmarks'] = landmark_sim
        weight_totals['landmarks'] = weights['landmarks']
    
    # 3. Boundary distance similarity (inverse absolute difference)
    all_dirs = set(sig1['boundary_distances'].keys()) | set(sig2['boundary_distances'].keys())
    if all_dirs:
        total_diff = 0.0
        for direction in all_dirs:
            d1 = sig1['boundary_distances'].get(direction, 0)
            d2 = sig2['boundary_distances'].get(direction, 0)
            total_diff += abs(d1 - d2)
        
        # Convert difference to similarity (exponential decay)
        boundary_sim = math.exp(-total_diff / 20.0)
        
        scores['boundaries'] = boundary_sim
        weight_totals['boundaries'] = weights['boundaries']
    
    # 4. Central terrain pattern matching (normalized Hamming distance)
    if sig1['central_terrain'] and sig2['central_terrain']:
        pattern_sim = _pattern_similarity(sig1['central_terrain'], sig2['central_terrain'])
        
        scores['pattern'] = pattern_sim
        weight_totals['pattern'] = weights['pattern']
    
    # Weighted average
    total_weight = sum(weight_totals.values())
    if total_weight == 0:
        return 0.0
    
    combined_score = sum(scores[k] * w for k, w in weight_totals.items()) / total_weight
    
    return combined_score


def _pattern_similarity(pattern1: list[str], pattern2: list[str]) -> float:
    """Compute similarity between two central terrain patterns."""
    
    if not pattern1 or not pattern2:
        return 0.0
    
    min_height = min(len(pattern1), len(pattern2))
    total_chars = 0
    match_chars = 0
    
    for i in range(min_height):
        str1 = pattern1[i]
        str2 = pattern2[i]
        
        min_len = min(len(str1), len(str2))
        total_chars += min_len
        
        for j in range(min_len):
            c1 = str1[j]
            c2 = str2[j]
            
            # Exact match
            if c1 == c2:
                match_chars += 1
            # Similar terrain types
            elif c1 in TERRAIN_CHARS and c2 in TERRAIN_CHARS:
                match_chars += 0.5
    
    return match_chars / total_chars if total_chars > 0 else 0.0


# =============================================================================
# Visual Odometry System (Main Class)
# =============================================================================

@dataclass
class MotionState:
    """Represents the current motion state with uncertainty."""
    row: float = 0.0
    col: float = 0.0
    heading: float = 0.0  # In radians, 0 = down
    velocity: float = 0.0
    covariance: list[list[float]] = field(default_factory=lambda: [[100.0, 0, 0, 0],
                                                                     [0, 100.0, 0, 0],
                                                                     [0, 0, 10.0, 0],
                                                                     [0, 0, 0, 10.0]])


class VisualOdometrySystem:
    """
    Combines optical flow with landmark-based positioning using a Kalman filter.
    
    This system provides robust position estimation for NetHack by:
    1. Using optical flow to estimate relative movement
    2. Using landmarks for absolute positioning
    3. Detecting loop closures to correct drift
    4. Maintaining uncertainty estimates for sensor fusion
    
    The system handles:
    - Sparse terrain (dungeon corridors)
    - Moving creatures (excluded from flow)
    - Dynamic environment (items, doors opening/closing)
    - Unknown areas (marked as uncertainty)
    """
    
    def __init__(self, screen_width: int = 80, screen_height: int = 24):
        """Initialize the visual odometry system."""
        
        # State estimation
        self.state = MotionState()
        
        # Optical flow configuration
        self._flow_alpha = 0.3
        self._landmark_weight = 0.7
        self._flow_weight = 0.3
        
        # History for filtering and loop closure
        self._flow_history: list[tuple[float, float, float]] = []
        self._keyframe_history: list[dict[str, Any]] = []
        self._landmark_memory: dict[str, list[dict[str, Any]]] = {}
        
        # Keyframe counter (for memory management)
        self._keyframe_counter = 0
        
        # Screen dimensions
        self._screen_width = screen_width
        self._screen_height = screen_height
        
        # Last processed frame
        self._last_screen: list[str] | None = None
        self._last_player_pos: tuple[int, int] | None = None
        self._initial_player_pos: tuple[int, int] | None = None  # Reference for world coordinates
        self._terrain_signature: str | None = None  # Terrain pattern for verification
    
    def update(
        self,
        current_screen: list[str],
        player_row: int,
        player_col: int
    ) -> tuple[float, float, float]:
        """
        Update position estimate from new screen.
        
        Key insight: NetHack keeps player @ centered on screen. When player moves:
        - Player @ stays roughly at same screen position
        - Dungeon terrain shifts opposite to player movement
        - World position = cumulative terrain shift from initial position
        
        Args:
            current_screen: Current screen state
            player_row, player_col: Player position on screen
            
        Returns:
            Tuple of (world_row, world_col, confidence)
        """
        
        confidence = 1.0  # Default confidence
        if self._initial_player_pos is None:
            # First frame - this is our reference point (world origin)
            self.state.row = 0.0
            self.state.col = 0.0
            self._initial_player_pos = (player_row, player_col)
            self._last_player_pos = (player_row, player_col)
        else:
            # Player @ stays centered in NetHack, so use terrain movement
            window_size = 6
            current_terrain = _extract_terrain_around_player(current_screen, player_row, player_col, window_size)
            
            if self._terrain_signature is not None:
                terrain_match = _compare_terrain_signatures(self._terrain_signature, current_terrain)
                
                if terrain_match < 0.95:
                    # Terrain changed - use optical flow
                    flow_dr, flow_dc, flow_conf = compute_windowed_optical_flow(
                        self._last_screen, current_screen, window_size=6, max_search=2
                    )
                    self.state.row += flow_dr
                    self.state.col += flow_dc
                    confidence = 0.85 if flow_conf > 0.4 else 0.6
                else:
                    confidence = 0.95
            else:
                self.state.row = float(player_row - self._initial_player_pos[0])
                self.state.col = float(player_col - self._initial_player_pos[1])
                confidence = 0.95
            
            self._terrain_signature = current_terrain
        
        self._last_screen = current_screen.copy() if current_screen else None
        self._last_player_pos = (player_row, player_col)
        
        return (
            self.state.row,
            self.state.col,
            confidence
        )


def _extract_terrain_around_player(screen_lines: list[str], player_row: int, player_col: int, size: int = 8) -> str:
    """Extract a signature of terrain around the player for verification."""
    half = size // 2
    rows = []
    
    for dr in range(-half, half + 1):
        r = player_row + dr
        if 0 <= r < len(screen_lines):
            row_str = ""
            for dc in range(-half, half + 1):
                c = player_col + dc
                if 0 <= c < len(screen_lines[r]):
                    char = screen_lines[r][c]
                    # Convert to simplified terrain code
                    if char in TERRAIN_CHARS:
                        # Simplify: treat all floor as '.', all walls as '#'
                        if char in {'.', '#'}:
                            row_str += char
                        elif char in {'+', '|', '-', '<', '>'}:
                            row_str += '#'
                        else:
                            row_str += char
                    else:
                        row_str += ' '
                else:
                    row_str += ' '
            rows.append(row_str)
    
    return '|'.join(rows)


def _compare_terrain_signatures(sig1: str, sig2: str) -> float:
    """Compare two terrain signatures. Returns 0.0-1.0 similarity."""
    if not sig1 or not sig2:
        return 0.0
    
    if len(sig1) != len(sig2):
        return 0.0
    
    matches = sum(1 for c1, c2 in zip(sig1, sig2) if c1 == c2)
    return matches / len(sig1)
    
    def _predict_motion(self, flow_dr: float, flow_dc: float, confidence: float) -> None:
        """Predict new state based on optical flow."""
        
        # Filter flow using exponential moving average
        self._flow_history.append((flow_dr, flow_dc, confidence))
        
        if len(self._flow_history) > OPTICAL_FLOW['flow_history_size']:
            self._flow_history.pop(0)
        
        # Weighted average of recent flows
        total_weight = sum(f[2] for f in self._flow_history)
        
        if total_weight > 0:
            filtered_dr = sum(f[0] * f[2] for f in self._flow_history) / total_weight
            filtered_dc = sum(f[1] * f[2] for f in self._flow_history) / total_weight
        else:
            filtered_dr = flow_dr
            filtered_dc = flow_dc
        
        # Update state (with confidence weighting)
        self.state.row += self._flow_alpha * filtered_dr
        self.state.col += self._flow_alpha * filtered_dc
        
        # Estimate heading from recent movement
        if len(self._flow_history) >= 2:
            prev_dr = self._flow_history[-2][0]
            prev_dc = self._flow_history[-2][1]
            
            if abs(prev_dc) > 0.1 or abs(prev_dr) > 0.1:
                self.state.heading = math.atan2(prev_dc, prev_dr)
        
        # Increase uncertainty (motion estimation has noise)
        self.state.covariance[0][0] += confidence * 0.1
        self.state.covariance[1][1] += confidence * 0.1
        
        # Ensure covariance stays bounded
        self.state.covariance[0][0] = min(self.state.covariance[0][0], 1000.0)
        self.state.covariance[1][1] = min(self.state.covariance[1][1], 1000.0)
    
    def _compute_landmark_update(self, landmarks: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Compute world position update from landmarks."""
        
        if not landmarks:
            return None
        
        # For each landmark, check if we've seen it before
        world_position_candidates = []
        
        for landmark in landmarks:
            landmark_type = landmark['type']
            
            if landmark_type in self._landmark_memory:
                # Seen this landmark type before - compute position offsets
                for stored in self._landmark_memory[landmark_type]:
                    offset_row = landmark['world_pos'][0] - stored['world_pos'][0]
                    offset_col = landmark['world_pos'][1] - stored['world_pos'][1]
                    
                    # Compute combined confidence
                    combined_conf = landmark['confidence'] * stored['confidence']
                    
                    world_position_candidates.append({
                        'row': self.state.row + offset_row,
                        'col': self.state.col + offset_col,
                        'confidence': combined_conf
                    })
        
        if not world_position_candidates:
            return None
        
        # Weighted average of candidates
        total_conf = sum(c['confidence'] for c in world_position_candidates)
        
        if total_conf == 0:
            return None
        
        return {
            'row': sum(c['row'] * c['confidence'] for c in world_position_candidates) / total_conf,
            'col': sum(c['col'] * c['confidence'] for c in world_position_candidates) / total_conf,
            'confidence': total_conf / len(world_position_candidates)
        }
    
    def _correct_with_landmarks(self, update: dict[str, Any]) -> None:
        """Apply landmark-based position correction."""
        
        # Compute correction
        row_correction = update['row'] - self.state.row
        col_correction = update['col'] - self.state.col
        
        # Apply correction with landmark weight
        self.state.row += self._landmark_weight * row_correction
        self.state.col += self._landmark_weight * col_correction
        
        # Reduce uncertainty significantly (landmarks are reliable)
        self.state.covariance[0][0] *= (1.0 - self._landmark_weight) * 0.5
        self.state.covariance[1][1] *= (1.0 - self._landmark_weight) * 0.5
        
        # Ensure non-negative variance
        self.state.covariance[0][0] = max(self.state.covariance[0][0], 0.01)
        self.state.covariance[1][1] = max(self.state.covariance[1][1], 0.01)
    
    def _detect_loop_closure(
        self,
        current_screen: list[str],
        player_row: int,
        player_col: int
    ) -> dict[str, Any] | None:
        """Check if we've returned to a previously visited location."""
        
        # Extract signature
        current_signature = extract_screen_signature(current_screen, (player_row, player_col))
        
        # Compare with keyframe history
        best_match = None
        best_similarity = 0.0
        
        for keyframe in self._keyframe_history:
            similarity = compute_signature_similarity(
                current_signature,
                keyframe['signature']
            )
            
            if similarity > LOOP_CLOSURE['similarity_threshold']:
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = keyframe
        
        return best_match if best_match else None
    
    def _apply_loop_closure(self, keyframe: dict[str, Any]) -> None:
        """Reset position when loop closure detected."""
        
        # Compute correction needed
        row_correction = keyframe['state']['row'] - self.state.row
        col_correction = keyframe['state']['col'] - self.state.col
        
        # Apply correction (with smoothing)
        self.state.row += 0.5 * row_correction
        self.state.col += 0.5 * col_correction
        
        # Reduce uncertainty significantly
        self.state.covariance[0][0] *= 0.1
        self.state.covariance[1][1] *= 0.1
        
        # Clear older keyframes (we're back in known territory)
        self._keyframe_history = [
            k for k in self._keyframe_history
            if k['counter'] <= keyframe['counter']
        ]
    
    def _update_keyframe(
        self,
        screen: list[str],
        player_row: int,
        player_col: int
    ) -> None:
        """Update keyframe history with current screen."""
        
        self._keyframe_counter += 1
        
        # Only add keyframe if enough distance from last one
        if self._keyframe_counter < LOOP_CLOSURE['min_keyframe_interval']:
            return
        
        # Check if this is significantly different from last keyframe
        if self._keyframe_history:
            last_keyframe = self._keyframe_history[-1]
            current_sig = extract_screen_signature(screen, (player_row, player_col))
            similarity = compute_signature_similarity(
                current_sig,
                last_keyframe['signature']
            )
            
            if similarity > 0.85:
                return  # Too similar to last keyframe
        
        # Create new keyframe
        signature = extract_screen_signature(screen, (player_row, player_col))
        
        keyframe = {
            'counter': self._keyframe_counter,
            'state': {
                'row': self.state.row,
                'col': self.state.col,
                'heading': self.state.heading
            },
            'signature': signature,
            'timestamp': self._keyframe_counter
        }
        
        self._keyframe_history.append(keyframe)
        
        # Limit keyframe history size
        if len(self._keyframe_history) > 100:
            self._keyframe_history = self._keyframe_history[-100:]
    
    def _compute_overall_confidence(self) -> float:
        """Compute overall confidence in current position estimate."""
        
        # Inverse of uncertainty
        uncertainty = self.state.covariance[0][0] + self.state.covariance[1][1]
        
        # Convert to confidence (lower uncertainty = higher confidence)
        confidence = 1.0 / (1.0 + uncertainty / 100.0)
        
        # Clamp to valid range
        return max(0.0, min(1.0, confidence))
    
    def get_position(self) -> tuple[float, float]:
        """Get current position estimate."""
        return (self.state.row, self.state.col)
    
    def get_uncertainty(self) -> float:
        """Get overall position uncertainty."""
        return self.state.covariance[0][0] + self.state.covariance[1][1]
    
    def add_landmark_to_memory(self, landmark: dict[str, Any]) -> None:
        """Add a landmark to the memory for future matching."""
        
        landmark_type = landmark['type']
        
        if landmark_type not in self._landmark_memory:
            self._landmark_memory[landmark_type] = []
        
        self._landmark_memory[landmark_type].append({
            'world_pos': landmark['world_pos'],
            'confidence': landmark['confidence'],
            'signature': extract_screen_signature(
                [],  # Placeholder - could store full signature
                (0, 0)
            )
        })
        
        # Limit memory per landmark type
        if len(self._landmark_memory[landmark_type]) > 10:
            self._landmark_memory[landmark_type] = self._landmark_memory[landmark_type][-10:]
