"""Dungeon mapping for NetHack.

The map is a record of *memory*, not of the real level.

* A tile has **memory** once a glyph has ever been drawn there on screen
  (``.`` floor, ``#`` cave floor, ``+`` door, ``<``/``>`` stairs, ``|``/``-``
  room walls).  tty NetHack keeps remembered terrain on screen, so memory is
  cumulative: it survives scrolling and going back and forth.
* A tile with **no memory** is blank.  A blank that a *reachable walkable tile*
  sits next to is rendered ``?``: it may be solid rockwall, but it may just as
  well turn into cave floor, a room or a passage the moment the player steps
  adjacent to it.  Blanks are therefore never recorded as walls, and blanks that
  cannot be reached or stood next to are not worth showing.
* The only way a blank becomes a *known* wall is the player bumping into it;
  those tiles are marked solid and rendered ``X``.
* **Door state is readable from the glyph** (``include/defsym.h``): ``+`` is a
  **shut** door, an **open** door is drawn as the wall it sits in (``|`` for a
  door in a horizontal wall, ``-`` in a vertical one) and a doorless doorway
  is drawn ``.``.  So once a position has been seen as ``+`` it stays a
  doorway forever, and its current glyph tells us whether the way through it
  is open right now.
* A door never yet seen shut is still recognisable from its setting
  (``infer_doorways``): a ``|`` whose left and right flanks are wall of the
  *same kind* - two drawn wall glyphs, or two rockwalls confirmed by a bump -
  over ground or still-unseen tiles above and below, is an open door.  The
  kinds have to agree because walls turn at corners: one flank is masonry
  and past the turn there is rock, never more masonry, and a door never sits
  where the wall changes material.  A plain wall tile fails the other test:
  along a room wall one flank is the room's own floor.
  Doorways themselves are evidence on neither axis, so an inference never
  creeps outwards from a known door.  The evidence has to be *seen*: an
  unexplored blank flanks nothing, because a blank is only rockwall once
  something has confirmed it.
* NetHack refuses **diagonal** moves into or out of an intact doorway, so routes
  never plan one.
* A tile that only ever showed an occupant is remembered as *guessed* ground.
  A guess that stands next to the player and still draws blank retires: the
  thing seen there was a transient effect (a beam, an explosion) and the tile
  is rockwall after all.  ``I`` - where an unseen monster was last *heard* -
  is never evidence of ground at all.

Rendered map glyphs
-------------------
===== ==========================================================
``@``  you
``.``  floor
``#``  cave floor / corridor
``+``  doorway with the door **shut**: open it with ``o``+direction
``'``  doorway standing **open**: walk through
``^``  a trap we have spotted (the tile is floor)
``<``  stairs up          ``>`` stairs down
``|``  wall               ``-`` wall
``}``  water or lava: routable around, walking in means swimming
``X``  solid: we tried to walk here and could not
``?``  unrevealed, next to ground you can reach -> explore here
`` ``  nothing remembered and nothing discoverable from here
===== ==========================================================
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
#  Terrain vocabulary
# --------------------------------------------------------------------------- #

FLOOR = "."
CAVE = "#"
DOOR = "+"  # NetHack draws '+' only for a door that is SHUT
UP = "<"
DOWN = ">"
WALL_V = "|"
WALL_H = "-"
LIQUID = "}"  # pool, moat, water, lava: enter at your peril
TRAP = "^"  # a spotted trap: not terrain, the tile underneath stays as it was

#: glyphs that carry terrain information when drawn on screen
VISIBLE_TERRAIN = frozenset({FLOOR, CAVE, DOOR, UP, DOWN, WALL_V, WALL_H, LIQUID})

#: terrain you can walk on.  A shut door is a way through, not a wall: reaching
#: it just means opening it first, so ``DOOR`` counts and the route keys carry
#: the ``o``.
PASSABLE_TERRAIN = frozenset({FLOOR, CAVE, DOOR, UP, DOWN})

#: terrain you can stand on: the player, a monster or an item is only ever
#: drawn on one of these, never inside a wall
OCCUPIABLE = frozenset({FLOOR, CAVE, DOOR, UP, DOWN, LIQUID})

#: a wall glyph and a doorway are the same piece of geometry seen in two states
DOORLIKE = frozenset({DOOR, WALL_V, WALL_H})

#: the two materials a doorway can be set in; the flanks of one door must
#: agree - masonry beside confirmed rock is a corner where the wall turns
BUILDING = "building"
ROCK = "rock"

#: ground that may be either room floor or corridor without contradicting us
GROUNDLIKE = frozenset({FLOOR, CAVE})

#: how far from the expected alignment to look, in tiles: enough for the window
#: to scroll all the way across a level between two reads
SEARCH_RADIUS = 24

#: an alignment has to be *tested* before it is believed: an offset that walks
#: the window off the edge of the remembered level contradicts nothing simply
#: because it looks at nothing, and that is not the same as matching
MIN_ALIGN_COVERAGE = 8


def window_width(screen_lines: list[str], map_rows: tuple[int, int]) -> int:
    """How many columns the map window really draws: tmux trims trailing blanks."""
    first, last = window_rows(screen_lines, map_rows)
    return max((len(screen_lines[row]) for row in range(first, last + 1)), default=0)


def window_rows(screen_lines: list[str], map_rows: tuple[int, int]) -> tuple[int, int]:
    """The first and last screen row we read as map, clamped to the capture."""
    first, last = map_rows
    return max(first, 0), min(last, len(screen_lines) - 1)


def tile_matches(drawn: str, tile: Tile) -> int:
    """Does the screen agree with what we remember here: ``1``, ``-1`` or ``0``?

    ``0`` means the pair says nothing either way: terrain drawn on a tile we
    have never seen, which is how new ground is discovered.
    """
    if tile.solid and not tile.memory:
        # rockwall we confirmed by walking into it: the map never draws it, so
        # a blank is agreement and any glyph at all means we are not here
        return 1 if drawn == " " else -1
    if drawn == " ":
        # everything remembered was drawn once, and the game does not forget,
        # so a blank over remembered terrain contradicts the alignment.  The
        # exception is a *guessed* tile: it was never drawn as terrain, only
        # seen under something standing there - and transient effects (a zap's
        # beam, an explosion) are drawn over rockwall too, so the guess may
        # have been wrong.  A blank over a guess says nothing about where the
        # screen sits; ``mark_solid`` retires the guess when adjacency proves
        # the tile is rockwall.
        if tile.guessed and not tile.visited:
            return 0
        return -1 if tile.memory else 0
    if drawn not in VISIBLE_TERRAIN:
        # the player, a monster, an item: all of them stand on ground.  A wall
        # glyph is not proof of a clash though: an open door is drawn as the
        # wall it sits in ('-' vertical, '|' horizontal, include/defsym.h) and
        # the only way to learn it is a doorway is to walk through it.
        if not tile.memory:
            return 0
        if tile.solid:
            return -1  # iron bars, a tree, rockwall: nothing stands in these
        if tile.doorway or tile.terrain in OCCUPIABLE:
            return 1
        return 0  # a wall we never saw as a doorway: it may be one, or we are
    if not tile.memory:
        return 0
    if tile.terrain == drawn or tile.guessed:
        return 1
    if {tile.terrain, drawn} <= GROUNDLIKE:
        return 1  # room floor beside a corridor: the same kind of ground
    if drawn in DOORLIKE and (tile.doorway or tile.terrain in DOORLIKE):
        return 1  # one doorway, drawn open as its wall or shut as '+'
    if drawn == FLOOR and tile.terrain in DOORLIKE:
        return 1  # a doorway whose door is gone
    return -1


def _ring(center: tuple[int, int], radius: int) -> Iterator[tuple[int, int]]:
    """The offsets exactly ``radius`` steps from ``center`` (Chebyshev)."""
    row, col = center
    if radius <= 0:
        yield center
        return
    for dr in range(-radius, radius + 1):
        yield (row + dr, col - radius)
        yield (row + dr, col + radius)
    for dc in range(-radius + 1, radius):
        yield (row - radius, col + dc)
        yield (row + radius, col + dc)

DIR_KEYS: dict[tuple[int, int], str] = {
    (0, -1): "h",
    (1, 0): "j",
    (-1, 0): "k",
    (0, 1): "l",
    (-1, -1): "y",
    (-1, 1): "u",
    (1, -1): "b",
    (1, 1): "n",
}
KEY_DIRS: dict[str, tuple[int, int]] = {v: k for k, v in DIR_KEYS.items()}


def route_displacement(command: str) -> tuple[int, int] | None:
    """The tiles a string of movement keys walks, or None when it may not walk.

    A frontier route arrives as one command (``hhhy``, ``jnnjj``), so odometry
    has to add the steps up rather than look at a single key.  Anything that is
    not a plain direction gives up: an uppercase run key travels until something
    happens, so its length is not known here, and a command with an ``o`` or a
    ``f`` in it does something other than walk.

    This is only ever a *starting point* for matching the screen: the moment the
    hero is blocked the guess is wrong, and the map has to be read anyway.
    """
    if len(command) == 2 and command[0] == "m" and command[1] in KEY_DIRS:
        command = command[1]  # m + direction: move silently, one step
    if not command:
        return None
    total = (0, 0)
    for char in command:
        step = KEY_DIRS.get(char)
        if step is None:
            return None
        total = (total[0] + step[0], total[1] + step[1])
    return total


def route_tokens(command: str) -> list[str] | None:
    """Split a frontier route into the atomic keystrokes it is made of.

    A route is direction keys, optionally with ``o``+direction in front of a
    step that enters a shut door.  Nothing else parses: a run key travels an
    unknown distance, a count repeats, a wait or a command letter has effects
    this split could not verify key by key, and anything with such a key in it
    goes to the game whole, exactly as before.
    """
    if not command:
        return None
    tokens: list[str] = []
    i = 0
    while i < len(command):
        char = command[i]
        if char == "o" and i + 1 < len(command) and command[i + 1] in KEY_DIRS:
            tokens.append(command[i : i + 2])
            i += 2
        elif char in KEY_DIRS:
            tokens.append(char)
            i += 1
        else:
            return None
    return tokens


# --------------------------------------------------------------------------- #
#  Tiles
# --------------------------------------------------------------------------- #


@dataclass
class Tile:
    """What we remember about one position of a dungeon level."""

    #: remembered glyph, or None when we have never seen anything here
    terrain: str | None = None
    #: confirmed impassable (we walked into it and could not get through)
    solid: bool = False
    #: the player has stood on this tile
    visited: bool = False
    #: this position is a doorway.  An *open* door is drawn with the wall glyph
    #: of the wall it sits in (``|`` or ``-``), so seeing a wall here does not
    #: undo the fact that it is a way through.  It gets set by seeing ``+``,
    #: by standing in it, and by ``infer_doorways`` reading the wall around it.
    doorway: bool = False
    #: a trap has been spotted here (``^``); the tile is still floor
    trap: bool = False
    #: a tile whose "floor" we only inferred - the player or a monster was
    #: standing there, so the game never drew its terrain for us
    guessed: bool = False

    @property
    def memory(self) -> bool:
        """True when we have remembered terrain here."""
        return self.terrain is not None

    @property
    def passable(self) -> bool:
        if self.solid:
            return False
        if self.doorway:
            # the way through a wall whatever the door is doing: an open one
            # you walk straight through, a shut one you open first (and the
            # route keys carry that 'o')
            return True
        return self.terrain in PASSABLE_TERRAIN

    @property
    def door_closed(self) -> bool:
        """A doorway whose door is shut right now.

        ``+`` on screen is a closed door (``include/defsym.h``), and standing in
        a doorway proves it is not: the tile under ``@`` keeps whatever glyph we
        saw there last, which can be a stale ``+`` after ``olo`` in one go.
        """
        return self.doorway and self.terrain == DOOR and not self.visited

    @property
    def door_open(self) -> bool:
        """A doorway we can walk through in either direction."""
        return self.doorway and self.memory and not (
            self.terrain == DOOR and not self.visited
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "terrain": self.terrain,
            "solid": self.solid,
            "visited": self.visited,
            "doorway": self.doorway,
            "trap": self.trap,
            "guessed": self.guessed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tile:
        terrain = data.get("terrain")
        return cls(
            terrain=terrain,
            solid=bool(data.get("solid", False)),
            visited=bool(data.get("visited", False)),
            doorway=bool(data.get("doorway", False)) or terrain == DOOR,
            trap=bool(data.get("trap", False)),
            guessed=bool(data.get("guessed", False)),
        )


# --------------------------------------------------------------------------- #
#  Rooms
# --------------------------------------------------------------------------- #


@dataclass
class Room:
    """A connected region of remembered floor, bounded by walls/doors/cave."""

    id: int
    tiles: list[tuple[int, int]]
    doors: list[tuple[int, int]]

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        rows = [r for r, _ in self.tiles]
        cols = [c for _, c in self.tiles]
        return (min(rows), max(rows), min(cols), max(cols))

    @property
    def center(self) -> tuple[int, int]:
        min_r, max_r, min_c, max_c = self.bounds
        return ((min_r + max_r) // 2, (min_c + max_c) // 2)

    def contains(self, pos: tuple[int, int]) -> bool:
        min_r, max_r, min_c, max_c = self.bounds
        return min_r <= pos[0] <= max_r and min_c <= pos[1] <= max_c


# --------------------------------------------------------------------------- #
#  Map
# --------------------------------------------------------------------------- #


class DungeonMap:
    """Cumulative memory of a single dungeon level."""

    def __init__(self, level: int = 1):
        self.level = level
        self.tiles: dict[tuple[int, int], Tile] = {}
        #: world coordinate = dungeon_offset + screen coordinate
        self.dungeon_offset: tuple[int, int] = (0, 0)
        self.player_world_pos: tuple[int, int] | None = None
        #: every staircase this level has shown us.  A level can hold two of
        #: either (a broken corridor, a shaft), and a single slot lets the
        #: second stair silently overwrite the first - which is how "no way
        #: down" gets invented on a level that has one.
        self.downstairs: set[tuple[int, int]] = set()
        self.upstairs: set[tuple[int, int]] = set()
        #: wall/rockwall tiles we have stood next to and pressed `s`; the
        #: ledger that makes "this level is searched" a fact instead of a vibe
        self.searched: set[tuple[int, int]] = set()
        self._rooms_cache: list[Room] | None = None
        self._reach_cache: dict[tuple[int, int], int] | None = None
        self._frontier_cache: set[tuple[int, int]] | None = None

    @property
    def downstairs_pos(self) -> tuple[int, int] | None:
        """The first remembered down stair, for callers that only want one."""
        return min(self.downstairs, default=None)

    @property
    def upstairs_pos(self) -> tuple[int, int] | None:
        return min(self.upstairs, default=None)

    def _invalidate(self) -> None:
        self._rooms_cache = None
        self._reach_cache = None
        self._frontier_cache = None

    # ------------------------------------------------------------------ #
    #  writing memory
    # ------------------------------------------------------------------ #

    def get_tile(self, row: int, col: int) -> Tile | None:
        return self.tiles.get((row, col))

    def terrain_at(self, row: int, col: int) -> str | None:
        tile = self.tiles.get((row, col))
        return tile.terrain if tile else None

    def is_passable(self, row: int, col: int) -> bool:
        tile = self.tiles.get((row, col))
        return bool(tile and tile.passable)

    def _touch(self, row: int, col: int) -> Tile:
        tile = self.tiles.get((row, col))
        if tile is None:
            tile = Tile()
            self.tiles[(row, col)] = tile
        return tile

    def observe(self, row: int, col: int, terrain: str, *, overwrite: bool = True) -> bool:
        """Remember the glyph the screen draws at world (row, col).

        Returns False when the tile already holds different terrain and
        ``overwrite`` is off.  Refusing to overwrite is what stops a bad
        alignment from smearing walls across the map.
        """
        if terrain not in VISIBLE_TERRAIN:
            return False
        tile = self._touch(row, col)
        if tile.memory and tile.terrain != terrain and not overwrite:
            return False
        changed = tile.terrain != terrain or tile.solid or tile.guessed
        tile.terrain = terrain
        tile.guessed = False
        # seeing the glyph the tile was blocked for keeps the block: iron bars
        # are drawn as '#' and stay impassable however often we look
        if not (tile.solid and terrain == "#"):
            tile.solid = False
        if terrain == DOOR:
            tile.doorway = True
        if changed:
            self._invalidate()
        if terrain == DOWN:
            self.downstairs.add((row, col))
        elif terrain == UP:
            self.upstairs.add((row, col))
        return True

    def observe_guess(self, row: int, col: int) -> None:
        """Remember "ground of some kind" where the screen drew an occupant.

        A monster, item or feature is only ever drawn on a tile you can stand on,
        which rules out walls and rockwall but says nothing about whether it is
        room floor, corridor, water or stairs - so the guess is marked as such and
        never allowed to contradict a later drawing of the real glyph.  It does
        undo a rockwall mark: something standing there proves it is not wall.
        """
        tile = self._touch(row, col)
        if tile.memory and not tile.solid:
            if tile.terrain in DOORLIKE:
                # whatever stands here is standing in a doorway, not in a wall
                tile.doorway = True
                self._invalidate()
            return  # we already know what this tile is
        tile.terrain = FLOOR
        tile.guessed = True
        tile.solid = False
        self._invalidate()

    def set_player_position(self, pos: tuple[int, int]) -> None:
        """Move the remembered player; reachability depends on where they are."""
        if self.player_world_pos != pos:
            self.player_world_pos = pos
            self._reach_cache = None
            self._frontier_cache = None

    def visit(self, row: int, col: int) -> None:
        """The player is standing on world (row, col)."""
        tile = self._touch(row, col)
        if not tile.visited or tile.solid or tile.terrain is None or (
            tile.terrain in DOORLIKE and not tile.doorway
        ):
            tile.visited = True
            tile.solid = False
            if tile.terrain is None:
                # the player's glyph hides the tile underneath; assume floor of
                # some kind, but remember that we only assumed it
                tile.terrain = FLOOR
                tile.guessed = True
            elif tile.terrain in DOORLIKE and not tile.doorway:
                # we are standing in it, so that '-' or '|' was never a piece of
                # wall: it is an open door we had not seen shut yet
                tile.doorway = True
            self._invalidate()

    def mark_solid(self, row: int, col: int) -> None:
        """Confirm we cannot walk here: we tried and the game refused.

        A tile with no memory of its own becomes rockwall, which is what almost
        all of that is.  A tile remembered as corridor or floor that turned out
        to bar the way is iron bars, a tree or a raised drawbridge - the tty map
        draws all of those as ``#`` - but only if we have never stood in it.
        A doorway keeps its state: a shut door is a way through, not a wall.

        A *guess* next to the player that drew blank was simply wrong: the tile
        showed an occupant (or a transient effect) at a moment we could not see
        the ground, and now that we overlook it, nothing is drawn there.  The
        guess retires and the rockwall takes its place.
        """
        tile = self._touch(row, col)
        if tile.doorway or tile.solid:
            return
        if tile.visited:
            return  # we have walked here, so whatever stopped us was not terrain
        if tile.guessed:
            tile.terrain = None
            tile.guessed = False
        elif tile.memory and tile.terrain != CAVE:
            return  # a wall or floor we saw drawn is not a surprise obstacle
        tile.solid = True
        self._invalidate()
        # a newly confirmed rockwall is exactly the evidence a nearby open
        # door was waiting for: its wall neighbours are never drawn until
        # something stands where the room used to be unseen
        self.infer_doorways()

    # ------------------------------------------------------------------ #
    #  the search ledger
    # ------------------------------------------------------------------ #

    def mark_searched_around(self, pos: tuple[int, int]) -> int:
        """Record that the player stood at ``pos`` and searched (``s``).

        Searching covers the eight tiles around the searcher; re-searching a
        tile is legal (a secret door may be found on the tenth attempt when
        the ninth failed), but the ledger is what lets the server say "every
        wall within reach has been searched twice" instead of leaving that to
        the player's memory of how many ``20s`` they typed.

        Returns how many wall tiles were newly covered.
        """
        row, col = pos
        covered = 0
        for dr, dc in DIR_KEYS:
            wall = (row + dr, col + dc)
            tile = self.tiles.get(wall)
            if tile is None or wall in self.searched:
                continue
            if tile.solid or tile.terrain in (WALL_V, WALL_H):
                self.searched.add(wall)
                covered += 1
        return covered

    def unsearched_wall_targets(self) -> list[tuple[int, int]]:
        """Wall or rockwall tiles that touch reachable ground and no search
        has covered yet - the checklist of where a secret door could still be.
        """
        found: set[tuple[int, int]] = set()
        for pos in self.reachable():
            row, col = pos
            for dr, dc in DIR_KEYS:
                wall = (row + dr, col + dc)
                if wall in self.searched:
                    continue
                tile = self.tiles.get(wall)
                if tile is not None and (tile.solid or tile.terrain in (WALL_V, WALL_H)):
                    found.add(wall)
        return sorted(found)

    # ------------------------------------------------------------------ #
    #  open doors, read from the wall around them
    # ------------------------------------------------------------------ #

    def _wall_evidence(self, pos: tuple[int, int]) -> str | None:
        """One side of a door in a wall: which kind of wall stands there.

        Either ``BUILDING`` - a remembered ``|``/``-``, the glyph mixing
        freely along a run that turns - or ``ROCK``, a rockwall we walked
        into.  Both say "the wall runs through here and not past you", but a
        door sits in *one* wall, so its two flanks have to agree on the
        material: masonry on one side and rock on the other is a corner, not
        a doorway.  An unexplored blank says nothing: it may be rockwall or
        it may be ground nobody has looked at, so it is no evidence at all.
        """
        tile = self.tiles.get(pos)
        if tile is None or tile.doorway:
            return None
        if tile.solid:
            return ROCK
        if tile.terrain in (WALL_V, WALL_H):
            return BUILDING
        return None

    def _ground_or_unseen(self, pos: tuple[int, int]) -> bool:
        """What an open door leads to: ground you can stand on, water, or
        tiles we have simply not seen yet.

        NetHack draws *through* an open door, so the ground beyond it joins
        memory at the same moment the doorway does; treating unseen as
        possible ground only means a door is recognised one screen earlier
        or one bump later, never that a wall becomes walkable by accident -
        anything the game confirms as impassable refuses this.

        A doorway is no passage either: doors never lead straight into
        other doors, and counting one would let an inference creep from a
        known doorway along whatever wall stands beside it.
        """
        tile = self.tiles.get(pos)
        if tile is None or not (tile.memory or tile.solid):
            return True
        if tile.doorway:
            return False
        return tile.passable or tile.terrain == LIQUID

    def infer_doorways(self) -> int:
        """Flag the ``|`` and ``-`` tiles that can only be open doors.

        An open door is drawn with a wall glyph (``include/defsym.h``):
        ``S_hodoor`` ``|`` is a door *in a horizontal wall* - the wall runs
        east-west as ``-------`` and its open door is drawn ``---|---``, so
        you walk through north/south and the wall continues left/right - and
        ``S_vodoor`` ``-`` is a door in a vertical wall.  So a wall glyph is
        a doorway when both flanks are wall evidence *of the same kind* and
        both ends of the passage axis are ground or still unseen::

            ...               <- ground (or unseen) above
           XX|X               <- '|' with rockwall left and right
            ##                <- ground below: the door into the top-right room

        No genuine wall tile can satisfy this: along a room wall one flank
        is always the room's own floor.  The kinds must agree because walls
        turn at corners - the corner tile carries a wall glyph, the masonry
        ends at it and outside the building stands rock - and a door is
        never set where the wall changes material.  This is the difference
        between ``---|---``, a way through, and ``X-|`` , the end of one.
        Doorways count as evidence on neither axis: that is what stops an
        inference creeping outwards from a known door along the wall beside
        it.  Run after every merge and every confirmed blank, so a door is
        recognised as soon as its surroundings are known, "just like if it
        was a + first"; the state is remembered, not re-guessed - with one
        exception: a flagged doorway whose flanks *contradict* each other
        (masonry one side, confirmed rock the other) was a corner mistaken
        for a door, and this pass retracts it.  Only inferred ones: what we
        saw as ``+``, stood in, or found broken keeps its flag whatever the
        flanks claim.

        Returns how many tiles changed.
        """
        changed = 0
        for (row, col), tile in self.tiles.items():
            if tile.solid or not tile.memory:
                continue
            if tile.terrain == WALL_V:
                flanks = ((row, col - 1), (row, col + 1))
                passage = ((row - 1, col), (row + 1, col))
            elif tile.terrain == WALL_H:
                flanks = ((row - 1, col), (row + 1, col))
                passage = ((row, col - 1), (row, col + 1))
            else:
                continue
            left, right = (self._wall_evidence(p) for p in flanks)
            if tile.doorway:
                if (
                    not tile.visited
                    and left is not None
                    and right is not None
                    and left != right
                ):
                    tile.doorway = False
                    changed += 1
            elif left is not None and left == right and all(
                self._ground_or_unseen(p) for p in passage
            ):
                tile.doorway = True
                changed += 1
        if changed:
            self._invalidate()
        return changed

    # ------------------------------------------------------------------ #
    #  screen -> map
    # ------------------------------------------------------------------ #

    @staticmethod
    def screen_cells(
        screen_lines: list[str], map_rows: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        """Terrain-bearing cells inside the map window.

        Rows outside the window (message line, status bar) are never read as
        terrain: ``You don't have that object.`` is not a corridor.
        """
        first, last = map_rows
        cells: list[tuple[int, int, str]] = []
        for srow in range(max(first, 0), min(last, len(screen_lines) - 1) + 1):
            for scol, char in enumerate(screen_lines[srow]):
                if char in VISIBLE_TERRAIN:
                    cells.append((srow, scol, char))
        return cells

    @staticmethod
    def occupied_cells(
        screen_lines: list[str], map_rows: tuple[int, int]
    ) -> list[tuple[int, int]]:
        """Cells showing something that is neither terrain nor blank.

        A monster, item or feature is only ever drawn on walkable ground, so a
        visible glyph there settles the question: the tile is not a wall.  The
        exact ground type stays unknown, so we remember plain floor.

        ``I`` is the notable exception (``MONSYM`` ``S_invisible``): it marks
        where an unseen monster was *last heard*, and that spot needs no ground
        at all - sound reaches through rockwall.  Remembering it as floor is
        how phantom rooms appear out of nowhere, so it proves nothing.
        """
        first, last = map_rows
        cells: list[tuple[int, int]] = []
        for srow in range(max(first, 0), min(last, len(screen_lines) - 1) + 1):
            for scol, char in enumerate(screen_lines[srow]):
                if char == " " or char == "@" or char == "I" or char in VISIBLE_TERRAIN:
                    continue
                cells.append((srow, scol))
        return cells

    @staticmethod
    def trap_cells(
        screen_lines: list[str], map_rows: tuple[int, int]
    ) -> list[tuple[int, int]]:
        """Cells showing ``^``: a trap the hero has spotted.

        A trap is drawn on top of the ground it is in, so the tile stays floor;
        remembering which tiles are trapped is worth far more later on.
        """
        first, last = map_rows
        cells: list[tuple[int, int]] = []
        for srow in range(max(first, 0), min(last, len(screen_lines) - 1) + 1):
            for scol, char in enumerate(screen_lines[srow]):
                if char == TRAP:
                    cells.append((srow, scol))
        return cells

    # ---- aligning a screen with memory -------------------------------- #

    def remembered_by_row(self) -> dict[int, list[tuple[int, Tile]]]:
        """Remembered tiles grouped per world row.

        Scanning an alignment means looking at the band of memory that the map
        window covers, so having the rows ready is what keeps a wide search
        cheap: one dict lookup per screen row instead of one per remembered tile.
        """
        index: dict[int, list[tuple[int, Tile]]] = {}
        for (row, col), tile in self.tiles.items():
            if tile.memory or tile.solid:
                index.setdefault(row, []).append((col, tile))
        return index

    def score_offset(
        self,
        screen_lines: list[str],
        map_rows: tuple[int, int],
        offset: tuple[int, int],
        index: dict[int, list[tuple[int, Tile]]],
        width: int,
        *,
        stop_at_conflicts: int | None = None,
    ) -> tuple[int, int, int]:
        """Compare memory with the screen held at ``offset``.

        Returns ``(matches, conflicts, coverage)``: how many remembered tiles the
        window covers and how many of them the screen draws as remembered.  Every
        remembered tile inside the window is asked the same question, walls and
        floors and doors and water weighing exactly the same: is the screen still
        drawing it here?  The game never forgets terrain it has shown you, so a
        remembered wall standing on a blank contradicts this alignment as
        strongly as a wall drawn where a corridor was.

        Two answers are deliberately not signals: terrain drawn where we have
        never seen anything (that is simply new ground), and a blank over
        rockwall we confirmed by walking into it, because rockwall is never
        drawn at all.
        """
        first, last = window_rows(screen_lines, map_rows)
        matches = 0
        conflicts = 0
        coverage = 0
        for srow in range(first, last + 1):
            line = screen_lines[srow]
            for wcol, tile in index.get(offset[0] + srow, ()):
                scol = wcol - offset[1]
                if not 0 <= scol < width:
                    continue  # the level carries on past the edge of the window
                coverage += 1
                drawn = line[scol] if 0 <= scol < len(line) else " "
                verdict = tile_matches(drawn, tile)
                if verdict > 0:
                    matches += 1
                elif verdict < 0:
                    conflicts += 1
                    if stop_at_conflicts is not None and conflicts > stop_at_conflicts:
                        return matches, conflicts, coverage
        return matches, conflicts, coverage

    @staticmethod
    def coverage(
        index: dict[int, list[tuple[int, Tile]]],
        offset: tuple[int, int],
        rows: range,
        width: int,
    ) -> int:
        """How many remembered tiles the window covers at this alignment."""
        return sum(
            sum(
                1
                for wcol, _ in index.get(offset[0] + srow, ())
                if 0 <= wcol - offset[1] < width
            )
            for srow in rows
        )

    def solve_offset(
        self,
        screen_lines: list[str],
        map_rows: tuple[int, int],
        player_screen: tuple[int, int] | None = None,
        expected_world: tuple[int, int] | None = None,
        *,
        search_radius: int = SEARCH_RADIUS,
    ) -> dict[str, Any]:
        """Find where this screen sits in the level.

        The search starts at the alignment the keys imply and the one the
        screen had when it was last read, and works outward one tile at a
        time.  Two rules keep the answer from wavering, and both were learned
        the hard way inside one symmetric shop room:

        * **A tie keeps the alignment we already had.**  A screen that
          contradicts nothing at two offsets - a room whose walls look the
          same shifted a tile - must not move the map to whichever offset the
          ring order happened to reach first, or every re-read of such a room
          flips the level between two readings.  A new alignment has to
          explain *strictly more* memory to win.
        * **An equally clean second reading is reported, not hidden.**  Once a
          conflict-free fit is found the search continues two rings further;
          any other clean fit that explains as much memory comes back in
          ``alternatives``, so the caller knows the screen places us in more
          than one place and can say so instead of steering by a coin flip.

        Returns ``{"offset", "matches", "conflicts", "coverage", "candidates",
        "alternatives"}``.
        """
        index = self.remembered_by_row()
        width = window_width(screen_lines, map_rows)
        first, last = window_rows(screen_lines, map_rows)
        rows = range(first, last + 1)

        starts: list[tuple[int, int]] = []
        if expected_world is not None and player_screen is not None:
            starts.append(
                (
                    expected_world[0] - player_screen[0],
                    expected_world[1] - player_screen[1],
                )
            )
        if self.dungeon_offset not in starts:
            starts.append(self.dungeon_offset)

        if not index:
            return {
                "offset": starts[0],
                "matches": 0,
                "conflicts": 0,
                "coverage": 0,
                "candidates": 0,
                "alternatives": [],
            }

        # An alignment only earns trust by being tested, so it has to cover a
        # decent share of what the alignment under our feet covers: an offset
        # that walks the window off the edge of the remembered level contradicts
        # nothing simply because it looks at nothing.
        need = max(MIN_ALIGN_COVERAGE, max(self.coverage(index, s, rows, width) for s in starts) // 2)

        best: tuple[tuple[int, int, int, int], tuple[int, int, int], tuple[int, int]] | None = None
        clean_fits: list[tuple[int, int, tuple[int, int]]] = []
        tried: set[tuple[int, int]] = set()
        scored = 0
        stop_after: int | None = None  # rings past the first clean fit still worth a look
        for start in starts:
            for radius in range(search_radius + 1):
                if stop_after is not None and radius > stop_after:
                    break
                for candidate in _ring(start, radius):
                    if candidate in tried:
                        continue
                    tried.add(candidate)
                    scored += 1
                    limit = None if best is None else best[1][1]
                    matches, conflicts, coverage = self.score_offset(
                        screen_lines,
                        map_rows,
                        candidate,
                        index,
                        width,
                        stop_at_conflicts=limit,
                    )
                    if coverage < need:
                        continue  # untested: this offset is not evidence of anything
                    if conflicts == 0:
                        clean_fits.append((matches, coverage, candidate))
                        if stop_after is None or radius < stop_after:
                            stop_after = radius + 2
                    key = (
                        conflicts,
                        -matches,
                        # stickiness: equal conflicts and matches keep the
                        # alignment we were already reading the world through
                        0 if candidate == self.dungeon_offset else 1,
                        abs(candidate[0] - starts[0][0]) + abs(candidate[1] - starts[0][1]),
                    )
                    if best is None or key < best[0]:
                        best = (key, (matches, conflicts, coverage), candidate)
        if best is None:
            # nothing in range covers enough memory to judge: keep the alignment
            # the keyboard implies, the map is entering country we have not seen
            matches, conflicts, coverage = self.score_offset(
                screen_lines, map_rows, starts[0], index, width
            )
            return {
                "offset": starts[0],
                "matches": matches,
                "conflicts": conflicts,
                "coverage": coverage,
                "candidates": scored,
                "alternatives": [],
            }
        _, (matches, conflicts, coverage), candidate = best
        alternatives = [
            {"offset": list(alt), "matches": alt_matches, "coverage": alt_cov}
            for alt_matches, alt_cov, alt in clean_fits
            if alt != candidate and conflicts == 0 and alt_matches >= matches
        ]
        return {
            "offset": candidate,
            "matches": matches,
            "conflicts": conflicts,
            "coverage": coverage,
            "candidates": scored,
            "alternatives": alternatives,
        }

    def apply_screen(
        self,
        screen_lines: list[str],
        player_screen: tuple[int, int],
        map_rows: tuple[int, int],
        expected_world: tuple[int, int] | None = None,
        player_candidates: list[tuple[int, int]] | None = None,
    ) -> dict[str, Any]:
        """Merge one screen into memory.

        ``expected_world`` is where the keyboard says the player should be now
        (the last position plus the steps of the command we sent).  It is only
        used as the place to *start* looking: the screen decides the alignment,
        because odometry is wrong the moment a move is refused, a monster
        blocks the way or the hero is levitating over water.

        ``player_candidates`` is every ``@`` the screen draws inside the map
        window.  One of them is the hero; a shopkeeper, a human-shaped statue
        or a shield spell effect can draw another, and picking the wrong one
        puts the whole level one tile sideways.  With two or more, each is
        tried as the hero in turn and the reading that contradicts memory
        least wins; where that still scores exactly equal, the screen cannot
        say who is who - so the merge says ``alignment_status: "ambiguous"``,
        touches nothing, and leaves the caller to move one tile and let the
        delta answer.  Which ``@`` is the hero is decided by continuity: the
        one memory and the keys agree is *moving*, not the first one found.

        Returns the offset, how much of memory it explains and how many tiles
        it contradicts; a nonzero conflict count means the map may be
        misaligned.
        """
        candidates = list(dict.fromkeys(player_candidates or [player_screen]))
        if player_screen not in candidates:
            candidates.insert(0, player_screen)
        predicted = expected_world if expected_world is not None else self.player_world_pos

        def chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
            return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

        alternatives: list[dict[str, Any]] = []
        if not self.tiles:
            # a level never seen anchors on the hero - with several @ glyphs on
            # the very first screen the hero is the one the viewport centres on,
            # the only evidence available before there is any memory
            first, last = window_rows(screen_lines, map_rows)
            mid_r, mid_c = (first + last) / 2, window_width(screen_lines, map_rows) / 2
            hero = min(candidates, key=lambda p: (abs(p[0] - mid_r), abs(p[1] - mid_c)))
            self.dungeon_offset = (-hero[0], -hero[1])
            scrolled = True
            matches, conflicts, coverage, tried = 0, 0, 0, 0
        else:
            readings = []
            for cand in candidates:
                solved = self.solve_offset(
                    screen_lines, map_rows, cand, expected_world
                )
                world = (solved["offset"][0] + cand[0], solved["offset"][1] + cand[1])
                continuity = chebyshev(world, predicted) if predicted is not None else 0
                readings.append((solved, cand, world, continuity))
            readings.sort(
                key=lambda r: (
                    r[0]["conflicts"],
                    -r[0]["matches"],
                    # stickiness comes before continuity: when two readings of
                    # this screen explain memory equally, the one that keeps
                    # the alignment we already read the world through wins -
                    # that is what stops a symmetric shop from flipping the
                    # level between two clean fits on every re-read.  (A real
                    # scroll puts the old offset's terrain out of register, so
                    # the old reading loses on conflicts and cannot hide.)
                    0 if r[0]["offset"] == self.dungeon_offset else 1,
                    # only between equally-standing alignments do the keys
                    # pick which @ is the hero
                    r[3],
                    r[0]["candidates"],
                )
            )
            solved, hero, hero_world, continuity = readings[0]
            matches, conflicts, coverage = (
                solved["matches"],
                solved["conflicts"],
                solved["coverage"],
            )
            tried = solved["candidates"]

            # A screen that fits twice may still be worth believing: when the
            # fit we keep is the one we already had, nothing flips and the
            # choice is coherent by construction.  But when the screen would
            # *move* us onto an alignment another reading explains just as
            # well - or when two @s tie with neither memory nor the keys able
            # to tell them apart - this screen cannot place us at all: report
            # it, touch nothing, and let one single-key move answer it.
            if conflicts == 0:
                winner_sticky = 0 if solved["offset"] == self.dungeon_offset else 1
                rivals = [
                    r
                    for r in readings[1:]
                    if r[0]["conflicts"] == 0
                    and r[0]["matches"] >= matches
                    and r[2] != hero_world
                    # a rival only counts when it stands exactly where the
                    # winner stands in the sticky order: an @ that could only
                    # be the hero by *moving* the alignment is not really a
                    # rival of the reading that keeps it
                    and (0 if r[0]["offset"] == self.dungeon_offset else 1)
                    == winner_sticky
                    # ...nor one the keys can already tell from the winner:
                    # a rival ties on continuity too, or the reading that
                    # matches where the hero is expected has won on evidence.
                    # (The common near-tie - bumping into another @ - is not
                    # geometry's call at all: the game forbids walking onto
                    # it, and the route layer decides that by its own rules.)
                    and r[3] == continuity
                ]
                moving = solved["offset"] != self.dungeon_offset
                if rivals or (moving and solved["alternatives"]):
                    return {
                        "offset": self.dungeon_offset,
                        "player_world_pos": self.player_world_pos,
                        "scrolled": False,
                        "alignment": round(1.0 if coverage == 0 else matches / coverage, 3),
                        "coverage": matches + conflicts,
                        "conflicts": conflicts,
                        "candidates": tried,
                        "rejected_tiles": 0,
                        "alignment_status": "ambiguous",
                        "hero_candidates": [list(hero_world)] + [list(r[2]) for r in rivals],
                        "alternatives": solved["alternatives"] if moving else [],
                    }
            alternatives = [] if conflicts else solved["alternatives"]
            scrolled = solved["offset"] != self.dungeon_offset
            self.dungeon_offset = solved["offset"]

        # a screen that clashes with memory means our alignment is suspect: keep
        # what we knew and only learn genuinely new tiles
        overwrite = conflicts == 0

        cells = self.screen_cells(screen_lines, map_rows)
        rejected = 0
        for srow, scol, char in cells:
            if not self.observe(
                self.dungeon_offset[0] + srow,
                self.dungeon_offset[1] + scol,
                char,
                overwrite=overwrite,
            ):
                rejected += 1

        # something visible but not terrain: the tile is ground, not a wall, but
        # which ground we have not seen - a monster in a doorway still stands in
        # the doorway, so a remembered tile is left exactly as it was
        for srow, scol in self.occupied_cells(screen_lines, map_rows):
            world = (self.dungeon_offset[0] + srow, self.dungeon_offset[1] + scol)
            self.observe_guess(*world)

        # '^' is a spotted trap sitting on ground we just remembered
        for srow, scol in self.trap_cells(screen_lines, map_rows):
            world = (self.dungeon_offset[0] + srow, self.dungeon_offset[1] + scol)
            tile = self._touch(*world)
            if not tile.trap:
                tile.trap = True
                self._invalidate()

        prow, pcol = hero
        self.set_player_position(
            (self.dungeon_offset[0] + prow, self.dungeon_offset[1] + pcol)
        )
        self.visit(*self.player_world_pos)
        self._reveal_neighbours(screen_lines, hero, map_rows)
        # the screen just told us about walls, ground and fresh rockwall:
        # an open door seen for the first time has its whole setting at once
        self.infer_doorways()

        # the score was taken against the memory as it was before this screen
        coverage = matches + conflicts
        return {
            "offset": self.dungeon_offset,
            "player_world_pos": self.player_world_pos,
            "scrolled": scrolled,
            "alignment": round(1.0 if coverage == 0 else matches / coverage, 3),
            "coverage": coverage,
            "conflicts": conflicts,
            "candidates": tried,
            "rejected_tiles": rejected,
            "alignment_status": "locked",
            "hero_candidates": [list(self.player_world_pos)],
            "alternatives": alternatives,
        }

    def _reveal_neighbours(
        self,
        screen_lines: list[str],
        player_screen: tuple[int, int],
        map_rows: tuple[int, int],
    ) -> None:
        """An ``?`` next to the player has just become real.

        NetHack draws everything adjacent to you, so a tile that is still blank
        on screen is solid rockwall and gets confirmed as ``X``.  A tile drawing
        terrain is remembered as that terrain, and a tile showing a monster or
        item is remembered as ground - nothing is ever drawn inside rockwall.

        tmux reports a row without its trailing blanks, so a position past the
        end of the line is a blank like any other: still rockwall, still settled.
        """
        first, last = window_rows(screen_lines, map_rows)
        prow, pcol = player_screen
        for dr, dc in DIR_KEYS:
            r, c = prow + dr, pcol + dc
            if not (first <= r <= last) or r >= len(screen_lines):
                continue
            drawn = screen_lines[r][c : c + 1]  # past the end of a trimmed row: blank
            if drawn != "" and drawn != " ":
                continue  # remembered as terrain, or as ground under something
            self.mark_solid(self.dungeon_offset[0] + r, self.dungeon_offset[1] + c)

    # ------------------------------------------------------------------ #
    #  queries
    # ------------------------------------------------------------------ #

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        if not self.tiles:
            return (0, 0, 0, 0)
        rows = [r for r, _ in self.tiles]
        cols = [c for _, c in self.tiles]
        return (min(rows), max(rows), min(cols), max(cols))

    def bounds_dict(self) -> dict[str, int]:
        min_r, max_r, min_c, max_c = self.bounds
        return {"min_row": min_r, "max_row": max_r, "min_col": min_c, "max_col": max_c}

    def remembered_count(self) -> int:
        return sum(1 for t in self.tiles.values() if t.memory or t.solid)

    def visited_count(self) -> int:
        return sum(1 for t in self.tiles.values() if t.visited)

    def reachable(self) -> dict[tuple[int, int], int]:
        """Walkable tiles the player can reach over *remembered* ground.

        Maps position -> number of steps from the player.  Unrevealed tiles are
        not part of this: a route through them would be a guess.
        """
        if self._reach_cache is not None:
            return self._reach_cache
        dist: dict[tuple[int, int], int] = {}
        if self.player_world_pos is not None:
            start = self.player_world_pos
            dist[start] = 0
            queue = deque([start])
            while queue:
                pos = queue.popleft()
                row, col = pos
                # the tile under the player may not be marked passable yet
                for dr, dc in DIR_KEYS:
                    npos = (row + dr, col + dc)
                    if npos in dist:
                        continue
                    tile = self.tiles.get(npos)
                    if tile is None or not tile.memory:
                        continue
                    if not self.step_ok(pos, npos):
                        continue
                    dist[npos] = dist[pos] + 1
                    queue.append(npos)
        self._reach_cache = dist
        return dist

    def frontier(self) -> set[tuple[int, int]]:
        """The ``?`` cells: unrevealed, but adjacent to ground we can reach.

        Only these are worth exploring: to learn what a tile is you have to
        stand next to it, so a blank whose neighbours are all walls, solid rock
        or unreachable can be ignored.
        """
        if self._frontier_cache is not None:
            return self._frontier_cache
        cells: set[tuple[int, int]] = set()
        for pos in self.reachable():
            row, col = pos
            for dr, dc in DIR_KEYS:
                npos = (row + dr, col + dc)
                tile = self.tiles.get(npos)
                if tile is None or (not tile.memory and not tile.solid):
                    cells.add(npos)
        self._frontier_cache = cells
        return cells

    def frontier_groups(self) -> list[dict[str, Any]]:
        """Blobs of ``?`` cells with the best tile to stand on to reveal them.

        Nearest, biggest target first.  ``route`` walks the player to ``stand``
        over ground we already remember and ``probe`` is the key that then pokes
        the blob into reality, so ``route + probe`` is one command that always
        learns something.  When the only way in is a door that was never opened,
        ``stand`` stays in front of it, ``door_ahead`` names it and the probe is
        an ``o`` + direction.
        """
        cells = set(self.frontier())
        reach = self.reachable()
        groups: list[dict[str, Any]] = []
        while cells:
            start = cells.pop()
            blob = [start]
            queue = deque([start])
            while queue:
                pos = queue.popleft()
                for dr, dc in DIR_KEYS:
                    npos = (pos[0] + dr, pos[1] + dc)
                    if npos in cells:
                        cells.remove(npos)
                        blob.append(npos)
                        queue.append(npos)

            # the reachable tile that overlooks the most of this blob
            coverage: dict[tuple[int, int], int] = {}
            for pos in blob:
                row, col = pos
                for dr, dc in DIR_KEYS:
                    vantage = (row + dr, col + dc)
                    if vantage in reach:
                        coverage[vantage] = coverage.get(vantage, 0) + 1
            if not coverage:
                continue
            # a shut door is not a tile to stand on: sort those vantages last,
            # we would rather poke from open ground
            def shut(p: tuple[int, int]) -> bool:
                tile = self.tiles.get(p)
                return bool(tile and tile.door_closed)

            stand = min(
                coverage,
                key=lambda p, cov=coverage, rch=reach: (
                    rch[p],
                    shut(p),
                    -cov[p],
                    p[0],
                    p[1],
                ),
            )
            door_ahead: tuple[int, int] | None = None
            overlook = coverage[stand]
            if shut(stand):
                # the only tile overlooking this blob is a shut door: stay on the
                # ground in front of it and offer to open the way instead
                door = stand
                before = [
                    p
                    for p in reach
                    if p != door
                    and max(abs(p[0] - door[0]), abs(p[1] - door[1])) == 1
                    and self.step_ok(p, door)
                    and not shut(p)
                ]
                if not before:
                    continue
                stand = min(before, key=lambda p: (reach[p], p[0], p[1]))
                door_ahead = door
                overlook = coverage[door]
            path = self.route_to(stand, through_unknown=False)
            if path is None:
                continue
            # the last key does the actual work: a step that makes a ``?`` real,
            # or the ``o`` that opens the door in front of us
            if door_ahead is not None:
                probe = "o" + DIR_KEYS[
                    (door_ahead[0] - stand[0], door_ahead[1] - stand[1])
                ]
            else:
                probe = next(
                    (
                        DIR_KEYS[(cell[0] - stand[0], cell[1] - stand[1])]
                        for cell in sorted(set(blob))
                        if 0 < max(abs(cell[0] - stand[0]), abs(cell[1] - stand[1])) <= 1
                    ),
                    "",
                )
            vantage = self.tiles.get(stand)
            ahead_tile = self.tiles.get(door_ahead) if door_ahead else None
            crossed = [self.tiles.get(p) for p in (path or [])]
            doors_on_route = [t for t in crossed if t is not None and t.doorway]
            needs_opening = any(t.door_closed for t in doors_on_route) or bool(
                ahead_tile and ahead_tile.door_closed
            )
            groups.append(
                {
                    "cells": len(blob),
                    "stand": list(stand),
                    "steps": reach[stand],
                    "route": self.route_keys(path, open_doors=True),
                    "probe": probe,
                    "seen_from_there": overlook,
                    "door_ahead": list(door_ahead) if door_ahead else None,
                    # a door means the way in may be locked or secret
                    "through_door": bool(doors_on_route)
                    or door_ahead is not None
                    or bool(vantage and vantage.doorway),
                    "door_shut": needs_opening,
                    "trap_on_route": any(t.trap for t in crossed if t is not None),
                    "on_stairs": bool(vantage and vantage.terrain in (UP, DOWN)),
                }
            )
        # cheap targets first; a door you have not opened yet costs a little
        # more than an open corridor, because it may well be locked
        groups.sort(key=lambda g: (g["steps"] + (1 if g["door_ahead"] else 0), -g["cells"]))
        return groups

    def rooms(self) -> list[Room]:
        """Connected regions of remembered floor ``.``.

        Cave floor and doors bound a room, so corridors never merge rooms.
        """
        if self._rooms_cache is not None:
            return self._rooms_cache

        rooms: list[Room] = []
        claimed: set[tuple[int, int]] = set()
        for start, tile in self.tiles.items():
            if start in claimed or tile.terrain != FLOOR:
                continue
            region: list[tuple[int, int]] = []
            doors: list[tuple[int, int]] = []
            seen_doors: set[tuple[int, int]] = set()
            queue = deque([start])
            claimed.add(start)
            while queue:
                row, col = queue.popleft()
                region.append((row, col))
                for dr, dc in DIR_KEYS:
                    npos = (row + dr, col + dc)
                    ntile = self.tiles.get(npos)
                    if ntile is None:
                        continue
                    if ntile.terrain == DOOR:
                        if npos not in seen_doors:
                            seen_doors.add(npos)
                            doors.append(npos)
                        continue
                    if ntile.terrain == FLOOR and npos not in claimed:
                        claimed.add(npos)
                        queue.append(npos)
            if len(region) >= 4:
                rooms.append(Room(id=len(rooms), tiles=region, doors=doors))

        self._rooms_cache = rooms
        return rooms

    def room_at(self, row: int, col: int) -> Room | None:
        for room in self.rooms():
            if room.contains((row, col)):
                return room
        return None

    # ------------------------------------------------------------------ #
    #  pathfinding
    # ------------------------------------------------------------------ #

    def has_door_leaf(self, pos: tuple[int, int]) -> bool:
        """True for a doorway that still has a door swinging in it.

        NetHack draws such a doorway ``+`` (shut), ``|`` or ``-`` (open); one
        that is just a hole in a wall is drawn ``.``.  Only the ones with a leaf
        in them refuse diagonal traffic.
        """
        tile = self.tiles.get(pos)
        return bool(tile and tile.doorway and tile.terrain in (DOOR, WALL_V, WALL_H))

    def _walkable(self, pos: tuple[int, int], through_unknown: bool) -> bool:
        tile = self.tiles.get(pos)
        if tile is None:
            return through_unknown
        if tile.solid:
            return False
        if not tile.memory:
            return through_unknown
        if tile.doorway:
            # a shut door is still the way through this wall: a route may use
            # it, and the route keys carry the 'o' that opens it
            return True
        return tile.passable

    def step_ok(
        self, frm: tuple[int, int], to: tuple[int, int], *, through_unknown: bool = True
    ) -> bool:
        """Whether one direction key can take the player from ``frm`` to ``to``."""
        dr, dc = to[0] - frm[0], to[1] - frm[1]
        if max(abs(dr), abs(dc)) != 1:
            return False
        if dr and dc and (self.has_door_leaf(to) or self.has_door_leaf(frm)):
            return False
        return self._walkable(to, through_unknown)

    def route_to(
        self, target: tuple[int, int], *, through_unknown: bool = True
    ) -> list[tuple[int, int]] | None:
        """Shortest remembered route from the player to ``target``.

        The route is cut at the first tile we have no memory of: beyond it the
        keys would be a guess.
        """
        if self.player_world_pos is None:
            return None
        if target == self.player_world_pos:
            return []

        prev: dict[tuple[int, int], tuple[int, int]] = {}
        seen = {self.player_world_pos}
        queue = deque([self.player_world_pos])
        while queue:
            pos = queue.popleft()
            if pos == target:
                break
            for dr, dc in DIR_KEYS:
                npos = (pos[0] + dr, pos[1] + dc)
                if npos in seen or not self.step_ok(
                    pos, npos, through_unknown=through_unknown
                ):
                    continue
                seen.add(npos)
                prev[npos] = pos
                queue.append(npos)

        if target not in prev:
            return None

        path: list[tuple[int, int]] = []
        cur = target
        while cur != self.player_world_pos:
            path.append(cur)
            cur = prev[cur]
        path.reverse()

        for index, pos in enumerate(path):
            tile = self.tiles.get(pos)
            if tile is None or (not tile.memory and not tile.solid):
                return path[:index]
        return path

    def route_keys(
        self, path: list[tuple[int, int]] | None, *, open_doors: bool = False
    ) -> str:
        """The vi-keys that walk ``path``.

        With ``open_doors`` every step that enters a door we know is still shut
        gets an ``o`` + direction in front of it: walking into a shut door only
        bumps, and an open door needs nothing (NetHack draws it as the wall it
        sits in, which is exactly how we remember it).
        """
        if not path:
            return ""
        assert self.player_world_pos is not None
        keys: list[str] = []
        cur = self.player_world_pos
        for pos in path:
            step = DIR_KEYS.get((pos[0] - cur[0], pos[1] - cur[1]), "?")
            tile = self.tiles.get(pos)
            if open_doors and step != "?" and tile is not None and tile.door_closed:
                keys.append(f"o{step}{step}")
            else:
                keys.append(step)
            cur = pos
        return "".join(keys)

    # ------------------------------------------------------------------ #
    #  rendering
    # ------------------------------------------------------------------ #

    def glyph_at(self, row: int, col: int) -> str:
        """Render one tile of the memory map."""
        if self.player_world_pos == (row, col):
            return "@"
        tile = self.tiles.get((row, col))
        if tile is not None:
            if tile.solid:
                return "X"
            if tile.doorway:
                # '+' is NetHack's own glyph for a door that is shut; an open
                # one is drawn as plain wall, so we mark the way through
                return "+" if tile.door_closed else "'"
            if tile.trap:
                return "^"
            if tile.memory:
                return tile.terrain or " "
        # never drawn: '?' only where we could actually walk up to it
        return "?" if (row, col) in self.frontier() else " "

    def render(self, padding: int = 3) -> str:
        if not self.tiles:
            return "(no memory yet)\n"
        positions = [
            pos
            for pos, tile in self.tiles.items()
            if tile.memory or tile.solid or tile.visited
        ]
        if not positions:
            return "(no memory yet)\n"
        # include the '?' frontier so it is never clipped away
        positions.extend(self.frontier())
        min_r = min(r for r, _ in positions) - padding
        max_r = max(r for r, _ in positions) + padding
        min_c = min(c for _, c in positions) - padding
        max_c = max(c for _, c in positions) + padding
        lines = [
            "".join(self.glyph_at(row, col) for col in range(min_c, max_c + 1))
            for row in range(min_r, max_r + 1)
        ]
        return "\n".join(lines) + "\n"

    def describe(self) -> dict[str, Any]:
        groups = self.frontier_groups()
        return {
            "level": self.level,
            "player_world_pos": list(self.player_world_pos) if self.player_world_pos else None,
            "bounds": self.bounds_dict(),
            "remembered_tiles": self.remembered_count(),
            "visited_tiles": self.visited_count(),
            "frontier_cells": sum(g["cells"] for g in groups),
            "frontier_areas": len(groups),
            "frontier_targets": groups[:6],
            "downstairs": [list(p) for p in sorted(self.downstairs)],
            "upstairs": [list(p) for p in sorted(self.upstairs)],
            "unsearched_walls": len(self.unsearched_wall_targets()),
            "rooms": [
                {
                    "id": room.id,
                    "bounds": list(room.bounds),
                    "size": len(room.tiles),
                    "doors": [list(d) for d in room.doors],
                }
                for room in self.rooms()
            ],
        }

    # ------------------------------------------------------------------ #
    #  persistence
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "player_world_pos": list(self.player_world_pos) if self.player_world_pos else None,
            "dungeon_offset": list(self.dungeon_offset),
            "downstairs": [list(p) for p in sorted(self.downstairs)],
            "upstairs": [list(p) for p in sorted(self.upstairs)],
            "searched": [list(p) for p in sorted(self.searched)],
            "tiles": {f"{r},{c}": t.to_dict() for (r, c), t in self.tiles.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DungeonMap:
        map_obj = cls(level=int(data.get("level", 1)))
        pos = data.get("player_world_pos")
        map_obj.player_world_pos = tuple(pos) if pos else None
        offset = data.get("dungeon_offset")
        map_obj.dungeon_offset = tuple(offset) if offset else (0, 0)
        # the old single-slot keys still in state files written before stairs
        # became a ledger: read both, prefer the lists
        for attr, key in (("downstairs", "downstairs"), ("upstairs", "upstairs")):
            entries = data.get(key)
            if entries is None:
                legacy = data.get(f"{key}_pos")
                entries = [legacy] if legacy else []
            getattr(map_obj, attr).update(tuple(p) for p in entries)
        map_obj.searched.update(tuple(p) for p in data.get("searched", []))
        for key, tile_data in data.get("tiles", {}).items():
            row, col = (int(v) for v in key.split(","))
            map_obj.tiles[(row, col)] = Tile.from_dict(tile_data)
        return map_obj
