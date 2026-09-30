"""Tests for the memory map: what is remembered, what stays '?'.

The rules under test:

* remembered geometry is exactly what a screen drew,
* a blank is never remembered as a wall,
* ``?`` only appears where the player could actually walk up to it,
* only bumping into a blank turns it into a confirmed ``X``,
* ``+`` is a shut door; at a known doorway the wall glyph means it stands open,
* a route never steps diagonally into or out of an intact doorway,
* the map does not drift when the viewport scrolls or the screen is flat.
"""


from nethackmcp.map import (
    CAVE,
    DOOR,
    FLOOR,
    LIQUID,
    UP,
    WALL_H,
    WALL_V,
    DungeonMap,
)

ROOM_SCREEN = [
    "-----------",
    "|.........|",
    "|....@....|",
    "|.........|",
    "-----+-----",
]


def build(lines, player=(2, 5)):
    """Apply a bare map screen (every row is map content)."""
    dungeon_map = DungeonMap(level=1)
    dungeon_map.apply_screen(lines, player, (0, len(lines) - 1))
    return dungeon_map


# --------------------------------------------------------------------------- #
#  memory
# --------------------------------------------------------------------------- #


def test_screen_geometry_is_remembered():
    dungeon_map = build(ROOM_SCREEN)
    assert dungeon_map.player_world_pos == (0, 0)
    assert dungeon_map.terrain_at(0, 0) == FLOOR  # under the player
    assert dungeon_map.terrain_at(-2, 0) == WALL_H
    assert dungeon_map.terrain_at(-1, -5) == WALL_V
    # the player was not standing there before, but the glyph is known
    assert dungeon_map.get_tile(-1, -1).memory
    assert not dungeon_map.get_tile(-1, -1).visited


def test_upstairs_are_passable():
    dungeon_map = build(
        [
            "-----------",
            "|....<....|",
            "|....@....|",
            "-----------",
        ]
    )
    assert dungeon_map.terrain_at(-1, 0) == UP
    assert dungeon_map.is_passable(-1, 0)
    assert dungeon_map.upstairs_pos == (-1, 0)


def test_downstairs_are_recorded():
    dungeon_map = build(
        [
            "-----------",
            "|....>....|",
            "|....@....|",
            "-----------",
        ]
    )
    assert dungeon_map.downstairs_pos == (-1, 0)
    assert dungeon_map.is_passable(-1, 0)


def test_blank_is_never_remembered_as_terrain():
    """Everything the screen leaves blank stays unknown until revealed."""
    lines = [
        "----------",
        "|.....---",  # no wall on the far right: blank beyond the room
        "|..@..",
        "|.....---",
        "----------",
    ]
    dungeon_map = build(lines, player=(2, 3))
    far_right = dungeon_map.get_tile(0, 8)
    assert far_right is None or not far_right.memory
    assert far_right is None or not far_right.solid


# --------------------------------------------------------------------------- #
#  the '?' frontier
# --------------------------------------------------------------------------- #


def test_frontier_only_contains_reachable_unknowns():
    lines = [
        "-----",
        "|.@.|",
        "-----",
    ]
    dungeon_map = build(lines, player=(1, 2))
    rendered = dungeon_map.render()
    # right above the player is floor, so nothing unknown is adjacent to it
    assert dungeon_map.frontier() == set()
    # the blank beyond the walls is unreachable: no memory, no '?'
    assert "?" not in rendered


def test_frontier_shows_the_way_out_of_a_door():
    """World coords: player at (0,0), the door south of it at (1,0)."""
    lines = [
        "-----",
        "|.@.|",
        "--+--",
        "     ",
    ]
    dungeon_map = build(lines, player=(1, 2))
    assert dungeon_map.terrain_at(1, 0) == DOOR
    # the door is walkable and remembered, so what lies beyond it is '?'
    assert (2, 0) in dungeon_map.frontier()
    assert "?" in dungeon_map.render()


def test_solid_rock_is_not_frontier():
    dungeon_map = build(["---", "|@|", "---"], player=(1, 1))
    dungeon_map.mark_solid(1, 2)  # east: confirmed rockwall
    assert (1, 2) not in dungeon_map.frontier()
    assert "X" in dungeon_map.render()


