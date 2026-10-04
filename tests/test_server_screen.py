"""Screen -> map tests against a captured real tty screen.

These guard the parts that used to corrupt the map:

* reading terrain out of the message line ("You don't have that object."),
* reading terrain out of menus that are not the dungeon at all,
* losing whole rooms because the dungeon offset drifted,
* losing the *entire* map because a `St:18/01` strength hid the status row.
"""

import sys
from pathlib import Path

import pytest

from nethackmcp.map import VISIBLE_TERRAIN
from nethackmcp.server import (
    NetHackClient,
    _format_state,
    _show_fullscreen,
    keys_for,
)

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


def test_draining_the_pager_records_a_diff_of_each_page(client, monkeypatch):
    """The agent never sees the pages it skimmed past on screen, so each Space
    that turns one must be captured as a diff of what changed."""
    frames = [
        ["A wolf bites!--More--", "", *MAP_ROWS_],
        ["You miss.  A wolf bites!--More--", "", *MAP_ROWS_],
        ["", "", *MAP_ROWS_],
    ]
    cursor = {"i": 0}

    def fake_send_keys(*keys):
        cursor["i"] = min(cursor["i"] + 1, len(frames) - 1)

    monkeypatch.setattr(client, "_send_keys", fake_send_keys)
    monkeypatch.setattr(
        client, "_capture_screen", lambda: ("\n".join(frames[cursor["i"]]), frames[cursor["i"]])
    )

    cleared = client._drain_more(frames[0])
    assert len(cleared) == 2
    assert client.pager_diff, "the turned pages must be recorded, not thrown away"
    assert "wolf" in client.pager_diff or "miss" in client.pager_diff


# --------------------------------------------------------------------------- #
#  the status row: St:18/01 must not hide the map (the bug that lost everything)
# --------------------------------------------------------------------------- #


def test_percentile_strength_status_row_is_detected(client, real_lines):
    """'St:18/01' (percentile strength) used to fail the regex, so the map
    window ran one row too low, ate the character-name line as map body, and
    the whole map vanished from every tool reply."""
    percentile = [line.replace("St:15 Dx:18", "St:18/01 Dx:11") for line in real_lines]
    assert client._status_row(percentile) == 22
    assert client._map_window(percentile) == (1, 21)
    load(client, percentile)
    assert client.maps[1].remembered_count() > 0, "the map was lost to a strength suffix"


def test_starred_strength_status_row_is_detected(client, real_lines):
    """'St:18/**' (over 100 percentile) is a status row too."""
    starred = [line.replace("St:15 Dx:18", "St:18/** Dx:11") for line in real_lines]
    assert client._status_row(starred) == 22
    assert client._map_window(starred) is not None


# --------------------------------------------------------------------------- #
#  the map is always part of the answer
# --------------------------------------------------------------------------- #


def test_map_and_position_always_reach_the_agent(client, monkeypatch, real_lines):
    """Even a screen that is not the map (a menu) hands back the memory map as
    it stands and the player's position - the agent should never have to ask."""
    load(client, real_lines)
    remembered_before = client.maps[1].remembered_count()
    inventory = [
        "Invoking:                        Prepare",
        "* a +0 dart (in quiver: b +0 dart)",
        "  b - a katana",
        "Virtuoos the Ronin   St:18/01 Dx:11 Co:18 In:9 Wi:8 Ch:8 Lawful",
        "Dlvl:1 $:10 HP:15(15) Pw:2(2) AC:4 Xp:1",
    ]
    monkeypatch.setattr(client, "_capture_screen", lambda: ("\n".join(inventory), inventory))

    state = client.get_state()
    assert state.get("map"), "the memory map must be in every state"
    assert state["map"].count("#") + state["map"].count(".") > 0, "map was not the remembered one"
    assert remembered_before > 0
    assert "YOU ARE" in state.get("hints", "")
    assert "unknown position" not in state["hints"], "position survived from memory"
    out = _format_state(state)
    assert "=== MAP" in out
    assert "YOU ARE" in out


def test_nothing_to_watch_still_renders_without_a_clear(monkeypatch):
    """The fullscreen watch homes and overwrites; it must never emit a full
    screen clear, and must survive a state with no map yet."""
    captured = []

    class FakeStdout:
        def write(self, s):
            captured.append(s)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", FakeStdout())
    _show_fullscreen({"status": "playing", "screen": "x", "level": 1})
    text = "".join(captured)
    assert text.startswith("\033[H"), "the cursor must home, not clear"
    assert "\033[2J" not in text, "a full clear flickers; the watch overwrites instead"
    assert "=== MAP" in text, "the map belongs in the fullscreen view too"



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


