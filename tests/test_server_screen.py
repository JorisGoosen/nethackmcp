"""Screen -> map tests against a captured real tty screen.

These guard the parts that used to corrupt the map:

* reading terrain out of the message line ("You don't have that object."),
* reading terrain out of menus that are not the dungeon at all,
* losing whole rooms because the dungeon offset drifted.
"""

from pathlib import Path

import pytest

from nethackmcp.map import VISIBLE_TERRAIN
from nethackmcp.server import NetHackClient, keys_for

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client():
    return NetHackClient(restore=False)


@pytest.fixture()
def real_lines():
    return (FIXTURES / "level1_screen.txt").read_text().rstrip("\n").split("\n")


def load(client, lines, expected_world=None):
    """Apply a screen the way ``get_state`` does: adopt the depth on the status
    line first, so every screen is merged into its own level's map."""
    level = client._extract_level(lines)
    client.current_level = level if level is not None else 1
    return client.update_map(lines, expected_world=expected_world)


def scroll(lines, dr, dc, player=(18, 46)):
    """Move the dungeon by (dr, dc) while the player stays on screen.

    That is exactly what NetHack does when you walk towards the edge of the
    viewport: the level slides, ``@`` does not.  The map window is padded to a
    fixed grid first, because a real pane is a fixed grid too: sliding must
    lose columns off one edge and reveal blank at the other, never truncate a
    row of terrain into nothing.
    """
    pr, pc = player
    width = max(len(line) for line in lines)
    bare = [list(line.ljust(width)) for line in lines]
    bare[pr][pc] = "."  # the tile under @ keeps its terrain
    out = []
    for i in range(len(bare)):
        if 1 <= i <= 21:
            row = []
            for j in range(width):
                src_r, src_c = i - dr, j - dc
                if 1 <= src_r <= 21 and 0 <= src_c < width:
                    row.append(bare[src_r][src_c])
                else:
                    row.append(" ")
            out.append("".join(row))
        else:
            out.append("".join(bare[i]))
    row = out[pr]
    out[pr] = row[:pc] + "@" + row[pc + 1 :]
    return out


# --------------------------------------------------------------------------- #
#  window detection
# --------------------------------------------------------------------------- #


def test_status_row_and_map_window(client, real_lines):
    assert client._status_row(real_lines) == 22
    # row 0 is the message line, row 1 is blank map, rows 2..21 are the level
    assert client._map_window(real_lines) == (1, 21)


def test_wrapped_message_is_not_read_as_map(client, real_lines):
    """A two-line message pushes the map down; the prose must stay out."""
    wrapped = list(real_lines)
    wrapped[1] = "Are you sure you want to abandon that mission? [yn]"
    assert client._map_window(wrapped) == (2, 21)


def test_message_row_never_enters_the_map(client, real_lines):
    """'You don't have that object.' is prose, not a corridor."""
    load(client, real_lines)
    dungeon_map = client.maps[1]
    window = client._map_window(real_lines)
    offset = dungeon_map.dungeon_offset
    for col, char in enumerate(real_lines[0]):
        if char in VISIBLE_TERRAIN:
            tile = dungeon_map.get_tile(offset[0], offset[1] + col)
            assert tile is None or not tile.memory, f"leaked from message row at col {col}"
            assert tile is None or not tile.solid
    assert window[0] >= 1


def test_level_is_read_from_the_status_line(client, real_lines):
    assert client._extract_level(real_lines) == 1


def test_menu_screen_is_not_a_map(client):
    lines = (FIXTURES / "menu_screen.txt").read_text().rstrip("\n").split("\n")
    assert client._map_window(lines) is None
    result = load(client, lines)
    assert result["updated"] is False
    assert client.maps.get(1) is None or client.maps[1].remembered_count() == 0


# --------------------------------------------------------------------------- #
#  ingestion
# --------------------------------------------------------------------------- #