def test_bumping_a_blank_confirms_wall_and_removes_it_from_frontier():
    dungeon_map = build(ROOM_SCREEN)
    target = (0, -7)  # west of the room's left wall, never drawn
    assert dungeon_map.get_tile(*target) is None
    dungeon_map.mark_solid(*target)
    tile = dungeon_map.get_tile(*target)
    assert tile.solid and not tile.memory
    assert target not in dungeon_map.frontier()
    assert dungeon_map.glyph_at(*target) == "X"


# --------------------------------------------------------------------------- #
#  observing new terrain over old guesses
# --------------------------------------------------------------------------- #


def test_cave_floor_appears_where_a_blank_was():
    """A '?' that lights up as cave floor replaces the unknown."""
    dungeon_map = build(["--", "|@", "--"], player=(1, 1))
    dungeon_map.observe(1, 2, CAVE)
    assert dungeon_map.terrain_at(1, 2) == CAVE
    assert dungeon_map.is_passable(1, 2)
    assert dungeon_map.glyph_at(1, 2) == "#"


def test_seeing_terrain_overrules_a_previous_bump():
    dungeon_map = DungeonMap()
    dungeon_map.mark_solid(0, 0)
    dungeon_map.observe(0, 0, DOOR)
    assert not dungeon_map.get_tile(0, 0).solid


# --------------------------------------------------------------------------- #
#  rooms
# --------------------------------------------------------------------------- #


def test_two_rooms_are_not_merged():
    dungeon_map = build(
        [
            "----------",
            "|....|....|",
            "|.@..|....|",
            "|....|....|",
            "----------",
        ],
        player=(2, 2),
    )
    rooms = dungeon_map.rooms()
    assert len(rooms) == 2
    assert rooms[0].id != rooms[1].id
    here = dungeon_map.room_at(*dungeon_map.player_world_pos)
    assert here is not None
    # the player cannot route to the other side: no door was ever remembered
    other = next(r for r in rooms if r.id != here.id)
    assert dungeon_map.route_to(other.center, through_unknown=False) is None


def test_corridors_do_not_count_as_rooms():
    dungeon_map = build(
        [
            "#####",
            "#...#",
            "#####",
        ]
    )
    assert dungeon_map.rooms() == []


# --------------------------------------------------------------------------- #
#  alignment / odometry
# --------------------------------------------------------------------------- #


def test_identical_screen_does_not_move_the_map():
    dungeon_map = build(ROOM_SCREEN)
    first = dungeon_map.dungeon_offset
    dungeon_map.apply_screen(ROOM_SCREEN, (2, 5), (0, len(ROOM_SCREEN) - 1))
    assert dungeon_map.dungeon_offset == first


def test_viewport_scroll_follows_the_terrain():
    dungeon_map = build(ROOM_SCREEN)
    # same dungeon, shifted one column left on screen: the player moved east
    shifted = [line[1:] + "." for line in ROOM_SCREEN]
    dungeon_map.apply_screen(shifted, (2, 4), (0, len(shifted) - 1))
    assert dungeon_map.player_world_pos == (0, 0)  # unchanged: same tile


def test_keyboard_beats_matching_in_an_ambiguous_corridor():
    """One `l` step must move exactly one tile even with nothing to match on."""
    corridor = ["#########", "#.......#", "#########"]
    dungeon_map = build(corridor, player=(1, 1))
    # player walks east: screen keeps looking identical except for the @
    moved = ["#########", "#.@.....#", "#########"]
    dungeon_map.apply_screen(
        moved, (1, 2), (0, len(moved) - 1), expected_world=(0, 1)
    )
    assert dungeon_map.player_world_pos == (0, 1)


def test_unremembered_tiles_do_not_score():
    """Alignment must not be rewarded for 'explaining' tiles we never saw."""
    dungeon_map = build(ROOM_SCREEN)
    cells = DungeonMap.screen_cells(ROOM_SCREEN, (0, len(ROOM_SCREEN) - 1))
    agree, comparable, conflicts = dungeon_map.score_offset(cells, (500, 500))
    assert comparable == 0.0
    assert agree == 0.0
    assert conflicts == 0.0


