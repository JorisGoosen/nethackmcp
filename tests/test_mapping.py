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
    DOWN,
    FLOOR,
    LIQUID,
    UP,
    WALL_H,
    WALL_V,
    DungeonMap,
    Tile,
    route_displacement,
    route_tokens,
    tile_matches,
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
    dungeon_map = build(["#########", "#@......#", "#########"], player=(1, 1))
    # player walks east: screen keeps looking identical except for the @
    moved = ["#########", "#.@.....#", "#########"]
    dungeon_map.apply_screen(
        moved, (1, 2), (0, len(moved) - 1), expected_world=(0, 1)
    )
    assert dungeon_map.player_world_pos == (0, 1)


def test_unremembered_tiles_do_not_score():
    """Alignment must not be rewarded for 'explaining' tiles we never saw."""
    dungeon_map = build(ROOM_SCREEN)
    index = dungeon_map.remembered_by_row()
    matches, conflicts, coverage = dungeon_map.score_offset(
        ROOM_SCREEN, (0, len(ROOM_SCREEN) - 1), (500, 500), index, max(len(l) for l in ROOM_SCREEN)
    )
    assert coverage == 0  # the window does not even overlap memory
    assert matches == 0
    assert conflicts == 0


def test_keyboard_is_refused_when_it_contradicts_memory():
    """An offset that would drop a remembered wall onto remembered floor is
    rejected: the keyboard only decides among alignments that are consistent."""
    dungeon_map = build(ROOM_SCREEN)
    info = dungeon_map.apply_screen(
        ROOM_SCREEN, (2, 5), (0, 4), expected_world=(0, -1)
    )
    assert dungeon_map.player_world_pos == (0, 0)  # not fooled
    assert info["conflicts"] == 0


# --------------------------------------------------------------------------- #
#  what the alignment is allowed to believe
# --------------------------------------------------------------------------- #


def remembered(lines, player=(2, 5)):
    dungeon_map = build(lines, player)
    return dungeon_map, dungeon_map.remembered_by_row()


def test_remembered_terrain_that_stops_being_drawn_is_a_conflict():
    """The game never forgets terrain it showed you: memory that lands on a
    blank says this alignment is wrong, and that is the strongest signal there
    is."""
    dungeon_map, index = remembered(ROOM_SCREEN)
    blanked = [line[:5] + "     " for line in ROOM_SCREEN]
    matches, conflicts, coverage = dungeon_map.score_offset(
        blanked, (0, 4), dungeon_map.dungeon_offset, index, 11
    )
    assert coverage == 55
    assert conflicts > 20, "a screen that erased half the room scored as a match"
    assert matches < conflicts


def test_confirmed_rockwall_is_allowed_to_sit_on_a_blank():
    """``X`` is what blank looks like: agreeing with nothing drawn is exactly
    right, so it must not be counted against an alignment."""
    dungeon_map, _ = remembered(ROOM_SCREEN)
    dungeon_map.mark_solid(-2, 6)  # beyond the east wall, never drawn
    tile = dungeon_map.get_tile(-2, 6)
    assert tile.solid and not tile.memory
    assert tile_matches(" ", tile) == 1


def test_the_player_tile_matches_whatever_ground_it_stands_on():
    """``@`` hides the tile under it, so it has to agree with floor, corridor,
    water, a stair and either state of a door - and with no wall."""
    for terrain in (FLOOR, CAVE, LIQUID, UP, DOWN):
        assert tile_matches("@", Tile(terrain=terrain)) == 1, terrain
    assert tile_matches("@", Tile(terrain=WALL_V)) == 0
    assert tile_matches("@", Tile(terrain=WALL_H, doorway=True)) == 1
    assert tile_matches("@", Tile(solid=True)) == -1  # rockwall we bumped into
    assert tile_matches("@", Tile(terrain=CAVE, solid=True)) == -1  # iron bars
    assert tile_matches("@", Tile()) == 0  # never seen: the hero is new ground


def test_an_open_door_matches_a_shut_one_and_its_wall():
    """One doorway, drawn three different ways depending on its state."""
    door = Tile(terrain=DOOR, doorway=True)
    assert tile_matches("|", door) == 1  # swung open: drawn as the wall in it
    assert tile_matches("+", door) == 1
    assert tile_matches(".", door) == 1  # the door is gone: a doorway still
    assert tile_matches("#", door) == -1