def test_the_whole_visible_level_is_remembered(client, real_lines):
    load(client, real_lines)
    dungeon_map = client.maps[1]
    first, last = client._map_window(real_lines)
    on_screen = [
        (row, col, char)
        for row in range(first, last + 1)
        for col, char in enumerate(real_lines[row])
        if char in VISIBLE_TERRAIN
    ]
    offset = dungeon_map.dungeon_offset
    # every glyph the screen drew is in memory, with the same character
    for row, col, char in on_screen:
        remembered = dungeon_map.terrain_at(offset[0] + row, offset[1] + col)
        assert remembered == char, f"({row},{col}) drew {char!r}, map says {remembered!r}"
    # a tile that only showed a monster became walkable ground, not a wall:
    # the dog next to the player on this screen
    dog = dungeon_map.get_tile(offset[0] + 18, offset[1] + 45)
    assert dog is not None and dog.memory and dog.passable
    assert dungeon_map.remembered_count() >= len(on_screen)


def test_message_line_does_not_pollute_the_map(client, real_lines):
    """Row 0 is full of '.' and '-'; none of it may reach the map."""
    info = load(client, real_lines)
    dungeon_map = client.maps[1]
    offset = info["offset"]
    screen_rows = {pos[0] - offset[0] for pos, t in dungeon_map.tiles.items() if t.memory}
    assert min(screen_rows) >= 1, "terrain was learned from the message line"


def test_map_keeps_the_layout_of_the_screen(client, real_lines):
    load(client, real_lines)
    dungeon_map = client.maps[1]
    rendered = dungeon_map.render()
    # the room the player is standing in is 12 wide, not 4
    player_room = dungeon_map.room_at(*dungeon_map.player_world_pos)
    assert player_room is not None
    min_r, max_r, min_c, max_c = player_room.bounds
    assert max_c - min_c >= 10
    assert max_r - min_r >= 2
    # several rooms are remembered at once
    assert len(dungeon_map.rooms()) >= 4
    assert "@" in rendered


# --------------------------------------------------------------------------- #
#  movement bookkeeping
# --------------------------------------------------------------------------- #


def test_single_step_moves_the_player_one_tile(client, real_lines):
    load(client, real_lines)
    start = client.maps[1].player_world_pos
    # walk west: the dungeon slides one column right, the player stays centred
    load(client, scroll(real_lines, 0, 1), expected_world=(start[0], start[1] - 1))
    assert client.maps[1].player_world_pos == (start[0], start[1] - 1)


def test_scrolling_the_viewport_never_rewrites_known_geometry(client, real_lines):
    load(client, real_lines)
    dungeon_map = client.maps[1]
    before = {pos: tile.terrain for pos, tile in dungeon_map.tiles.items() if tile.memory}
    start = dungeon_map.player_world_pos

    # the viewport slides 2 rows down and 2 columns right
    moved = scroll(real_lines, -2, -2)
    load(client, moved, expected_world=(start[0] + 2, start[1] + 2))

    # nothing we already knew got overwritten: the map grew, it did not warp
    for pos, terrain in before.items():
        assert dungeon_map.terrain_at(*pos) == terrain, f"{pos} rewritten"
    assert dungeon_map.player_world_pos == (start[0] + 2, start[1] + 2)
    assert dungeon_map.remembered_count() >= len(before)


def test_matching_recovers_a_scroll_without_the_keyboard(client, real_lines):
    """Even with no move to go on, the terrain itself pins the map down."""
    load(client, real_lines)
    dungeon_map = client.maps[1]
    start = dungeon_map.player_world_pos
    moved = scroll(real_lines, -1, -1)
    info = load(client, moved)
    assert dungeon_map.player_world_pos == (start[0] + 1, start[1] + 1)
    assert info["alignment"] > 0.95


def test_big_viewport_jump_is_recovered_by_widening_the_search(client, real_lines):
    """A jump further than the normal search range must not smear the map."""
    load(client, real_lines)
    dungeon_map = client.maps[1]
    start = dungeon_map.player_world_pos
    known = {pos: t.terrain for pos, t in dungeon_map.tiles.items() if t.memory}

    # the dungeon slides 6 columns right: further than the default ±4 search
    jumped = scroll(real_lines, 0, 6)
    info = load(client, jumped)
    assert dungeon_map.player_world_pos == (start[0], start[1] - 6)
    assert info["conflicts"] == 0
    for pos, terrain in known.items():
        assert dungeon_map.terrain_at(*pos) == terrain, f"{pos} smeared"