# --------------------------------------------------------------------------- #
#  stepwise routes: verified key by key, never silently swallowed
# --------------------------------------------------------------------------- #


class FakePane:
    """A scripted tmux pane: it renders a dungeon and *really* moves the @
    for each direction key, refusing walls - so a swallowed or blocked key
    changes the screen exactly the way the game would."""

    def __init__(self, rows, hero, msg=""):
        self.rows = [list(r) for r in rows]
        self.hero = hero
        self.msg = msg
        self.sent = []
        self.pager_at = None  # hero cell after which a message pages up
        self.prompt_at = None  # hero cell after which the game asks a question

    def render(self):
        rows = ["".join(r) for r in self.rows]
        r, c = self.hero
        line = list(rows[r])
        line[c] = "@"
        rows[r] = "".join(line)
        return [self.msg, "", *rows, "St:12 Dx:12", "Dlvl:1 $:0 HP:15(15)"]

    def capture(self):
        lines = self.render()
        return "\n".join(lines), lines

    def _passable(self, r, c):
        if not (0 <= r < len(self.rows)) or not (0 <= c < len(self.rows[r])):
            return False
        return "".join(self.rows[r])[c] in ".#<>"

    def send_keys(self, *keys):
        self.sent.append(keys)
        if "Space" in keys and "--More--" in self.msg:
            self.msg = ""

    def type(self, text):
        self.sent.append(text)
        for char in text:
            step = {
                "h": (0, -1), "j": (1, 0), "k": (-1, 0), "l": (0, 1),
                "y": (-1, -1), "u": (-1, 1), "b": (1, -1), "n": (1, 1),
            }.get(char)
            if step is None:
                continue
            target = (self.hero[0] + step[0], self.hero[1] + step[1])
            if self._passable(*target):
                self.hero = target
                if self.pager_at is not None and self.hero == self.pager_at:
                    self.msg = "You are becoming hungry.--More--"
                    self.pager_at = None
                if self.prompt_at is not None and self.hero == self.prompt_at:
                    self.msg = "In what direction? "
                    self.prompt_at = None


CORRIDOR = ["----------", "|........|", "----------"]


def wire(client, pane, monkeypatch):
    monkeypatch.setattr(client, "_capture_screen", pane.capture)
    monkeypatch.setattr(client, "_type", pane.type)
    monkeypatch.setattr(client, "_send_keys", pane.send_keys)
    monkeypatch.setattr(client, "_wait_until_stable", lambda **kw: pane.capture()[1])
    client.session_name = "fake-session"


def test_route_stops_at_the_blocked_key_and_types_nothing_after(client, monkeypatch):
    pane = FakePane(CORRIDOR, (1, 3))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])

    state = client.send_command("lllllll")  # seven east; only five fit

    assert pane.hero == (1, 8)  # the corridor ends at the east wall
    typed = [s for s in pane.sent if s == "l"]
    assert len(typed) == 6, "the key past the block must never be typed"
    note = state.get("move_result", "")
    assert "consumed 6 of 7 keys" in note
    assert "did not move you" in note
    assert "wall" in note
    assert client.maps[1].player_world_pos[1] == 5  # world col 8 at anchor screen col 3


def test_pager_mid_route_is_drained_and_the_route_finishes(client, monkeypatch):
    pane = FakePane(CORRIDOR, (1, 2))
    pane.pager_at = (1, 4)
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])

    state = client.send_command("llll")

    assert pane.hero == (1, 6), "the route must resume after draining the pager"
    assert ("Space",) in pane.sent, "only Space turns this build's pager"
    note = state.get("move_result", "")
    assert "cleared 1 waiting message(s)" in note
    assert "consumed" not in note, "the route completed; nothing to stop for"
    assert state.get("pager_diff"), "the turned page must reach the agent"


def test_prompt_before_a_key_pauses_the_route(client, monkeypatch):
    pane = FakePane(CORRIDOR, (1, 2))
    pane.prompt_at = (1, 3)
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])

    state = client.send_command("llll")

    typed = [s for s in pane.sent if s == "l"]
    assert len(typed) == 1, "no route key may be typed into a live prompt"
    note = state.get("move_result", "")
    assert "stopped before key 2" in note
    assert "In what direction" in note
    assert state["awaiting"]["kind"] == "direction"