def test_a_monster_standing_there_proves_ground_not_wall():
    """Any glyph we do not track is something standing on a tile, so it can
    never agree with rockwall or iron bars - and it cannot outvote a remembered
    wall either, because an open door is drawn as the wall it sits in."""
    assert tile_matches("d", Tile(terrain=CAVE)) == 1
    assert tile_matches("$", Tile(terrain=FLOOR)) == 1
    assert tile_matches("Z", Tile(terrain=WALL_H)) == 0  # may be an open door
    assert tile_matches("Z", Tile(terrain=WALL_H, doorway=True)) == 1  # it is one
    assert tile_matches("d", Tile(solid=True)) == -1
    # and the guess does not overwrite the doorway it is standing in
    dungeon_map = DungeonMap()
    dungeon_map.observe(0, 0, DOOR)
    dungeon_map.observe_guess(0, 0)
    assert dungeon_map.terrain_at(0, 0) == DOOR


def test_walking_into_an_open_door_learns_it_was_a_doorway():
    """A ``|`` we only ever saw standing open is still a way through: getting
    inside it settles the question, and the tile must stop being a wall."""
    room = [
        "------",
        "|....|",
        "|...@|",  # the hero inside; the west wall is one '|' at col 0
        "|....|",
    ]
    dungeon_map = build(room, player=(2, 4))
    wall = (0, -4)  # world position of that '|' in the hero's row
    assert dungeon_map.terrain_at(*wall) == WALL_V
    assert not dungeon_map.get_tile(*wall).doorway

    stepped_in = [
        "------",
        "|....|",
        "@....|",  # the hero is standing in it now: it was an open door
        "|....|",
    ]
    info = dungeon_map.apply_screen(stepped_in, (2, 0), (0, 3), expected_world=wall)
    assert info["conflicts"] == 0, "standing in a doorway is not a contradiction"
    tile = dungeon_map.get_tile(*wall)
    assert tile.doorway and tile.door_open and tile.visited
    assert dungeon_map.is_passable(*wall)


def test_an_inferred_floor_never_disagrees_with_the_real_glyph():
    """We guessed 'floor' because the hero was standing there.  When the game
    finally draws that tile as a corridor it has not contradicted us."""
    dungeon_map = build(["#########", "#@......#", "#########"], player=(1, 1))
    tile = dungeon_map.get_tile(*dungeon_map.player_world_pos)
    assert tile.terrain == FLOOR and tile.guessed
    assert tile_matches("#", tile) == 1
    assert tile_matches("}", tile) == 1
    assert tile_matches("|", tile) == 1  # even a wall: we never saw this tile


def test_an_untested_alignment_is_not_a_match():
    """An offset that puts the window off the edge of memory contradicts nothing
    only because it looks at nothing."""
    dungeon_map, index = remembered(ROOM_SCREEN)
    far = (dungeon_map.dungeon_offset[0] + 9, dungeon_map.dungeon_offset[1] + 9)
    _, _, coverage = dungeon_map.score_offset(ROOM_SCREEN, (0, 4), far, index, 11)
    assert coverage == 0
    solved = dungeon_map.solve_offset(ROOM_SCREEN, (0, 4), (2, 5))
    assert solved["offset"] == dungeon_map.dungeon_offset


def test_the_search_starts_where_the_keys_say_and_stops_at_the_first_clean_fit():
    """A whole route is one command, so the starting point has to be the end of
    the route, not the tile we left."""
    assert route_displacement("hhhy") == (-1, -4)
    assert route_displacement("jnnjj") == (5, 2)
    assert route_displacement("mh") == (0, -1)
    assert route_displacement("olo") is None  # opens a door: not walking
    assert route_displacement("H") is None  # a run key travels an unknown way
    assert route_displacement("ff") is None


