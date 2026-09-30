"""Screen -> map tests against a captured real tty screen.

These guard the parts that used to corrupt the map:

* reading terrain out of the message line ("You don't have that object."),
* reading terrain out of menus that are not the dungeon at all,
* losing whole rooms because the dungeon offset drifted.
"""

from pathlib import Path

import pytest

from nethackmcp.map import VISIBLE_TERRAIN
from nethackmcp.server import NetHackClient

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client():
    return NetHackClient(restore=False)


@pytest.fixture()
def real_lines():
    return (FIXTURES / "level1_screen.txt").read_text().rstrip("\n").split("\n")


def load(client, lines, expected_world=None):
    client.current_level = 1
    return client.update_map(lines, expected_world=expected_world)


def scroll(lines, dr, dc, player=(18, 46)):
    """Move the dungeon by (dr, dc) while the player stays on screen.

    That is exactly what NetHack does when you walk towards the edge of the
    viewport: the level slides, ``@`` does not.
    """
    pr, pc = player
    bare = [list(line) for line in lines]
    bare[pr][pc] = "."  # the tile under @ keeps its terrain
    out = []
    for i in range(len(lines)):
        if 1 <= i <= 21:
            row = []
            for j in range(len(lines[i])):
                src_r, src_c = i - dr, j - dc
                if 1 <= src_r <= 21 and 0 <= src_c < len(bare[src_r]):
                    row.append(bare[src_r][src_c])
                else:
                    row.append(" ")
            out.append("".join(row))
        else:
            out.append(lines[i])
    out[pr] = out[pr][:pc] + "@" + out[pr][pc + 1 :]
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
    dungeon_map.dungeon_offset = (  # deliberately wrong by one column
        dungeon_map.dungeon_offset[0],
        dungeon_map.dungeon_offset[1] + 1,
    )
    cells = dungeon_map.screen_cells(real_lines, (2, 21))
    solved = dungeon_map.solve_offset(cells, (18, 46), search_range=4)
    assert solved != dungeon_map.dungeon_offset
    assert solved[1] == dungeon_map.dungeon_offset[1] - 1


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