def test_bumping_into_another_at_blocks_instead_of_swapping(client, monkeypatch):
    """A shopkeeper walks up while the hero watches, then `ll` tries to walk
    into her: the game refuses, and the map must refuse too - the D3 tragedy
    was the map deciding the hero *was* the standing @ after such a bump."""
    rows = ["-------------", "|...........|", "-------------"]
    pane = FakePane(rows, (1, 5))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])
    assert client.maps[1].player_world_pos == (0, 0)

    # the shopkeeper arrives one tile east, in plain sight
    pane.rows[1][6] = "@"
    client.get_state()
    assert client.maps[1].player_world_pos == (0, 0), "an adjacent @ must not move us"

    state = client.send_command("ll")

    assert pane.hero == (1, 5), "the game refused the step"
    assert client.maps[1].player_world_pos == (0, 0), "the map must not swap us"
    note = state.get("move_result", "")
    assert "did not move you" in note
    assert "something is standing there (@)" in note
    assert [s for s in pane.sent if s == "l"] == ["l"], "second key never typed"
    keeper = client.maps[1].get_tile(0, 1)
    assert keeper is None or not keeper.solid, "the shopkeeper is not a wall"


def test_single_key_bump_does_not_move_the_hero_onto_the_keeper(client, monkeypatch):
    rows = ["-------------", "|...........|", "-------------"]
    pane = FakePane(rows, (1, 5))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])
    pane.rows[1][6] = "@"
    client.get_state()

    state = client.send_command("l")

    assert client.maps[1].player_world_pos == (0, 0)
    note = state.get("move_result", "")
    assert "went nowhere" in note and "(@)" in note


# --------------------------------------------------------------------------- #
#  what the reply says: awaiting, stairs ledger, no-exit
# --------------------------------------------------------------------------- #


def test_awaiting_classifies_who_owns_the_keyboard(client):
    assert client._awaiting(["hi there.--More--", "", *MAP_ROWS_])["kind"] == "pager"
    assert (
        client._awaiting(["In what direction?", "", *MAP_ROWS_])["kind"]
        == "direction"
    )
    assert (
        client._awaiting(
            ["There is a chest here; force its lock? [ynq]", "", *MAP_ROWS_]
        )["kind"]
        == "ynq"
    )
    clean = ["", "", *MAP_ROWS_]
    assert client._awaiting(clean) is None
    menu = [
        "",
        "Pick an object from your inventory:",
        "+----------------+",
        "a - a +0 katana",
        "b - a potion of sickness",
        "+----------------+",
        "St:15 Dx:18",
        "Dlvl:1",
    ]
    assert client._awaiting(menu)["kind"] == "menu"


def test_no_exit_warning_when_a_mapped_level_has_no_way_down(client, monkeypatch):
    rows = [
        "-------",
        "|.....|",
        "|.....|",
        "|..@..|",
        "|.....|",
        "|.....|",
        "-------",
    ]
    pane = FakePane(rows, (3, 3))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])
    state = client.get_state()

    assert state["map_summary"]["frontier_cells"] == 0
    assert state["map_summary"]["downstairs"] == []
    assert "NO EXIT" in state["exit_warning"]
    assert "never been searched" in state["exit_warning"]
    assert "WARNING: NO EXIT" in _format_state(state)


def test_a_level_with_stairs_never_shouts_no_exit(client, monkeypatch):
    rows = [
        "-------",
        "|....>|",
        "|..@..|",
        "|.....|",
        "-------",
    ]
    pane = FakePane(rows, (2, 3))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])
    state = client.get_state()

    assert "exit_warning" not in state
    assert "stairs down (-1,2)" in _format_state(state)


def test_format_state_lists_every_down_stair():
    state = {
        "status": "playing",
        "screen": "",
        "map": "x",
        "map_summary": {
            "remembered_tiles": 9,
            "visited_tiles": 3,
            "frontier_cells": 0,
            "downstairs": [[-3, 4], [1, 2]],
        },
    }
    out = _format_state(state)
    assert "(-3,4)" in out and "(1,2)" in out


def test_search_command_marks_the_walls_it_covered(client, monkeypatch):
    rows = ["-------", "|.....|", "|..@..|", "-------"]
    pane = FakePane(rows, (2, 3))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])
    dungeon_map = client.maps[1]
    assert dungeon_map.searched == set()

    client.send_command("2s")

    assert dungeon_map.searched, "a finished search must hit the ledger"
    # and 's' alone is a search too, while 's' inside a command is not
    client.send_command("20s")
    wider = set(dungeon_map.searched)
    assert wider == dungeon_map.searched


# --------------------------------------------------------------------------- #
#  window and plumbing hygiene
# --------------------------------------------------------------------------- #