def test_keyboard_is_refused_when_it_contradicts_memory():
    """An offset that would drop a remembered wall onto remembered floor is
    rejected: the keyboard only decides among alignments that are consistent."""
    dungeon_map = build(ROOM_SCREEN)
    info = dungeon_map.apply_screen(
        ROOM_SCREEN, (2, 5), (0, 4), expected_world=(0, -1)
    )
    assert dungeon_map.player_world_pos == (0, 0)  # not fooled
    assert info["conflicts"] == 0


def test_conflicting_screen_keeps_memory_instead_of_smearing():
    """A screen that contradicts memory never repaints what we already know."""
    dungeon_map = build(ROOM_SCREEN)
    # a hole punched in the middle of the remembered top wall: no shift can
    # explain it away, so the map must keep the wall and refuse the lie
    corrupt = list(ROOM_SCREEN)
    corrupt[0] = "----" + "." + "----"
    info = dungeon_map.apply_screen(corrupt, (2, 5), (0, len(corrupt) - 1))
    assert dungeon_map.terrain_at(-2, 0) == WALL_H, "remembered wall was overwritten"
    assert info["conflicts"] > 0
    assert info["rejected_tiles"] > 0  # the clashing glyph was refused


def test_message_rows_are_never_terrain():
    lines = [
        "You don't have that object.--More--",
        "",
        "-----------",
        "|....@....|",
        "-----------",
        "Virtuoos the Hatamoto   St:15 Dx:18",
        "Dlvl:1 $:10 HP:15(15)",
    ]
    dungeon_map = DungeonMap(level=1)
    dungeon_map.apply_screen(lines, (3, 5), (2, 4))
    # world (0,0) is the player; the wall row above is world (-1, .)
    assert dungeon_map.terrain_at(0, 0) == FLOOR
    assert dungeon_map.terrain_at(-1, 0) == WALL_H
    # nothing from the message row (screen row 0) may have entered the map
    assert dungeon_map.get_tile(-3, 0) is None
    assert dungeon_map.get_tile(-3, 5) is None


def test_no_question_mark_ever_touches_the_player():
    """Standing next to a '?' settles it: it becomes X or real terrain."""
    lines = [
        "-----",
        "|.@  ",  # the three tiles east of the player are drawn blank
        "-----",
    ]
    dungeon_map = build(lines, player=(1, 2))
    centre = dungeon_map.player_world_pos
    around = [
        (centre[0] + dr, centre[1] + dc)
        for dr in (-1, 0, 1)
        for dc in (-1, 0, 1)
        if (dr, dc) != (0, 0)
    ]
    for pos in around:
        glyph = dungeon_map.glyph_at(*pos)
        assert glyph in "X.#<>+|-", f"{pos} still renders {glyph!r}"
    # the blanks we were next to are now confirmed rockwall
    assert dungeon_map.glyph_at(0, 1) == "X"
    assert not (set(around) & dungeon_map.frontier())


def test_a_frontier_cell_becomes_real_terrain():
    """A '?' beyond a door turns into whatever the next screen draws there."""
    first = [
        "-------",
        "|...+  ",  # door at col 4; beyond it nothing is drawn yet
        "|..@...",
        "-------",
    ]
    dungeon_map = build(first, player=(2, 3))
    assert dungeon_map.terrain_at(-1, 1) == DOOR
    assert dungeon_map.glyph_at(-1, 2) == "?"  # unrevealed, but reachable

    second = [
        "-------",
        "|...+##",  # cave floor showed up past the door
        "|...@..",
        "-------",
    ]
    info = dungeon_map.apply_screen(
        second, (2, 4), (0, len(second) - 1), expected_world=(0, 1)
    )
    assert info["conflicts"] == 0
    assert dungeon_map.player_world_pos == (0, 1)
    assert dungeon_map.terrain_at(-1, 2) == CAVE  # the '?' is real now
    assert dungeon_map.is_passable(-1, 2)
    assert (-1, 2) not in dungeon_map.frontier()