def test_a_row_trimmed_by_tmux_is_still_a_blank():
    """tmux reports a row without its trailing blanks, so a tile next to us that
    is past the end of the line was drawn *nothing* - which means rockwall, not
    'still unknown'."""
    room = [
        "|....",
        "|.@",  # this row stops after the hero: east of it nothing was drawn
        "|....",
    ]
    dungeon_map = build(room, player=(1, 2))
    here = dungeon_map.player_world_pos
    assert dungeon_map.glyph_at(here[0], here[1] + 1) == "X"
    assert (here[0], here[1] + 1) not in dungeon_map.frontier()
    # the very same column drawn on a longer row next door is floor, not wall
    assert dungeon_map.glyph_at(here[0] + 1, here[1] + 1) == "."


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
#  open doors, recognised from the wall around them
# --------------------------------------------------------------------------- #


def test_open_door_recognised_between_confirmed_rock():
    """The live shape: a '|' in a room's bottom wall with its wall neighbours
    never drawn but confirmed rockwall where the hero bumped them, room floor
    above, corridor below.  Only ever seen open, it is still the way in."""
    dungeon_map = build(
        [
            " ... ",
            "  |  ",
            "#####",
        ],
        player=(2, 2),
    )
    door = (-1, 0)  # the '|' is world-adjacent to the corridor the hero walks
    tile = dungeon_map.get_tile(*door)
    assert tile.doorway, "wall flanks over ground above and below: it is a door"
    assert tile.door_open and not tile.door_closed
    assert dungeon_map.is_passable(*door)
    assert dungeon_map.glyph_at(*door) == "'"  # rendered as the open doorway

    # the route into the room now runs through it
    assert dungeon_map.route_to((-2, 0)) == [door, (-2, 0)]
    # ... but not diagonally: NetHack refuses that into an intact doorway
    assert not dungeon_map.step_ok((0, -1), door)


def test_imaginary_room_with_four_doors():
    """A room whose doors all stand open; every other wall tile must stay a
    wall - corners included."""
    dungeon_map = build(
        [
            "---|-",
            "-...|",
            "|...-",
            "---|-",
        ],
        player=(1, 2),
    )
    doors = {pos for pos, tile in dungeon_map.tiles.items() if tile.doorway}
    # world = screen minus the player's screen position (1, 2)
    assert doors == {(0 - 1, 3 - 2), (1 - 1, 0 - 2), (2 - 1, 4 - 2), (3 - 1, 3 - 2)}


def test_plain_room_walls_are_never_inferred_as_doors():
    """A whole room drawn at once: the only doorway is the shut '+'."""
    dungeon_map = build(ROOM_SCREEN)
    doors = {pos for pos, tile in dungeon_map.tiles.items() if tile.doorway}
    assert doors == {(2, 0)}  # the '+' in the bottom wall


def test_unseen_flanks_are_no_evidence_until_a_bump_proves_them():
    """A blank says nothing - it is rockwall only once something confirms
    it.  The door is recognised the moment the bumps complete its setting."""
    dungeon_map = DungeonMap(level=1)
    dungeon_map.observe(-2, 0, FLOOR)  # the room beyond
    dungeon_map.observe(-1, 0, WALL_V)  # the door, seen only open so far
    dungeon_map.observe(0, 0, CAVE)  # the corridor below
    assert not dungeon_map.get_tile(-1, 0).doorway

    dungeon_map.mark_solid(-1, -1)  # bumped: confirmed rock, one flank
    assert not dungeon_map.get_tile(-1, 0).doorway
    dungeon_map.mark_solid(-1, 1)  # ... and the other
    assert dungeon_map.get_tile(-1, 0).doorway


def test_confirmed_rock_where_the_door_should_lead_refuses_it():
    """Ground on the passage axis may be unseen but never confirmed rock:
    with rock above, this '|' is masonry, not a doorway."""
    dungeon_map = DungeonMap(level=1)
    dungeon_map.observe(-1, 0, WALL_V)
    dungeon_map.mark_solid(-2, 0)  # above: solid
    dungeon_map.observe(0, 0, CAVE)  # below: corridor
    dungeon_map.mark_solid(-1, -1)
    dungeon_map.mark_solid(-1, 1)
    assert not dungeon_map.get_tile(-1, 0).doorway
    assert not dungeon_map.is_passable(-1, 0)