def test_a_misaligned_screen_is_corrected(client, real_lines):
    """If the map drifts, terrain matching pulls it back onto the walls."""
    load(client, real_lines)
    dungeon_map = client.maps[1]
    drifted = (dungeon_map.dungeon_offset[0], dungeon_map.dungeon_offset[1] + 1)
    dungeon_map.dungeon_offset = drifted  # deliberately wrong by one column
    solved = dungeon_map.solve_offset(real_lines, (2, 21), (18, 46))
    assert solved["offset"] != drifted
    assert solved["offset"] == (drifted[0], drifted[1] - 1)
    assert solved["conflicts"] == 0


def test_a_route_of_keys_never_maps_the_same_room_twice(client, real_lines):
    """The bug that duplicated a level: several keys in one command mean the
    server has no single expected position to go on, so the screen alone has to
    say where we are - and it does."""
    load(client, real_lines)
    dungeon_map = client.maps[1]
    known = {pos: t.terrain for pos, t in dungeon_map.tiles.items() if t.memory}
    before = dungeon_map.bounds

    # what "hhhy" looks like on a level that does not scroll: @ walks four
    # columns west, the terrain stays exactly where it was
    pr, pc = 18, 46
    walked = [line[:] for line in real_lines]
    row = list(walked[pr])
    row[pc], row[pc - 4] = ".", "@"
    walked[pr] = "".join(row)
    info = load(client, walked)  # expected_world=None: a multi-key route

    assert info["conflicts"] == 0, "the screen was matched somewhere else"
    assert dungeon_map.player_world_pos == (0, -4)
    assert dungeon_map.bounds == before, "a second copy of the level appeared"
    for pos, terrain in known.items():
        assert dungeon_map.terrain_at(*pos) == terrain, f"{pos} smeared"


def test_blocked_move_into_a_seen_wall_keeps_the_wall(client, real_lines):
    load(client, real_lines)
    dungeon_map = client.maps[1]
    before = dungeon_map.player_world_pos
    note = client._note_move_result("u", before, real_lines, before)
    assert note is not None and "wall" in note
    # the remembered '|' is not overwritten by a generic rockwall marker
    bumped = (before[0] - 1, before[1] + 1)
    assert dungeon_map.terrain_at(*bumped) == "|"
    assert not dungeon_map.get_tile(*bumped).solid


def test_blocked_move_into_a_blank_confirms_rockwall(client):
    lines = [
        "You have a good feeling.--More--          ",
        "                                          ",
        "------                                    ",
        "|....@                                    ",
        "------                                    ",
        "Virtuoos the Hatamoto   St:15 Dx:18       ",
        "Dlvl:1 $:10 HP:15(15)                     ",
    ]
    load(client, lines)
    dungeon_map = client.maps[1]
    before = dungeon_map.player_world_pos
    # the tile east of the player was drawn blank while we were next to it
    east = (before[0], before[1] + 1)
    assert dungeon_map.get_tile(*east).solid
    note = client._note_move_result("l", before, lines, before)
    assert note is not None and "solid rockwall" in note


def test_successful_move_leaves_no_note(client, real_lines):
    load(client, real_lines)
    before = client.maps[1].player_world_pos
    after = (before[0], before[1] + 1)
    assert client._note_move_result("l", before, real_lines, after) is None


# --------------------------------------------------------------------------- #
#  message pager and prompts
# --------------------------------------------------------------------------- #

MAP_ROWS_ = ["-----", "|..@.", "-----", "St:15 Dx:18", "Dlvl:1"]


def test_more_pager_is_drained_with_space(client, monkeypatch):
    """Letters do not dismiss --More-- here; only Space does.

    Without draining, every movement key is eaten by the pager and the player
    never moves while the map keeps "updating".
    """
    frames = [
        ["This door is locked.--More--", "", *MAP_ROWS_],
        ["", "", *MAP_ROWS_],
    ]
    sent: list[tuple] = []
    cursor = {"i": 0}

    def fake_send_keys(*keys):
        sent.append(keys)
        cursor["i"] = min(cursor["i"] + 1, len(frames) - 1)

    def fake_capture():
        lines = frames[cursor["i"]]
        return "\n".join(lines), lines

    monkeypatch.setattr(client, "_send_keys", fake_send_keys)
    monkeypatch.setattr(client, "_capture_screen", fake_capture)

    cleared = client._drain_more(frames[0])
    assert cleared == ["This door is locked."]
    assert sent == [("Space",)], "the pager must be cleared with Space, not a letter"
    assert "--More--" not in "\n".join(frames[cursor["i"]])