def test_frontier_follows_a_move_onto_already_visited_ground():
    """Reachability is anchored on the player, not on the last tile change."""
    dungeon_map = DungeonMap()
    for col in range(5):
        dungeon_map.observe(0, col, FLOOR)
    for col in range(-1, 6):
        dungeon_map.observe(-1, col, WALL_H)
        dungeon_map.observe(1, col, WALL_H)
    dungeon_map.observe(0, -1, WALL_V)  # sealed behind, open ahead
    for col in range(5):
        dungeon_map.visit(0, col)  # the corridor is fully explored

    dungeon_map.set_player_position((0, 0))
    group = dungeon_map.frontier_groups()[0]
    assert (group["steps"], group["route"]) == (4, "llll")
    assert group["probe"] == "l"  # the key that pokes the blob once you are there

    # walk to the far end: nothing new is remembered there, but the frontier
    # must follow the player - now the whole route is the poke itself
    dungeon_map.set_player_position((0, 4))
    dungeon_map.visit(0, 4)
    group = dungeon_map.frontier_groups()[0]
    assert (group["steps"], group["route"]) == (0, "")
    assert group["stand"] == [0, 4]
    assert group["probe"] == "l"


def test_door_state_follows_the_glyph():
    """``+`` is a SHUT door and the wall glyph at a known doorway means it is
    standing open: the map has to remember which of the two it is."""
    shut_frame = [
        "-----",
        "|..+ ",  # shut door in the east side, unrevealed beyond it
        "|.@..|",
        "-----",
    ]
    dungeon_map = build(shut_frame, player=(2, 2))
    door = (dungeon_map.dungeon_offset[0] + 1, dungeon_map.dungeon_offset[1] + 3)
    tile = dungeon_map.get_tile(*door)
    assert tile.doorway and tile.door_closed
    assert dungeon_map.glyph_at(*door) == "+"

    open_frame = [
        "-----",
        "|..| ",  # same position, drawn as wall: the door swung open
        "|.@..|",
        "-----",
    ]
    info = dungeon_map.apply_screen(open_frame, (2, 2), (0, len(open_frame) - 1))
    assert info["conflicts"] == 0, "a door opening is not a contradiction"
    tile = dungeon_map.get_tile(*door)
    assert tile.doorway and tile.door_open
    assert dungeon_map.glyph_at(*door) == "'"


def test_standing_in_a_doorway_proves_it_open():
    """``olo`` in one keystroke run: we never saw the door swing, but we are
    standing in it, so it cannot be shut any more."""
    dungeon_map = DungeonMap()
    dungeon_map.observe(0, 0, FLOOR)
    dungeon_map.observe(0, 1, DOOR)
    dungeon_map.player_world_pos = (0, 0)
    assert dungeon_map.get_tile(0, 1).door_closed

    dungeon_map.set_player_position((0, 1))
    dungeon_map.visit(0, 1)
    assert dungeon_map.get_tile(0, 1).door_open
    assert not dungeon_map.get_tile(0, 1).door_closed


def test_frontier_behind_a_shut_door_is_still_offered():
    """The only way in is the door: shutting it must not hide the target."""
    dungeon_map = DungeonMap()
    for col in range(3):
        dungeon_map.observe(0, col, FLOOR)
    dungeon_map.observe(0, 3, DOOR)  # the only way on
    for col in range(-1, 5):  # walls above, below and behind the corridor
        dungeon_map.observe(-1, col, WALL_H)
        dungeon_map.observe(1, col, WALL_H)
    dungeon_map.observe(0, -1, WALL_V)
    dungeon_map.player_world_pos = (0, 0)
    dungeon_map.visit(0, 0)

    assert dungeon_map.frontier() == {(0, 4)}
    groups = dungeon_map.frontier_groups()
    assert groups and groups[0]["through_door"]
    assert groups[0]["stand"] == [0, 2]  # in front of the door, not on it
    assert groups[0]["door_ahead"] == [0, 3]
    assert groups[0]["probe"] == "ol"  # the key that opens the way in
    assert groups[0]["door_shut"], "'+' is a shut door, and we know it"

    # the door swings open: it is drawn as the wall it sits in, still a doorway,
    # and now a tile we can actually stand on to see beyond it
    dungeon_map.observe(0, 3, WALL_V)
    groups = dungeon_map.frontier_groups()
    assert groups, "an open door must not hide what lies beyond it"
    assert not any(g["door_shut"] for g in groups)
    best = groups[0]
    assert best["stand"] == [0, 3]
    assert (best["steps"], best["route"], best["probe"]) == (3, "lll", "l")
    assert (0, 4) in dungeon_map.frontier()