def test_a_known_door_never_begets_another_one():
    """The corner beside a walked-through door: its passage axis runs into
    that doorway.  A door is neither wall evidence nor ground - counting
    one either way would let an inference creep from a known doorway along
    the wall beside it, one tile per pass."""
    dungeon_map = DungeonMap(level=1)
    dungeon_map.observe(0, -1, DOOR)  # a doorway we know first-hand
    dungeon_map.observe(0, -2, FLOOR)  # the room behind it
    dungeon_map.observe(0, 1, CAVE)  # corridor around on the east
    dungeon_map.observe(-1, 0, WALL_V)  # the wall run the corner joins
    dungeon_map.observe(0, 0, WALL_H)  # the corner itself
    dungeon_map.mark_solid(1, 0)  # rock below, bumped from the corridor
    # flanks N/S: '|' and X - evidence.  passages W/E: the door and '#'.
    assert dungeon_map.get_tile(0, -1).doorway  # unchanged, it is a door
    assert not dungeon_map.get_tile(0, 0).doorway, "doorways do not beget doors"
    assert not dungeon_map.is_passable(0, 0)


def test_a_corner_is_never_a_doorway():
    """The live bug: the room's '|' wall turned a corner and the rock outside
    it was confirmed by a bump.  Masonry on one flank and rock on the other
    is the end of a wall, not a door - but both flanks *were* evidence, and
    with room floor on one side of the passage axis and never-seen blank on
    the other, the old rule called it one and the player kicked it for turns."""
    # '-' corner: '|' wall coming down to it, confirmed rock below it,
    # room floor west, the never-seen outside east (level 2, (3,-21))
    masonry_meets_rock = DungeonMap(level=1)
    masonry_meets_rock.observe(-1, 0, WALL_V)
    masonry_meets_rock.observe(0, 0, WALL_H)
    masonry_meets_rock.observe(0, -1, FLOOR)
    masonry_meets_rock.mark_solid(1, 0)
    assert not masonry_meets_rock.get_tile(0, 0).doorway
    assert not masonry_meets_rock.is_passable(0, 0)

    # the mirrored corner (level 2, (8,-25)): rock above, '|' wall below
    rock_above_wall = DungeonMap(level=1)
    rock_above_wall.observe(0, 0, WALL_H)
    rock_above_wall.observe(1, 0, WALL_V)
    rock_above_wall.observe(0, 1, FLOOR)
    rock_above_wall.mark_solid(-1, 0)
    assert not rock_above_wall.get_tile(0, 0).doorway

    # and the same turn drawn as '|': the horizontal wall running east from
    # the corner, rock confirmed west of it, room floor north
    turned_corner = DungeonMap(level=1)
    turned_corner.observe(-1, 0, FLOOR)
    turned_corner.observe(0, 0, WALL_V)
    turned_corner.observe(0, 1, WALL_H)
    turned_corner.mark_solid(0, -1)
    assert not turned_corner.get_tile(0, 0).doorway


def test_a_mistaken_corner_doorway_is_retracted():
    """State saved before flanks had to agree still carries the corner
    flagged as a door.  Inference re-reads it: contradictory flanks retract
    the flag - but a door we stood in keeps it whatever the wall claims."""
    dungeon_map = DungeonMap(level=1)
    dungeon_map.observe(-1, 0, WALL_V)
    dungeon_map.observe(0, 0, WALL_H)
    dungeon_map.observe(0, -1, FLOOR)
    dungeon_map.mark_solid(1, 0)  # completes the old rule's evidence
    dungeon_map.get_tile(0, 0).doorway = True  # what the buggy inference stored
    dungeon_map.infer_doorways()
    assert not dungeon_map.get_tile(0, 0).doorway, "the corner is not a door"

    # proven doors are beyond retraction: standing in it settles everything
    dungeon_map.get_tile(0, 0).doorway = True
    dungeon_map.get_tile(0, 0).visited = True
    dungeon_map.infer_doorways()
    assert dungeon_map.get_tile(0, 0).doorway, "we walked through that doorway"


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


# --------------------------------------------------------------------------- #
#  route parsing for key-by-key walking
# --------------------------------------------------------------------------- #


def test_route_tokens_splits_directions_and_door_openers():
    assert route_tokens("hhb") == ["h", "h", "b"]
    assert route_tokens("hhol") == ["h", "h", "ol"]
    assert route_tokens("ol") == ["ol"]