def test_open_question_prompt_is_reported(client):
    lines = ["Unlock it with your key? [ynq] (q)", "", *MAP_ROWS_]
    assert client._open_prompt(lines) == "Unlock it with your key? [ynq] (q)"


def test_plain_message_is_not_a_prompt(client):
    lines = ["You have a bad feeling about this.", "", *MAP_ROWS_]
    assert client._open_prompt(lines) == ""


def test_pager_is_not_reported_as_a_prompt(client):
    """--More-- is drained, not answered: it must not look like a question."""
    lines = ["This door is locked.--More--", "", *MAP_ROWS_]
    assert client._open_prompt(lines) == ""


def test_menu_still_blocks_map_learning(client, real_lines):
    """A screen with a prompt over the map teaches us nothing new."""
    lines = list(real_lines)
    lines[1] = "Replace old item? [ynaq] (a)"
    before = dict(client.maps.get(1).tiles) if client.maps.get(1) else {}
    result = load(client, lines)
    if result["updated"]:
        # memory is never rewritten by a screen we may have mis-read
        for pos, tile in before.items():
            if tile.memory:
                assert client.maps[1].terrain_at(*pos) == tile.terrain


# --------------------------------------------------------------------------- #
#  command string -> keystrokes
# --------------------------------------------------------------------------- #


def test_extended_command_is_typed_after_the_hash():
    """'#' is this build's extended-command key; the name goes after it."""
    assert keys_for("#force") == [("text", "#force"), ("keys", "Enter")]


def test_extended_command_is_not_sent_as_meta_x():
    """Escape cancels the pending command, then the name's letters start moving.

    The old M-x route made `#quit` reach `q` (quaff) and answer it with `uit`.
    """
    for name in ("#quit", "#kick", "#herecmdmenu"):
        actions = keys_for(name)
        assert all(value != "Escape" for _, value in actions), name
        assert all(value != "x" for _, value in actions), name


def test_caret_command_is_a_control_key():
    assert keys_for("^X") == [("keys", "C-x")]
    assert keys_for("^D") == [("keys", "C-d")]
    # a lone ^ is the letter: it examines an adjacent trap
    assert keys_for("^") == [("text", "^")]


def test_ordinary_commands_are_sent_as_literal_text():
    """Never send-keys without -l: 'Enter' as text is not a carriage return."""
    for command in ("mh", ",", "40j", "F", "o", "Enter the dungeon"):
        assert keys_for(command) == [("text", command)]


def test_named_keys_stay_named_keys():
    assert keys_for("Escape") == [("keys", "Escape")]
    assert keys_for("\n") == [("keys", "Enter")]


# --------------------------------------------------------------------------- #
#  one map per level
# --------------------------------------------------------------------------- #


def remembered(dungeon_map):
    return {pos: tile.terrain for pos, tile in dungeon_map.tiles.items() if tile.memory}


def test_each_level_keeps_its_own_memory(client, real_lines):
    """Going down and coming back must not mix two levels up."""
    load(client, real_lines)
    level_one = remembered(client.maps[1])
    assert level_one

    deeper = [line.replace("Dlvl:1", "Dlvl:3") for line in real_lines]
    load(client, deeper)
    assert client.current_level == 3
    assert client.maps[3].tiles  # level 3 got its own map from the same screen

    load(client, real_lines)
    assert client.current_level == 1
    assert remembered(client.maps[1]) == level_one, "level 1's memory was written over"


def test_a_new_level_anchors_where_the_hero_lands(client, real_lines):
    """A level we have never seen starts its coordinates at the player, and then
    keeps them: walking must not re-anchor the map we have just drawn."""
    load(client, real_lines)
    deeper = [line.replace("Dlvl:1", "Dlvl:2") for line in real_lines]
    load(client, deeper)
    assert client.maps[2].player_world_pos == (0, 0)

    slid = scroll(deeper, 0, -5)
    load(client, slid)
    assert client.maps[2].player_world_pos == (0, 5), "the origin slid with the screen"
    assert client.maps[1].player_world_pos == (0, 0), "level 1 was redrawn by level 2"