def test_adjacent_blank_becomes_confirmed_wall():
    """An ``?`` next to the player that stays blank on screen is rockwall."""
    lines = [
        "-----",
        "|..@ ",  # the two tiles east of the player are drawn blank
        "-----",
    ]
    dungeon_map = build(lines, player=(1, 3))
    assert dungeon_map.glyph_at(0, 1) == "X"  # directly east: blank while adjacent
    # ...and it is no longer offered as something to explore
    assert (0, 1) not in dungeon_map.frontier()


def test_monster_tile_is_ground_not_wall():
    """'d' is a dog: it stands on walkable ground, so the tile is not a wall."""
    lines = [
        "-----",
        "|..@d",  # 'd' is a dog, not geometry
        "-----",
    ]
    dungeon_map = build(lines, player=(1, 3))
    tile = dungeon_map.get_tile(0, 1)
    assert tile is not None and tile.memory
    assert tile.passable
    # and it is therefore not offered as something left to discover
    assert (0, 1) not in dungeon_map.frontier()


def test_item_tile_is_remembered_as_floor():
    lines = [
        "-------",
        "|..$..|",
        "|..@..|",
        "-------",
    ]
    dungeon_map = build(lines, player=(2, 3))
    assert dungeon_map.terrain_at(-1, -1) == FLOOR  # the gold was standing there
    assert dungeon_map.is_passable(-1, -1)


def test_liquid_is_known_but_not_walkable():
    lines = [
        "-------",
        "|..}..|",
        "|..@..|",
        "-------",
    ]
    dungeon_map = build(lines, player=(2, 3))
    assert dungeon_map.terrain_at(-1, 0) == LIQUID  # directly above the player
    assert not dungeon_map.is_passable(-1, 0)
    assert dungeon_map.glyph_at(-1, 0) == "}"
    # known terrain is not an exploration target, even though it blocks the way
    assert (-1, 0) not in dungeon_map.frontier()


def test_spotted_trap_stays_floor_but_is_remembered():
    """``^`` is a trap on top of ground: the tile is walkable, and dangerous."""
    lines = [
        "-------",
        "|..^..|",
        "|..@..|",
        "-------",
    ]
    dungeon_map = build(lines, player=(2, 3))
    tile = dungeon_map.get_tile(-1, 0)
    assert tile is not None and tile.memory and tile.passable
    assert tile.trap
    assert dungeon_map.glyph_at(-1, 0) == "^"


def test_frontier_beyond_a_door_is_offered():
    """Two steps away, unseen: that is exactly what '?' is for."""
    lines = [
        "-----",
        "|.@.|",
        "--+--",
        "     ",
    ]
    dungeon_map = build(lines, player=(1, 2))
    groups = dungeon_map.frontier_groups()
    assert groups, "a doorway should leave something to explore"
    nearest = groups[0]
    # the door is the only tile overlooking that blob, so the entry keeps the
    # player on ground we can stand on and offers to open the way
    assert nearest["stand"] == [0, 0]
    assert (nearest["steps"], nearest["route"]) == (0, "")
    assert nearest["door_ahead"] == [1, 0]
    assert nearest["probe"] == "oj"
    assert nearest["through_door"]


# --------------------------------------------------------------------------- #
#  routes
# --------------------------------------------------------------------------- #