def test_route_tokens_refuses_anything_that_is_not_walking():
    assert route_tokens("olo") is None  # a lone 'o' cannot end a route
    assert route_tokens("20s") is None  # a count repeats an unknown number of times
    assert route_tokens("H") is None  # a run key travels until it likes
    assert route_tokens("dh") is None  # 'd' is drop, not a step south
    assert route_tokens("") is None


# --------------------------------------------------------------------------- #
#  two @ on one screen: who is the hero
# --------------------------------------------------------------------------- #

# two rooms joined by a doorway; shopkeeper stands in the right-hand room
TWO_AT_SCREEN = [
    "---------------",
    "|.....|.......|",
    "|.....+...@...|",
    "|..@..|.......|",
    "|.....|.......|",
    "---------------",
]
HERO_AT = (3, 3)
KEEPER_AT = (2, 10)


def build_two_at():
    dungeon_map = DungeonMap(level=1)
    dungeon_map.apply_screen(
        TWO_AT_SCREEN, HERO_AT, (0, len(TWO_AT_SCREEN) - 1),
        player_candidates=[HERO_AT],
    )
    return dungeon_map


def test_the_at_that_memory_and_keys_agree_on_is_the_hero():
    """A shopkeeper above-left of the hero is the first @ on screen and used
    to be *the* @ - memory and continuity must pick the real one anyway."""
    dungeon_map = build_two_at()
    home = dungeon_map.player_world_pos
    assert dungeon_map.get_tile(*home).visited
    # re-read the very same screen with both @ offered: nothing moved, so
    # nothing moves - continuity with the committed position decides
    info = dungeon_map.apply_screen(
        TWO_AT_SCREEN, KEEPER_AT, (0, len(TWO_AT_SCREEN) - 1),
        player_candidates=[KEEPER_AT, HERO_AT],
    )
    assert info["alignment_status"] == "locked"
    assert dungeon_map.player_world_pos == home


def test_alternating_expectations_never_walk_the_hero_onto_the_keeper():
    """The D3 shop brick: every re-read flipped YOU ARE between two readings.
    A swallowed key must not be able to teleport the hero onto the standing
    @ - the sticky alignment keeps its ground through a lying expectation."""
    dungeon_map = build_two_at()
    home = dungeon_map.player_world_pos
    remembered = dungeon_map.remembered_count()
    for expected in (
        (home[0], home[1] + 1),
        home,
        (home[0], home[1] + 1),
        None,
    ):
        dungeon_map.apply_screen(
            TWO_AT_SCREEN, KEEPER_AT, (0, len(TWO_AT_SCREEN) - 1),
            expected_world=expected,
            player_candidates=[KEEPER_AT, HERO_AT],
        )
        assert dungeon_map.player_world_pos == home
    assert dungeon_map.remembered_count() == remembered  # and no duplicated room


def test_a_screen_that_cannot_tell_two_at_apart_says_so():
    """Hero teleported left and a monster @ arrived on the right: from this
    one screen both readings are perfect and continuity cannot pick.  The
    merge must refuse to rewrite memory and report both candidates."""
    cave = ["#########", "#########", "#########"]
    start = [cave[0], "####@####", cave[2]]  # hero at (1, 4)
    dungeon_map = DungeonMap(level=1)
    dungeon_map.apply_screen(start, (1, 4), (0, 2), player_candidates=[(1, 4)])
    home = dungeon_map.player_world_pos
    remembered = dungeon_map.remembered_count()

    twins = [cave[0], "###@#@###", cave[2]]  # @ at (1, 3) and (1, 5)
    info = dungeon_map.apply_screen(
        twins, (1, 3), (0, 2), player_candidates=[(1, 3), (1, 5)]
    )
    assert info["alignment_status"] == "ambiguous"
    assert dungeon_map.player_world_pos == home  # committed position kept
    assert dungeon_map.remembered_count() == remembered  # memory untouched
    assert len(info["hero_candidates"]) == 2

    # the next look settles it: one @ is left where it moved to, memory
    # re-anchors on it and the hero follows
    stepped = [cave[0], "##@######", cave[2]]  # single @ at (1, 2)
    dungeon_map.apply_screen(stepped, (1, 2), (0, 2), player_candidates=[(1, 2)])
    assert dungeon_map.player_world_pos[1] == home[1] - 2