def test_a_dense_shop_screen_is_still_the_map(client):
    """Shelves and stock are dense but no *words* - the old junk budget
    refused such screens, and shop memory went stale exactly where a run
    needed it."""
    lines = [
        "",
        "Tefenni's used armor dealership.",
        " ---------------- ",
        " |[[[[[[[[[[[[[| ",
        " |.....$..)....| ",
        " |......@......| ",
        " |$)($)%(*#$%.| ",
        " ---------------- ",
        "St:12 Dx:15      ",
        "Dlvl:3 $:9 HP:12(12)",
    ]
    assert client._map_window(lines) is not None
    # the hero's screen cell is the committed one, not the first '@' found
    load(client, lines)
    assert client.maps[3].player_world_pos == (0, 0)


def test_restore_state_picks_the_newest_file(tmp_path, monkeypatch):
    import json
    import os

    monkeypatch.setattr(NetHackClient, "STATE_FILE_PREFIX", str(tmp_path / "nh_"))
    old = tmp_path / "nh_aaa.json"
    new = tmp_path / "nh_zzz.json"
    old.write_text(json.dumps({"session_name": "nethack_old"}))
    new.write_text(json.dumps({"session_name": "nethack_new"}))
    os.utime(old, (0, 0))
    os.utime(new, (1000, 1000))

    client = NetHackClient(restore=False)
    client._restore_state()
    assert client.session_name == "nethack_new"
    assert client.state_file == str(new)


def test_instructions_send_force_to_chests_and_kicks_to_doors():
    """#force in this build only ever forces a box at your feet (src/lock.c
    carries a literal TODO about doors) - telling agents to '#force' a locked
    door produced a no-op and a lost run's worth of confusion."""
    from nethackmcp.server import SERVER_INSTRUCTIONS

    assert "box you are standing on" in SERVER_INSTRUCTIONS
    assert "`#force` it" not in SERVER_INSTRUCTIONS
    assert "one key at a time" in SERVER_INSTRUCTIONS
    assert "AMBIGUOUS" in SERVER_INSTRUCTIONS
    assert "NO EXIT" in SERVER_INSTRUCTIONS


def test_a_successful_single_step_gets_no_move_note(client, monkeypatch):
    """The MOVE note must judge the step against the *merged* position:
    reading the pre-merge map reported every successful single key as
    "went nowhere" - and a false block on drawn '#' marks the hero's own
    new tile solid."""
    pane = FakePane(CORRIDOR, (1, 3))
    wire(client, pane, monkeypatch)
    load(client, pane.capture()[1])

    state = client.send_command("l")

    assert pane.hero == (1, 4)
    assert client.maps[1].player_world_pos == (0, 1)
    assert "move_result" not in state
    tile = client.maps[1].get_tile(0, 1)
    assert tile is not None and not tile.solid and tile.visited


def test_the_live_d3_shop_scene_never_anchors_on_the_shopkeeper(client):
    """The real scene that bricked the wizard's run, as captured: hero in the
    cave south of Tefenni's door, shopkeeper visible inside, and a D3 memory
    scrambled enough by the old algorithm that the keeper's cell explains the
    screen just as cleanly as ours.  The merge must pick the cave `@`, keep
    the alignment it has, and stay there through every re-read - the old code
    flipped between the two readings and duplicated the room out from under
    the hero."""
    import json

    from nethackmcp.map import DungeonMap

    state = json.loads((FIXTURES / "shop_two_at_state.json").read_text())
    lines = (FIXTURES / "shop_two_at_screen.txt").read_text().rstrip("\n").split("\n")
    dungeon_map = DungeonMap.from_dict(state["maps"]["3"])
    client.current_level = 3
    client.maps[3] = dungeon_map

    window = client._map_window(lines)
    assert window is not None, "the shop screen must read as a map"
    cands = client._player_candidates(lines, window)
    assert len(cands) == 2, "the capture really shows two @ glyphs"

    home = dungeon_map.player_world_pos  # (4, -3): the stale committed spot
    assert home == (4, -3), "fixture premise: memory is out of date"
    for _ in range(4):
        info = client.update_map(lines)
        assert info["updated"]
        assert info["alignment_status"] == "locked"
        # never the shopkeeper: the cave @ stands at world (3, -1) on this screen
        assert dungeon_map.player_world_pos != (1, 0)
        assert dungeon_map.dungeon_offset == (-5, -73), "the alignment drifted"
    # recovery, not stasis: the merge re-anchors onto the cave @ and stays put
    assert dungeon_map.player_world_pos == (3, -1)

    keeper = dungeon_map.get_tile(1, 0)
    assert keeper is None or not keeper.solid, "a shopkeeper is not a wall"
    assert keeper is None or not keeper.visited, "we never walked into the shop"