def test_route_stops_at_unremembered_ground():
    dungeon_map = build(ROOM_SCREEN)
    path = dungeon_map.route_to((0, 4))
    assert dungeon_map.route_keys(path) == "llll"
    # a target outside all memory yields an empty, useless route
    far = dungeon_map.route_to((30, 30))
    assert far == [] or far is None or dungeon_map.route_keys(far).count("?") == 0


def test_route_around_a_wall():
    dungeon_map = build(
        [
            "-------",
            "|..|..|",
            "|..|..|",
            "|..|..|",
            "-------",
        ]
    )
    left = (0, -2)  # inside the left half
    right = (0, 2)
    dungeon_map.visit(*left)
    dungeon_map.player_world_pos = left
    path = dungeon_map.route_to(right, through_unknown=False)
    assert path is None  # the wall divides the two halves, no door remembered


def test_route_opens_a_shut_door_on_the_way():
    """Walking into a shut door is a bump, so the keys have to open it first."""
    dungeon_map = DungeonMap()
    for col in (0, 1, 3, 4):
        dungeon_map.observe(0, col, FLOOR)
        dungeon_map.visit(0, col)
    dungeon_map.observe(0, 2, DOOR)  # '+' on screen: the door is shut
    for col in range(-1, 6):
        dungeon_map.observe(-1, col, WALL_H)
        dungeon_map.observe(1, col, WALL_H)
    dungeon_map.player_world_pos = (0, 0)

    path = dungeon_map.route_to((0, 4), through_unknown=False)
    assert dungeon_map.route_keys(path) == "llll"
    # 'ol' is the argument to open, the key after it is the step through
    assert dungeon_map.route_keys(path, open_doors=True) == "lollll"

    # once the door has been seen standing open it needs no 'o'
    dungeon_map.observe(0, 2, WALL_H)
    dungeon_map.visit(0, 2)
    assert dungeon_map.route_keys(dungeon_map.route_to((0, 4)), open_doors=True) == "llll"


def test_diagonals_never_use_an_intact_doorway():
    """NetHack refuses diagonal steps into or out of a doorway with a door."""
    dungeon_map = DungeonMap()
    dungeon_map.observe(0, 0, FLOOR)
    dungeon_map.observe(1, 1, DOOR)
    dungeon_map.observe(2, 2, FLOOR)
    dungeon_map.observe(2, 1, FLOOR)

    assert dungeon_map.step_ok((0, 0), (1, 1)) is False  # diagonal in
    assert dungeon_map.step_ok((1, 1), (2, 2)) is False  # diagonal out
    assert dungeon_map.step_ok((2, 1), (1, 1)) is True  # straight at it
    # a doorless doorway (drawn '.') lets diagonals through
    dungeon_map.observe(1, 1, FLOOR)
    assert dungeon_map.step_ok((0, 0), (1, 1)) is True


def test_frontier_target_reports_its_size():
    lines = [
        "-----",
        "|.@.|",
        "--+--",
        "     ",
    ]
    dungeon_map = build(lines, player=(1, 2))
    groups = dungeon_map.frontier_groups()
    assert sum(g["cells"] for g in groups) == len(dungeon_map.frontier())
    for group in groups:
        assert group["seen_from_there"] >= 1


# --------------------------------------------------------------------------- #
#  persistence
# --------------------------------------------------------------------------- #


def test_state_round_trip():
    dungeon_map = build(ROOM_SCREEN)
    dungeon_map.mark_solid(-5, 7)  # blank space beyond the drawn room
    dungeon_map.observe(0, 4, DOOR)
    dungeon_map.observe(-1, -1, FLOOR)
    dungeon_map.tiles[(-1, -1)].trap = True
    restored = DungeonMap.from_dict(dungeon_map.to_dict())
    assert restored.render() == dungeon_map.render()
    assert restored.dungeon_offset == dungeon_map.dungeon_offset
    assert restored.player_world_pos == dungeon_map.player_world_pos
    assert restored.get_tile(-5, 7).solid
    assert restored.get_tile(0, 4).door_closed  # a shut door stays shut
    assert restored.get_tile(-1, -1).trap