# --------------------------------------------------------------------------- #
#  what is *not* evidence of ground
# --------------------------------------------------------------------------- #


def test_the_last_heard_marker_is_not_ground():
    """'I' marks where an unseen monster was heard - sound reaches through
    rockwall - so it must never become remembered floor."""
    dungeon_map = build(["-------", "|..@.I|", "-------"], player=(1, 3))
    tile = dungeon_map.get_tile(1 - 1, 5 - 3)  # screen (1,5) -> world (0, 2)
    assert tile is None or (not tile.memory and not tile.solid)


def test_a_wrong_guess_retires_instead_of_becoming_a_conflict():
    """A '*' zap drawn over rockwall guessed the tile as floor.  Standing
    next to it and seeing blank now settles it: no conflict, guess retired,
    confirmed X."""
    dungeon_map = build(["---------", "|..@*...|", "---------"], player=(1, 3))
    world = (1 - 1, 4 - 3)  # screen (1, 4) -> world (0, 1)
    tile = dungeon_map.get_tile(*world)
    assert tile is not None and tile.guessed

    # the beam gone, the hero still adjacent: the tile draws nothing, while
    # the ground on the other side of it is still drawn and still matches
    lines = ["---------", "|..@ ...|", "---------"]
    info = dungeon_map.apply_screen(lines, (1, 3), (0, 2))
    assert info["conflicts"] == 0  # the stale guess contradicts nothing
    tile = dungeon_map.get_tile(*world)
    assert not tile.guessed
    assert tile.solid  # adjacent and blank: rockwall, confirmed


# --------------------------------------------------------------------------- #
#  stairs and searches: ledgers, not single slots
# --------------------------------------------------------------------------- #


def test_every_stair_seen_is_remembered():
    dungeon_map = build(
        [
            "-----------",
            "|..>......|",
            "|....@....|",
            "|..<......|",
            "-----------",
        ]
    )
    dungeon_map.observe(0, 6, DOWN)  # a second down stair found later
    assert len(dungeon_map.downstairs) == 2
    assert dungeon_map.upstairs_pos is not None


def test_stair_ledger_round_trips_and_reads_old_state_files():
    dungeon_map = DungeonMap()
    dungeon_map.observe(0, 0, DOWN)
    dungeon_map.observe(3, 4, DOWN)
    dungeon_map.observe(2, 2, UP)
    dungeon_map.mark_searched_around((2, 2))
    restored = DungeonMap.from_dict(dungeon_map.to_dict())
    assert restored.downstairs == {(0, 0), (3, 4)}
    assert restored.upstairs == {(2, 2)}

    legacy = {
        "level": 1,
        "player_world_pos": None,
        "dungeon_offset": [0, 0],
        "downstairs_pos": [7, 8],
        "upstairs_pos": None,
        "tiles": {},
    }
    assert DungeonMap.from_dict(legacy).downstairs == {(7, 8)}


def test_search_ledger_covers_the_walls_around_you():
    dungeon_map = build(ROOM_SCREEN)
    before = len(dungeon_map.unsearched_wall_targets())
    assert before > 0
    # stand against the top wall and search: the three wall tiles touching
    # that spot are covered; the rest of the room's walls are not
    by_the_wall = (-1, 0)  # one row under the top wall, world coords
    assert dungeon_map.mark_searched_around(by_the_wall) >= 3
    after = dungeon_map.unsearched_wall_targets()
    assert len(after) < before
    summary = dungeon_map.describe()
    assert summary["unsearched_walls"] == len(after)
    assert DungeonMap.from_dict(dungeon_map.to_dict()).searched == dungeon_map.searched


def test_standing_on_a_tile_undoes_a_false_solid():
    """'X' means the game refused us here; the hero standing on it is proof
    to the contrary, whatever an earlier wrong reading remembered."""
    dungeon_map = DungeonMap()
    dungeon_map.mark_solid(5, 5)
    assert dungeon_map.get_tile(5, 5).solid
    dungeon_map.visit(5, 5)
    assert not dungeon_map.get_tile(5, 5).solid
    assert dungeon_map.get_tile(5, 5).visited
