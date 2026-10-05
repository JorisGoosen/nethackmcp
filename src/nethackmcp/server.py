"""MCP server for playing NetHack.

The server drives NetHack inside a tmux session and keeps a *memory map* of
every level (see :mod:`nethackmcp.map`).  Position tracking combines two
sources so the map stays coherent:

* terrain matching against remembered geometry, and
* the keyboard: a single movement key always moves exactly one tile, so when
  the two disagree we trust the key unless the screen clearly contradicts it.

A movement key that does *not* move the player proves the tile it pointed at is
solid rockwall, which is the only reliable way to tell a wall from unrevealed
space.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from nethackmcp.map import (
    CAVE,
    DIR_KEYS,
    DOOR,
    KEY_DIRS,
    LIQUID,
    VISIBLE_TERRAIN,
    DungeonMap,
    route_displacement,
    route_tokens,
    tile_matches,
)

SERVER_INSTRUCTIONS = """\
You are connected to NetHack through this MCP server.  It drives NetHack in a
tmux session and keeps a *memory map* of every level for you.

## Commands (vi keys: this build runs with number_pad off)

- **Directions**: h(←) j(↓) k(↑) l(→) y(↖) u(↗) b(↙) n(↘)
- **Run**: uppercase direction (H J K L Y U B N) runs that way until something
  happens; `g`+dir runs until something *interesting* is seen, `G`+dir the same
  but ignoring branching corridors
- **Move without picking up**: `m`+direction (e.g. `mh`)
- **Fight**: walk into it, or `F`+direction to attack even where you sense
  nothing.  `f`+direction is *fire from the quiver*, not fighting
- **Repeat**: a count before a command repeats it - `20s` searches twenty times
- **Pick up**: `,`   **Drop**: `d`   **Inventory**: `i`   **What here**: `:`
- **Wear/wield**: `W`/`w`   **Take off armor**: `T`   **Remove ring/amulet**: `R`
  **Put on accessory**: `P`
- **Eat**: `e`   **Drink**: `q`   **Read**: `r`   **Engrave**: `E`   **Throw**: `t`
- **Zap wand**: `z`   **Cast spell**: `Z`   **Fire ammo**: `f`
- **Open door**: `o`+direction   **Close door**: `c`+direction   **Apply tool**: `a`
- **Kick**: `^D`+direction - the way to break a **locked or stuck door**: one or two
  calls per kick, 1-6 kicks usually finish it and leave `%` rubble where the door was
- **Force a chest**: `#force` - forces the lock of a **box you are standing on**,
  after asking you `[ynq]`, and only with a proper melee weapon wielded.  It never
  asks a direction and does nothing at all to doors in this build: for a locked
  door, kick it.  (this build has **no** `#knock`)
- **Search** for secret doors and traps: `s` (stand next to the wall, repeat; a
  count repeats it - `2s`, `20s`)
- **The game's own map**: `#terrain` draws the level as NetHack itself remembers
  it - the cross-check to run when this server's map and the world disagree; it
  opens a menu and pages, answer each with its own call
- **Look**: `:` here, `;` somewhere else, `/` what a symbol is, `^` an adjacent trap
- **Travel**: `_` lets NetHack pathfind to a spot you pick on the map
- **Menus of what you can do**: `#herecmdmenu`, `#therecmdmenu`
- **Status**: `^X`   **Event journal**: `v`   **Save and quit**: `S`
- **Extended command**: `#name` - `#` is the extended-command key in this build,
  the name is typed after it and completed as you go (`#force`, `#kick`,
  `#lookaround`, `#herecmdmenu`...).  Send it whole: `nethack_move("#force")`
- **Answer a prompt**: send the letter the question asks for (`y`, `n`, `q`, ...)
- **Cancel** whatever is pending: `Escape` (also closes a menu); `?` for help

You do not need to clear message pages: when the screen sits on `--More--`, the
next command drains it with Space first, and what it turned past is handed back
as a `PAGER` block at the top of the reply - the concatenated screen diffs of
each page turn, so nothing that scrolled by is lost to you.
Letters do not dismiss `--More--` in this build, which is why that matters.

## What every move returns

`PAGER` (only when pages were drained: diffs of the skipped text, prepended),
`SCREEN` (raw), `NEIGHBOURS` (the eight tiles around you and what the map
remembers about each), `YOU ARE` (your world position and what you stand on),
`MAP` (accumulated memory - **always shown**, even when the screen is a menu;
the map keeps whatever it last knew), `FRONTIER` (the places still worth
exploring, with the keys to reach them), `AWAITING` (when something owns your
keystrokes: a pager, a yes/no question, a direction prompt, a menu - and what
is safe to send), and `MOVE` when your command did not do what you asked.

### Map glyphs

The game's own alphabet where it can be.  From `include/defsym.h`: a door is
drawn `+` while it is **shut** and as the wall it sits in (`|` for a door in a
horizontal wall, `-` in a vertical one) once it is **open**, which is why the
map spells the two states out separately.  A door you never saw shut is still
recognised from its setting - a wall glyph whose two sides are wall of the
*same kind* (two drawn walls, or two rockwalls confirmed by a bump; masonry
beside rock is a corner where the wall turns, not a way through), with ground
or yet-unseen tiles beyond it - and remembered as an open doorway `'` like
any other.

| | |
|---|---|
| `@` | you - shopkeepers and some statues draw `@` too; the map follows the one that moves when you do |
| `.` | floor |
| `#` | cave floor / corridor (`#` is also iron bars and trees: a bump settles it) |
| `+` | doorway, door **shut** - `o`+direction opens it, `^D` kicks it in |
| `'` | doorway, standing **open** - walk through |
| `<` | stairs up, `>` stairs down (ladders look the same) |
| `\\|` `-` | walls you have seen |
| `}` | water or lava: you *can* step in, you would just swim or burn |
| `^` | a trap you have spotted (the tile is floor - go around it) |
| `X` | solid: you tried to walk here and could not |
| `?` | **unrevealed**, and reachable: stand next to it and it becomes real |
| ` ` | nothing remembered, nothing discoverable from here |

The rule that makes `?` meaningful: as soon as you are adjacent, a `?` resolves
into `X` (it stayed blank, so it is rockwall) or into the terrain now drawn
there (`.`, `#`, `+`, `<`, `>`).  Nothing blank is ever recorded as a wall, and
nothing is ever `?` unless you could walk up to it.  A tile that only ever showed
a monster or an item is remembered as *guessed* ground: certainly not a wall, but
which kind of ground is still an open question that standing next to it settles.

### How a screen is placed

Your position comes from the **screen**, not from the keys you sent.  The server
matches what is drawn against what it remembers and takes the alignment that
contradicts nothing, nearest first; where the keyboard says you should have ended
up is only where it starts looking.  That matters because keystrokes lie - a step
into a wall, a monster blocking the corridor, a run key that stopped early, a
teleport, or the message window changing height and sliding the whole dungeon a
row.  When a key did not do what it was asked, the `MOVE` note says so and names
what was in the way.

Which `@` on the screen is you is a question, not an assumption: shopkeepers,
pirates and human-shaped statues are drawn `@` too.  When several are visible
each is tried as the hero, and the reading that contradicts memory least and
continues where you were wins.  A tie is reported, not guessed: on
`AMBIGUOUS: ...` the map was deliberately **not** rewritten, both candidate
positions are named, and one single-key move resolves it - the `@` that moves
is you.  Two alignments that explain memory equally well are reported the same
way, and an alignment never moves just because the ring order reached another
clean fit first: ties keep the reading you already had.

If a screen disagrees with memory you get a warning saying how many remembered
tiles disagree.  Believe the screen in that moment: the map around you may be
stale until the alignment settles, so re-read it before trusting a long route.

If the frontier comes back empty and no `>` has ever been drawn on the level,
you get a `NO EXIT` warning with the count of wall tiles that touch reachable
ground and have never been searched - a checklist, not a dead end.  Search each
(`2s` standing next to it), cross-check the level with `#terrain` and
`#lookaround`, and if it stays dry go back up (`<`) and hunt a shaft instead of
orbiting the same walls.

## Coordinates and routes

World coordinates are (row, col): row+1 is south, col+1 is east, the origin is
where you first appeared on the level.  A frontier entry reads

    N cell(s) next to (r,c), S step(s): <route>, poke with `<probe>`
    N cell(s) beyond the door at (r,c), S step(s): <route>, open it with `o<dir>`

`route` walks over remembered ground to the tile that looks out over the biggest
part of that `?` blob; `<probe>` is the one key that makes a `?` real from there,
so sending route+probe as a single `nethack_move` always learns something
(`(you are there)` means you already overlook it).  A route carries `o<dir><dir>`
where it has to open a shut door on the way.  If a door will not open it is
locked: kick it (`^D`+direction), or search (`s`) for a secret way round.

A route is walked and **verified one key at a time**: before each key the server
checks that no pager or prompt owns the input, and after each key that the `@`
really moved where you asked.  The first key the game does not execute stops the
route - the remaining keys are never typed - and the `MOVE` note says after how
many of them you stopped and why.  Nothing you send is silently eaten any more.

Routes never plan a **diagonal** step into or out of an intact doorway: NetHack
refuses those, so approach a door from the side it opens onto.

## Notes

- Monsters, items and messages only appear in the raw `SCREEN`; the map stores
  geometry.  A tile showing a monster or item is remembered as walkable ground,
  because nothing is ever drawn inside rockwall.  `I` on the real screen is not
  a monster either: it marks where an unseen monster was last heard.  And a
  transient effect - a zap's beam, an explosion's `*` - is drawn over
  anything at all, walls included, so it says nothing about the tile.
- Memory per level survives going downstairs and back up.
- `nethack_map` re-reads just the map when you do not need the screen again.
"""

#: friendly key names -> tmux key names
_NAMED_KEYS = {
    "Enter": "Enter",
    "Return": "Enter",
    "\n": "Enter",
    "Escape": "Escape",
    "Space": "Space",
    "Tab": "Tab",
    "Backspace": "BSpace",
    "Ctrl+C": "C-c",
    "Ctrl+D": "C-d",
    "Ctrl+L": "C-l",
    "Ctrl+N": "C-n",
    "Ctrl+P": "C-p",
    "Ctrl+X": "C-x",
    "Ctrl+Z": "C-z",
}

#: an action for tmux: ``("keys", "Enter")`` or ``("text", "#force")``
KeyAction = tuple[str, str]


def keys_for(command: str) -> list[KeyAction]:
    """Translate one command string into what tmux should do with it.

    ``("keys", name)`` is ``tmux send-keys <name>`` and ``("text", s)`` is
    ``tmux send-keys -l s``: literal, so a key string is never reinterpreted as
    a tmux key name (``"Enter"`` typed as text is not a carriage return).

    Two things here are easy to get wrong and both were:

    * ``#`` is this build's extended-command key (``src/cmd.c``, the table entry
      for ``doextcmd``; ``gc.Cmd.extcmd_char``) and the command name is typed
      after it as a line.  M-x is *not* an extended-command initiator: Escape
      cancels whatever is pending and the letters of the name are then read as
      separate commands - ``#quit`` used to reach ``q`` (quaff) and answer it
      with ``uit``, which is how a quit attempt ended in ``a - a +0 katana``.
    * ``^X`` written out is Ctrl-X, not the letter ``^`` (examine trap) and then
      ``X``.  A lone ``^`` is still the letter.
    """
    if command in _NAMED_KEYS:
        return [("keys", _NAMED_KEYS[command])]
    if len(command) == 2 and command[0] == "^" and command[1].isalpha():
        return [("keys", "C-" + command[1].lower())]
    if command.startswith("#"):
        return [("text", command), ("keys", "Enter")]
    return [("text", command)]


def _log_error(context: str, exc: Exception) -> None:
    print(f"[ERROR] {context}: {exc}", file=sys.stderr, flush=True)


class NetHackClient:
    """Drives one NetHack game in a tmux session and remembers the dungeon."""

    TMUX_SESSION_PREFIX = "nethack_"
    STATE_FILE_PREFIX = "/tmp/nethack_state_"

    def __init__(self, restore: bool = True) -> None:
        self.session_name: str | None = None
        self.state_file: str | None = None
        self.current_goal: str = ""
        self.current_level: int = 1
        self.maps: dict[int, DungeonMap] = {}
        self.previous_screen: str | None = None
        self.game_state: dict[str, Any] = {}
        self.cleared_messages: list[str] = []
        self.pager_diff: str = ""
        # the fullscreen poller reads and merges the map concurrently with tool
        # calls; a command must stay atomic so a capture never lands mid-sequence
        self._lock = threading.RLock()
        if restore:
            self._restore_state()

    # ------------------------------------------------------------------ #
    #  tmux plumbing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_ansi(text: str) -> str:
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["tmux", *args], check=check, capture_output=True, text=True
        )
        return result

    def _session_alive(self) -> bool:
        if self.session_name is None:
            return False
        return (
            self._tmux("has-session", "-t", self.session_name, check=False).returncode
            == 0
        )

    def _capture_screen(self) -> tuple[str, list[str]]:
        if self.session_name is None:
            raise RuntimeError("Game not started. Call start_game first.")
        if not self._session_alive():
            raise RuntimeError(f"tmux session '{self.session_name}' not found")
        result = self._tmux("capture-pane", "-t", self.session_name, "-p", "-J")
        text = self._strip_ansi(result.stdout)
        return text, text.split("\n")

    def _send_keys(self, *keys: str) -> None:
        self._tmux("send-keys", "-t", self.session_name, *keys)

    def _type(self, text: str) -> None:
        """Send literal text (never interpreted as tmux key names)."""
        self._tmux("send-keys", "-t", self.session_name, "-l", text)

    #: the game is waiting for an answer; a keypress replies, it does not move
    PROMPT_MARKERS = (
        "[ynq]",
        "[y/n]",
        "[na]",
        "[aq]",
        "Do you want",
        "Do you wish",
        "What do",
        "In what direction",
        "Really",
        "Replace",
        "How many",
        "Which",
        "Unlock it",
    )

    def _open_prompt(self, screen_lines: list[str]) -> str:
        """The question on the message line, if the game awaits an answer."""
        window = self._map_window(screen_lines)
        top = window[0] if window else 1
        message = " ".join(line.strip() for line in screen_lines[: max(top, 1)]).strip()
        if not message or "--More--" in message:
            return ""
        if any(marker in message for marker in self.PROMPT_MARKERS):
            return message
        if message.endswith(("?", "]")):
            return message
        return ""

    def _awaiting(self, screen_lines: list[str]) -> dict[str, Any] | None:
        """What owns the keyboard right now, if anything - and what is safe.

        Movement keys are not neutral while a prompt or a menu is up: they
        *answer* it - `d` drops something instead of stepping south, Enter at
        a direction prompt means "never mind", letters at `--More--` are
        destroyed (``more()`` -> ``xwaitforspace``, win/tty/topl.c).  Say what
        is waiting so a wrong key costs a note, not a run.
        """
        joined = "\n".join(screen_lines)
        if "--More--" in joined:
            return {
                "kind": "pager",
                "question": "--More--",
                "reply": "Space turns the page; nethack_move drains every page for you first",
                "note": (
                    "messages are waiting behind --More--; a 'nethack_move' clears "
                    "them automatically before your command runs"
                ),
            }
        question = self._open_prompt(screen_lines)
        if question:
            if "In what direction" in question:
                return {
                    "kind": "direction",
                    "question": question,
                    "reply": "exactly one of h j k l y u b n, sent alone; Escape or Enter gives up",
                    "note": f"the game waits for a direction: {question}",
                }
            bracket = re.search(r"\[([a-zA-Z/]+)\]", question)
            if bracket:
                letters = sorted(set(bracket.group(1).replace("/", "")))
                kind = "ynq" if "q" in letters else "yn"
                return {
                    "kind": kind,
                    "question": question,
                    "reply": "exactly one of " + " ".join(letters) + ", sent alone",
                    "note": f"the game is waiting for an answer: {question}",
                }
            return {
                "kind": "prompt",
                "question": question,
                "reply": "the letter or line the question asks for, then re-read the screen",
                "note": f"the game is waiting for an answer: {question}",
            }
        if self._map_window(screen_lines) is None and screen_lines:
            body = "".join(screen_lines)
            if "+---" in body or len(re.findall(r"[A-Za-z]{3,}", body)) >= 4:
                return {
                    "kind": "menu",
                    "question": "",
                    "reply": "a menu letter to choose, Escape to close; movement keys are letters here",
                    "note": "a menu is open - keys answer it, they do not move the hero",
                }
        return None

    def _drain_more(self, screen_lines: list[str], limit: int = 15) -> list[str]:
        """Page through pending messages so the real command lands.

        Letters do not dismiss ``--More--`` in this build - only Space does, so
        sending a movement key while the pager is up loses the key entirely.

        Each Space that turns a page changes what the message window shows, so
        we record a diff of that change; the concatenation tells the agent what
        it would otherwise have skimmed past without ever seeing it.
        """
        cleared: list[str] = []
        pager_diffs: list[str] = []
        for _ in range(limit):
            joined = "\n".join(screen_lines)
            if "--More--" not in joined:
                break
            text = joined.split("--More--")[0].strip().split("\n")[-1].strip()
            if text:
                cleared.append(text)
            self._send_keys("Space")
            time.sleep(0.15)
            _, after_lines = self._capture_screen()
            page_diff = self._create_diff(after_lines, screen_lines)
            if page_diff:
                pager_diffs.append(page_diff)
            screen_lines = after_lines
        self.cleared_messages = cleared
        self.pager_diff = "\n".join(pager_diffs)
        return cleared

    def _wait_until_stable(self, timeout: float = 2.0, settle: float = 0.25) -> list[str]:
        """Capture until two consecutive screens agree (game stopped redrawing)."""
        deadline = time.time() + timeout
        previous: list[str] | None = None
        while True:
            _, lines = self._capture_screen()
            if previous is not None and lines == previous:
                return lines
            previous = lines
            if time.time() > deadline:
                return lines
            time.sleep(settle)

    # ------------------------------------------------------------------ #
    #  screen parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_player(screen_lines: list[str]) -> tuple[int, int] | None:
        for r, line in enumerate(screen_lines):
            c = line.find("@")
            if c >= 0:
                return (r, c)
        return None

    @staticmethod
    def _player_candidates(
        screen_lines: list[str], map_rows: tuple[int, int]
    ) -> list[tuple[int, int]]:
        """Every ``@`` drawn inside the map window, top-left first.

        One of them is the hero.  Shopkeepers, pirates and human-shaped statues
        are drawn ``@`` as much as you are (``include/defsym.h``, MONSYM HUMAN),
        and anchoring the map on the wrong one slides the whole level sideways -
        which is how one shop room bricked a run.  Which candidate *is* the hero
        is a question for memory and the keys to answer (``DungeonMap``
        ``apply_screen``), not for ``str.find``.
        """
        first, last = map_rows
        cells: list[tuple[int, int]] = []
        for srow in range(max(first, 0), min(last, len(screen_lines) - 1) + 1):
            for scol, char in enumerate(screen_lines[srow]):
                if char == "@":
                    cells.append((srow, scol))
        return cells

    @staticmethod
    def _status_row(screen_lines: list[str]) -> int | None:
        # get_strength_str() (src/botl.c) writes St as 15, 18/51, 18/** or 100+,
        # so the optional /xx suffix must be part of the match: missing it makes
        # the name row look like map body and the junk budget rejects the window.
        for r, line in enumerate(screen_lines):
            if "Dlvl:" in line or re.search(r"\bSt:\d+(?:/[0-9*]+)?\s+Dx:", line):
                return r
        return None

    @classmethod
    def _text_count(cls, line: str) -> int:
        """Characters that are clearly prose rather than dungeon geometry."""
        return sum(
            1
            for ch in line
            if ch.isprintable()
            and not ch.isspace()
            and ch not in VISIBLE_TERRAIN
            and ch not in "\"'`.,:;!@()$"
        )

    @classmethod
    def _map_window(cls, screen_lines: list[str]) -> tuple[int, int] | None:
        """Inclusive screen rows holding the dungeon map, or None when this
        screen is not the map (menu, prompt, death screen...).

        Row 0 is the message line, and a long message keeps writing on the rows
        below it, so the map starts at the first row that is not prose.  The
        window's height changes with the message, and because the map is
        centred on the player the whole dungeon slides a row when a message
        clears - which is why this must be detected and never assumed.
        """
        status = cls._status_row(screen_lines)
        if status is None:
            return None
        first = 1
        while first < status - 1 and cls._text_count(screen_lines[first]) >= 3:
            first += 1
        last = status - 1
        if last < first:
            return None
        candidates = cls._player_candidates(screen_lines, (first, last))
        if not candidates:
            return None
        body = "".join(screen_lines[first : last + 1])
        # what makes a menu a menu is that it spells words; the dungeon never
        # does.  Monsters and items are scattered single letters, a shop full
        # of stock is rows of '[' and '$' - and 40 of those used to make the
        # window "not a map", which stopped shop interiors merging at all and
        # let the memory behind them go stale exactly when it mattered.
        if len(re.findall(r"[A-Za-z]{3,}", body)) >= 4:
            return None  # a menu or prompt drawn over the map area
        return (first, last)

    @staticmethod
    def _extract_level(screen_lines: list[str]) -> int | None:
        for line in screen_lines:
            match = re.search(r"Dlvl:\s*(\d+|[A-Za-z])", line)
            if match:
                value = match.group(1)
                return int(value) if value.isdigit() else ord(value.upper()) - 64
        return None

    @staticmethod
    def _is_new_game_screen(raw_text: str) -> bool:
        markers = (
            "Who are you?",
            "Do you want your possessions identified?",
            "Shall I pick",
        )
        return any(marker in raw_text for marker in markers)

    # ------------------------------------------------------------------ #
    #  map / position
    # ------------------------------------------------------------------ #

    def get_map(self) -> DungeonMap:
        if self.current_level not in self.maps:
            self.maps[self.current_level] = DungeonMap(level=self.current_level)
        return self.maps[self.current_level]

    def update_map(
        self,
        screen_lines: list[str],
        expected_world: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """Merge the current screen into the level map."""
        window = self._map_window(screen_lines)
        if window is None:
            return {"updated": False, "reason": "no map window on screen"}
        candidates = self._player_candidates(screen_lines, window)
        assert candidates, "_map_window already required an @"
        info = self.get_map().apply_screen(
            screen_lines,
            candidates[0],
            window,
            expected_world=expected_world,
            player_candidates=candidates,
        )
        info["updated"] = True
        return info

    def _note_move_result(
        self,
        command: str,
        before_pos: tuple[int, int] | None,
        before_lines: list[str],
        after_pos: tuple[int, int] | None,
    ) -> str | None:
        """Reconcile a move with the map; name what blocked us.

        A route of several keys is reported the same way: if the hero did not
        move at all then it was the *first* key that was refused, which is the
        only step worth explaining.
        """
        if before_pos is None or after_pos is None:
            return None
        if route_displacement(command) is None:
            return None  # a run key or something that is not walking
        first = command[1] if command[0] == "m" and len(command) == 2 else command[0]
        step = KEY_DIRS[first]
        target = (before_pos[0] + step[0], before_pos[1] + step[1])
        if after_pos != before_pos:
            return None

        # we did not move: find out what stopped us
        dungeon_map = self.get_map()
        offset = dungeon_map.dungeon_offset
        srow, scol = target[0] - offset[0], target[1] - offset[1]
        shown = (
            before_lines[srow][scol]
            if 0 <= srow < len(before_lines) and 0 <= scol < len(before_lines[srow])
            else " "
        )
        tile = dungeon_map.get_tile(*target)
        diagonal = step[0] != 0 and step[1] != 0
        if shown == " ":
            # blank while we were right next to it: confirmed rockwall
            dungeon_map.mark_solid(*target)
            reason = "solid rockwall, nothing was drawn there"
        elif shown == DOOR or (tile is not None and tile.door_closed):
            reason = f"shut door: open it with 'o{first}', or kick it ('^D')"
        elif diagonal and (
            (tile is not None and tile.doorway)
            or dungeon_map.has_door_leaf(before_pos)
        ):
            reason = "NetHack refuses diagonals into or out of an intact doorway"
        elif shown not in VISIBLE_TERRAIN:
            # any other visible thing - a monster, a shopkeeper drawn '@', an
            # item, a boulder - stands in the way without being terrain
            reason = f"something is standing there ({shown})"
        elif tile is not None and tile.terrain == CAVE:
            # drawn '#' and it stopped us: not a corridor after all
            dungeon_map.mark_solid(*target)
            reason = "'#' that will not pass: iron bars, a tree or a raised drawbridge"
        elif tile is not None and tile.terrain == LIQUID:
            reason = "water or lava"
        elif tile is not None and not tile.passable:
            reason = f"{tile.terrain} wall"
        else:
            reason = "blocked"
        return f"{command!r} went nowhere: blocked heading {first} -> ({target[0]},{target[1]}): {reason}"

    # ------------------------------------------------------------------ #
    #  lifecycle
    # ------------------------------------------------------------------ #

    def start_game(self, character: str = "Hero") -> dict[str, Any]:
        """Start a fresh NetHack game in a dedicated tmux session."""
        with self._lock:
            return self._start_game(character)

    def _start_game(self, character: str = "Hero") -> dict[str, Any]:
        session_name = self._new_session_name()
        state_file = self._new_state_file()
        self.stop_game()

        cmd = [
            "tmux", "new-session", "-d", "-s", session_name,
            "-x", "80", "-y", "24",
            "bash", "-c",
            'export NETHACKOPTIONS="color"; exec nethack -d /var/games/nethack',
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            return {
                "status": "error",
                "error": f"Failed to start tmux session: {exc.stderr or exc}",
            }

        self.session_name = session_name
        self.state_file = state_file
        self.maps = {}
        self.current_level = 1
        self.current_goal = "Explore the level"
        self.previous_screen = None

        try:
            self._wait_until_stable(timeout=6.0)
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}

        # answer the intro prompts so the caller lands in the dungeon
        for _ in range(8):
            raw, lines = self._capture_screen()
            if self._map_window(lines) is not None and not self._is_new_game_screen(raw):
                break
            if "Who are you?" in raw:
                self._type(character)
                self._send_keys("Enter")
            elif "Do you want your possessions identified?" in raw:
                self._send_keys("n")
            elif "Shall I pick" in raw:
                self._type("random")
                self._send_keys("Enter")
            elif "Hit return" in raw or "to continue" in raw:
                self._send_keys("Enter")
            elif "Ready to play" in raw:
                self._send_keys("y")
            elif "--More--" in raw:
                # Space is what this build's pager answers; a letter is eaten
                self._send_keys("Space")
            else:
                break
            time.sleep(1.0)

        # start memory from a clean frame: the map window shifts a row whenever
        # a message clears, and an origin taken mid-prompt would be off by one
        self.maps = {}
        state = self.get_state()
        state["status"] = "started"
        state["session_name"] = session_name
        self._persist_state(state)
        return state

    def send_command(self, command: str) -> dict[str, Any]:
        """Send keystrokes to NetHack and return the resulting state."""
        with self._lock:
            return self._run_command(command)

    def _run_command(self, command: str) -> dict[str, Any]:
        if self.session_name is None:
            return {"status": "error", "error": "Game not started. Call start_game first."}
        if not command:
            return {"status": "error", "error": "Empty command"}

        try:
            _, before_lines = self._capture_screen()
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}
        self.previous_screen = "\n".join(before_lines)
        before_map = self.maps.get(self.current_level)
        before_pos = before_map.player_world_pos if before_map else None

        # A paused message pager swallows whatever key you send it, so clear it
        # first and keep the text we skimmed past.
        cleared = self._drain_more(before_lines)
        if cleared:
            _, before_lines = self._capture_screen()
        pre_cleared = list(self.cleared_messages)
        pre_pager_diff = self.pager_diff

        paused = bool(self._open_prompt(before_lines))

        # A pure route (direction keys and o+dir pairs) is walked one key at a
        # time with the screen checked between each, instead of being typed
        # into the void and hoped for.
        tokens = None if paused else route_tokens(command)
        if tokens is not None and len(tokens) > 1 and before_pos is not None:
            routed = self._run_route(
                command, tokens, pre_cleared, pre_pager_diff
            )
            if routed is not None:
                return routed
            # position could not be verified before starting: fall through and
            # send the command whole, letting the screen re-anchor us

        expected_world = None
        if not paused and before_pos is not None:
            # not a verifiable route, but the keys may still walk: add the
            # steps up so the screen match has somewhere sensible to start
            # looking.  It is only a starting point - a blocked move makes it
            # wrong, and the screen decides either way.
            steps = route_displacement(command)
            if steps is not None and steps != (0, 0):
                expected_world = (before_pos[0] + steps[0], before_pos[1] + steps[1])
                if steps in KEY_DIRS.values():
                    # one step that would land on an @ the screen already
                    # drew is a bump, not an arrival: NetHack refuses the tile
                    # and certainly does not swap you with the shopkeeper
                    # standing in it.  Withholding the expected position lets
                    # the merge anchor on the @ that was ours last frame,
                    # which is the difference between "blocked, try around"
                    # and living the rest of the run one tile sideways.
                    b_off = before_map.dungeon_offset
                    wr = before_pos[0] - b_off[0] + steps[0]
                    wc = before_pos[1] - b_off[1] + steps[1]
                    if (
                        0 <= wr < len(before_lines)
                        and before_lines[wr][wc : wc + 1] == "@"
                    ):
                        expected_world = None

        try:
            for kind, payload in keys_for(command):
                if kind == "keys":
                    self._send_keys(payload)
                else:
                    self._type(payload)
        except subprocess.CalledProcessError as exc:
            return {"status": "error", "error": f"Failed to send command: {exc}"}

        prompt = self._open_prompt(before_lines)
        if not paused:
            self._mark_search(command)
        try:
            self._wait_until_stable(timeout=3.0)
            # A message this command produced can already be sitting on the
            # pager; drain it now, before the reply is built, instead of
            # leaving it to eat the next command's keys.
            _, after_lines = self._capture_screen()
            post_cleared = self._drain_more(after_lines)
            # merge *before* judging the move: "did it go nowhere" is a
            # question about where the map says the hero is *after* the
            # screen spoke, and answering it from the pre-merge position
            # reports every successful step as a block - and a false block
            # on drawn '#' would even mark the hero's new tile solid
            state = self.get_state(expected_world=expected_world)
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}

        notes = []
        cleared_all = list(dict.fromkeys(pre_cleared + post_cleared))
        if cleared_all:
            notes.append(
                "cleared {} waiting message(s): {}".format(
                    len(cleared_all), "; ".join(cleared_all)
                )
            )
        pager_diffs = [d for d in (pre_pager_diff, self.pager_diff if post_cleared else "") if d]
        if prompt:
            notes.append(
                f"the game is asking {prompt!r} - your key answered that, it did not move you"
            )
        else:
            after_map = self.maps.get(self.current_level)
            after_pos = after_map.player_world_pos if after_map else None
            note = self._note_move_result(command, before_pos, before_lines, after_pos)
            if note:
                notes.append(note)
        if notes:
            state["move_result"] = "\n".join(notes)
        if pager_diffs:
            state["pager_diff"] = "\n".join(pager_diffs)
        self._persist_state(state)
        return state

    def _run_route(
        self,
        command: str,
        tokens: list[str],
        pre_cleared: list[str],
        pre_pager_diff: str,
    ) -> dict[str, Any] | None:
        """Walk a frontier route one key at a time, verifying each step.

        Sending a route as one string is what turned two runs into coordinate
        archaeology: a message that fires mid-route pages on ``--More--``, and
        this build's pager destroys every letter typed at it (``more()`` waits
        on ``xwaitforspace`` in ``win/tty/topl.c`` - Space turns the page, all
        else is eaten).  So keys go out singly and each is checked against the
        screen before the next follows: the first key the game does not execute
        stops the route, the remaining keys are never typed, and the reply
        names how far the route actually got and why it stopped.

        Returns ``None`` when the map's hero position is not the ``@`` on
        screen - the disagreement itself is decided by the normal path.
        """
        dungeon_map = self.get_map()
        offset = dungeon_map.dungeon_offset
        hero_screen = (
            dungeon_map.player_world_pos[0] - offset[0],
            dungeon_map.player_world_pos[1] - offset[1],
        )
        _, lines = self._capture_screen()
        window = self._map_window(lines)
        if window is None:
            return None
        if not (
            0 <= hero_screen[0] < len(lines)
            and lines[hero_screen[0]][hero_screen[1] : hero_screen[1] + 1] == "@"
        ):
            return None  # memory and screen disagree before we even started
        prev_candidates = self._player_candidates(lines, window)

        notes: list[str] = []
        pager_diffs: list[str] = [d for d in (pre_pager_diff,) if d]
        cleared: list[str] = list(pre_cleared)
        consumed = 0
        pending = (0, 0)  # fast-pathed steps not yet merged into the map
        merged_world = dungeon_map.player_world_pos
        stop_reason = ""
        blocked_note = ""

        for index, token in enumerate(tokens, start=1):
            _, lines = self._capture_screen()
            if "--More--" in "\n".join(lines):
                cleared += self._drain_more(lines)
                if self.pager_diff:
                    pager_diffs.append(self.pager_diff)
                _, lines = self._capture_screen()
            question = self._open_prompt(lines)
            if question:
                stop_reason = (
                    f"stopped before key {index} ({token!r}): the game is "
                    f"asking {question!r}"
                )
                break
            world_before = (
                merged_world
                if pending == (0, 0)
                else (merged_world[0] + pending[0], merged_world[1] + pending[1])
            )
            try:
                self._type(token)
            except subprocess.CalledProcessError as exc:
                return {"status": "error", "error": f"Failed to send command: {exc}"}
            consumed += 1
            if len(token) > 1:  # 'o'+direction opens a door, moves nobody
                continue

            step = KEY_DIRS[token]
            want = (hero_screen[0] + step[0], hero_screen[1] + step[1])
            self._wait_until_stable(timeout=1.5, settle=0.12)
            _, lines = self._capture_screen()
            if self._extract_level(lines) not in (None, self.current_level):
                stop_reason = f"stopped after {index} key(s): the level changed"
                break
            window = self._map_window(lines)
            if window is None:
                asked = self._open_prompt(lines)
                stop_reason = f"stopped after {index} key(s): " + (
                    f"the game is asking {asked!r}" if asked else "the screen is not the map any more"
                )
                break
            candidates = self._player_candidates(lines, window)
            if candidates == [want]:
                # exactly one @ and it is where the key said: odometry holds,
                # the merge can wait for the end of the route
                hero_screen = want
                prev_candidates = candidates
                pending = (pending[0] + step[0], pending[1] + step[1])
                continue
            if want in prev_candidates and hero_screen in candidates:
                # that @ sat on `want` before this key, too - and NetHack
                # refuses to walk into a tile an @ already holds.  So the
                # hero did not move, the other @ was in the way; merging
                # without a key-implied position lets continuity keep us
                # where we were, and the block gets named below.
                info = self.update_map(lines, expected_world=None)
                pending = (0, 0)
                prev_candidates = candidates
                if info.get("updated") and info.get("player_world_pos"):
                    merged_world = info["player_world_pos"]
                    offset = self.get_map().dungeon_offset
                    hero_screen = (
                        merged_world[0] - offset[0],
                        merged_world[1] - offset[1],
                    )
                blocked_note = (
                    self._note_move_result(token, world_before, lines, merged_world) or ""
                )
                stop_reason = f"key {index} ({token!r}) did not move you"
                break
            # anything else - the viewport scrolled, the hero stayed, another
            # @ appeared beside the tile we wanted - is decided by the merge,
            # with the keys naming where it should start looking
            expected = (world_before[0] + step[0], world_before[1] + step[1])
            info = self.update_map(lines, expected_world=expected)
            pending = (0, 0)
            prev_candidates = candidates
            new_world = info.get("player_world_pos") if info.get("updated") else None
            if new_world is None:
                stop_reason = f"stopped after {index} key(s): the screen gave the map nothing to hold"
                break
            moved = new_world != world_before
            merged_world = new_world
            offset = self.get_map().dungeon_offset
            hero_screen = (new_world[0] - offset[0], new_world[1] - offset[1])
            if info.get("alignment_status") == "ambiguous":
                stop_reason = (
                    f"stopped after {index} key(s): two @ on this screen and it "
                    f"cannot say which is you - the map is unchanged"
                )
                break
            if not moved:
                blocked_note = self._note_move_result(token, world_before, lines, new_world) or ""
                stop_reason = f"key {index} ({token!r}) did not move you"
                break

        try:
            _, lines = self._capture_screen()
            if "--More--" in "\n".join(lines):
                cleared += self._drain_more(lines)
                if self.pager_diff:
                    pager_diffs.append(self.pager_diff)
        except RuntimeError:
            pass  # the game may have ended mid-route; get_state will report it

        if cleared:
            notes.append(
                "cleared {} waiting message(s): {}".format(
                    len(cleared), "; ".join(dict.fromkeys(cleared))
                )
            )
        if stop_reason:
            notes.append(
                f"route consumed {consumed} of {len(tokens)} keys ({sum(len(t) for t in tokens[:consumed])} of {len(command)} characters): {stop_reason}"
            )
            if blocked_note:
                notes.append(blocked_note)

        expected_world = None
        if pending != (0, 0):
            expected_world = (merged_world[0] + pending[0], merged_world[1] + pending[1])
        return self._finish_state(expected_world, notes, pager_diffs)

    def _mark_search(self, command: str) -> None:
        """A completed ``s`` (or ``2s``, ``20s``) searched the walls around
        where the hero stands; the ledger is what later turns "no exit here"
        into a checklist with a bounded end."""
        if not re.fullmatch(r"\d*s", command):
            return
        dungeon_map = self.maps.get(self.current_level)
        if dungeon_map is not None and dungeon_map.player_world_pos is not None:
            dungeon_map.mark_searched_around(dungeon_map.player_world_pos)

    def _finish_state(
        self,
        expected_world: tuple[int, int] | None,
        notes: list[str],
        pager_diffs: list[str],
    ) -> dict[str, Any]:
        try:
            state = self.get_state(expected_world=expected_world)
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}
        if notes:
            state["move_result"] = "\n".join(notes)
        if pager_diffs:
            state["pager_diff"] = "\n".join(pager_diffs)
        self._persist_state(state)
        return state

    def stop_game(self) -> dict[str, Any]:
        """Kill the tmux session and forget this game."""
        with self._lock:
            return self._stop_game()

    def _stop_game(self) -> dict[str, Any]:
        if self.session_name is not None:
            try:
                if self._session_alive():
                    self._tmux("kill-session", "-t", self.session_name, check=False)
            except Exception as exc:  # pragma: no cover - defensive
                _log_error("stop_game: killing session", exc)
        try:
            if self.state_file and os.path.exists(self.state_file):
                os.remove(self.state_file)
        except Exception as exc:  # pragma: no cover - defensive
            _log_error("stop_game: removing state file", exc)

        self.session_name = None
        self.state_file = None
        self.current_goal = ""
        self.current_level = 1
        self.maps = {}
        self.previous_screen = None
        self.game_state = {}
        return {"status": "stopped", "message": "Game stopped"}

    # ------------------------------------------------------------------ #
    #  state / output
    # ------------------------------------------------------------------ #

    def _create_diff(self, current: list[str], previous: list[str]) -> str:
        if not previous:
            return ""
        max_lines = max(len(current), len(previous))
        max_width = max(
            (max((len(l) for l in current), default=0)),
            (max((len(l) for l in previous), default=0)),
        )

        def cell(row: int, col: int) -> str:
            cur = current[row] if row < len(current) else ""
            old = previous[row] if row < len(previous) else ""
            c = cur[col] if col < len(cur) else " "
            o = old[col] if col < len(old) else " "
            if c == o:
                return " "
            # a cell that changed shows what is there now - and when the new
            # screen blanked something, what used to be there: a drained pager
            # line *disappears*, and a diff that only ever showed additions
            # would report the page turn as nothing at all
            return c if c != " " else o

        lines = []
        for row in range(max_lines):
            changed = "".join(cell(row, col) for col in range(max_width)).rstrip()
            if changed:
                lines.append(f"{row:2d}|{changed}")
        return "\n".join(lines)

    def _describe_position(self, dungeon_map: DungeonMap) -> str:
        pos = dungeon_map.player_world_pos
        if pos is None:
            return "unknown position"
        row, col = pos
        tile = dungeon_map.get_tile(row, col)
        terrain = tile.terrain if tile else None
        names = {
            ".": "floor",
            "#": "cave floor",
            "+": "doorway",
            "<": "stairs up",
            ">": "stairs down",
        }
        where = names.get(terrain or "", "unrecognised tile")
        if tile is not None and tile.doorway:
            where = "open doorway" if tile.door_open else "shut doorway"
        if tile is not None and tile.trap:
            where += " on a spotted TRAP"
        room = dungeon_map.room_at(row, col)
        suffix = f", in room {room.id}" if room else ""
        return f"world ({row},{col}) on {where}{suffix}"

    def _neighbour_hints(self, screen_lines: list[str]) -> str:
        """What the eight tiles around the player are, live and remembered.

        The hero's screen cell comes from the *committed* map position, not
        from rescanning for an ``@`` - with a shopkeeper in view the rescan and
        the memory can name different @s, and then the eight neighbours would
        be listed around the wrong one.
        """
        dungeon_map = self.maps.get(self.current_level)
        if dungeon_map is None or dungeon_map.player_world_pos is None:
            return ""
        offset = dungeon_map.dungeon_offset
        prow, pcol = (
            dungeon_map.player_world_pos[0] - offset[0],
            dungeon_map.player_world_pos[1] - offset[1],
        )
        if not (
            0 <= prow < len(screen_lines)
            and screen_lines[prow][pcol : pcol + 1] == "@"
        ):
            window = self._map_window(screen_lines)
            if window is None:
                return ""
            candidates = self._player_candidates(screen_lines, window)
            if not candidates:
                return ""
            prow, pcol = candidates[0]
        lines = []
        for step, key in DIR_KEYS.items():
            r, c = prow + step[0], pcol + step[1]
            on_screen = (
                screen_lines[r][c]
                if 0 <= r < len(screen_lines) and 0 <= c < len(screen_lines[r])
                else " "
            )
            world = (dungeon_map.dungeon_offset[0] + r, dungeon_map.dungeon_offset[1] + c)
            tile = dungeon_map.get_tile(*world)
            diagonal = step[0] != 0 and step[1] != 0
            if tile is not None and tile.solid:
                known, walkable = "X", "solid: you cannot walk here"
            elif tile is not None and tile.doorway:
                if tile.door_closed:
                    known, walkable = "+", f"door SHUT: 'o{key}' opens it"
                else:
                    known, walkable = "'", "doorway open: walk through"
            elif tile is not None and tile.memory:
                known = f"{tile.terrain} remembered"
                walkable = "walkable" if tile.passable else "wall"
            elif world in dungeon_map.frontier():
                known, walkable = "?", "UNREVEALED (maybe cave floor)"
            else:
                known, walkable = " ", "unknown"
            if tile is not None and tile.trap and walkable.startswith("walkable"):
                walkable += " but a TRAP is on it"
            if tile is not None and tile.terrain == LIQUID:
                walkable = "water/lava: you would swim"
            if diagonal and tile is not None and tile.doorway:
                walkable += " and no diagonal entry"
            # say something only where the screen and memory are worth
            # comparing: occupants (monsters, items) always, and any drawn
            # glyph that disagrees with what is remembered here
            live = ""
            if on_screen != " " and (
                on_screen not in VISIBLE_TERRAIN
                or (tile is not None and tile_matches(on_screen, tile) < 0)
            ):
                live = f" screen shows '{on_screen}'"
            lines.append(f"`{key}` {known:<12} {walkable}{live}")
        return "\n".join(lines)

    def _frontier_report(self, summary: dict[str, Any], limit: int = 6) -> str:
        groups = summary.get("frontier_targets") or []
        if not summary.get("frontier_areas"):
            return "none - everything within reach is remembered"
        lines = [
            "{} unrevealed reachable cell(s) in {} area(s):".format(
                summary.get("frontier_cells", 0), summary.get("frontier_areas", 0)
            )
        ]
        for index, group in enumerate(groups[:limit], start=1):
            stand_r, stand_c = group["stand"]
            probe = group.get("probe") or ""
            door = group.get("door_ahead")
            walk = group["route"] or "(you are there)"
            verb = f"open it with `{probe}`" if door else f"poke with `{probe}`"
            where = (
                f"beyond the door at ({door[0]},{door[1]})"
                if door
                else f"next to ({stand_r},{stand_c})"
            )
            notes = []
            if group.get("door_shut"):
                notes.append("shut door: 'o'+direction, or kick it ('^D')")
            elif group.get("through_door") and not door:
                notes.append("a door on the way: it may be locked")
            if group.get("trap_on_route"):
                notes.append("a TRAP is on the way")
            if group.get("on_stairs"):
                notes.append("stairs")
            tag = f" [{'; '.join(notes)}]" if notes else ""
            lines.append(
                f"{index}. {group['cells']} cell(s) {where}, "
                f"{group['steps']} step(s): {walk}, {verb}{tag}"
            )
        if len(groups) > limit:
            lines.append(f"... {len(groups) - limit} more, further away")
        return "\n".join(lines)

    def get_state(self, expected_world: tuple[int, int] | None = None,
                  advance_baseline: bool = True) -> dict[str, Any]:
        """Full game state: screen, map memory, frontier, hints.

        ``advance_baseline`` is False for the fullscreen watch poller, which
        must not steal the diff baseline from between real tool calls.
        """
        with self._lock:
            return self._collect_state(expected_world, advance_baseline)

    def _collect_state(self, expected_world: tuple[int, int] | None = None,
                       advance_baseline: bool = True) -> dict[str, Any]:
        raw_text, screen_lines = self._capture_screen()

        if self._is_new_game_screen(raw_text):
            self.maps = {}

        new_level = self._extract_level(screen_lines)
        if new_level is not None and new_level != self.current_level:
            self.current_level = new_level

        info = self.update_map(screen_lines, expected_world=expected_world)
        # The map and the player's place on it are always part of the answer:
        # the agent guides itself by them and should never have to ask for a
        # map it then finds missing.  A level nothing was learned of yet
        # renders "(no memory yet)"; a screen that is not the map (menu,
        # prompt) still gets back the memory as it stands.
        dungeon_map = self.get_map()
        if info.get("alignment_status") == "ambiguous":
            candidates = info.get("hero_candidates") or []
            state_warning = (
                "AMBIGUOUS position: the @ on screen could be you at "
                + " or ".join(f"({c[0]},{c[1]})" for c in candidates)
                + "; memory was NOT rewritten. Take one single-key step, or run "
                "`#lookaround` from where you stand - the @ that moves is you"
            )
        elif info.get("conflicts"):
            state_warning = (
                f"{info['conflicts']} of the {info['coverage']} remembered tile(s) this "
                f"screen covers disagree with it (alignment {info.get('alignment')}): the "
                f"map may be misaligned, so distrust the route until it settles"
            )
        else:
            state_warning = ""

        diff = ""
        if self.previous_screen:
            diff = self._create_diff(screen_lines, self.previous_screen.split("\n"))
        if advance_baseline:
            self.previous_screen = raw_text

        state: dict[str, Any] = {
            "status": "playing",
            "screen": raw_text.rstrip("\n"),
            "screen_lines": screen_lines,
            "session_name": self.session_name,
            "level": self.current_level,
            "map_updated": info.get("updated", False),
            "diff": diff,
        }
        if state_warning:
            state["map_warning"] = state_warning
        state["alignment"] = {
            "status": info.get("alignment_status", "locked"),
            "alternatives": info.get("alternatives", []),
        }
        summary = dungeon_map.describe()
        state["player_world_pos"] = summary["player_world_pos"]
        state["map_summary"] = summary
        if (
            info.get("updated")
            and summary.get("frontier_cells", 0) == 0
            and not summary.get("downstairs")
            and summary.get("remembered_tiles", 0) >= 30
        ):
            # "nothing left to explore" and "no way out found" together are not
            # a finished level - they are the level that eats runs.  Say so,
            # with the checklist that ends it.
            state["exit_warning"] = (
                "NO EXIT remembered on this level: frontier empty and no '>' "
                "ever drawn. {} wall tile(s) touching reachable ground have "
                "never been searched - stand next to each and `2s`, cross-check "
                "with `#terrain` or `#lookaround` for stairs the map missed, and "
                "if it stays dry go back up (`<`) and hunt a shaft instead of "
                "orbiting these walls.".format(summary.get("unsearched_walls", 0))
            )
        hints = []
        if self.current_goal:
            hints.append(f"Goal: {self.current_goal}")
        neighbours = self._neighbour_hints(screen_lines)
        if neighbours:
            hints.append("NEIGHBOURS\n" + neighbours)
        hints.append(f"YOU ARE: {self._describe_position(dungeon_map)}")
        state["hints"] = "\n\n".join(hints)
        state["map"] = dungeon_map.render()
        state["frontier"] = self._frontier_report(summary)

        awaiting = self._awaiting(screen_lines)
        if awaiting:
            state["awaiting"] = awaiting
            state["prompt"] = awaiting["note"]

        self.game_state = state
        return state

    def map_payload(self) -> dict[str, Any]:
        """Map-only payload (no screen capture)."""
        dungeon_map = self.get_map()
        summary = dungeon_map.describe()
        return {
            "status": "success",
            "level": self.current_level,
            "map": dungeon_map.render(),
            "frontier": self._frontier_report(summary),
            "summary": summary,
            "player": self._describe_position(dungeon_map),
        }

    # ------------------------------------------------------------------ #
    #  persistence (survives a server restart while the game keeps running)
    # ------------------------------------------------------------------ #

    def _new_session_name(self) -> str:
        import random
        import string

        suffix = "".join(random.choices(string.ascii_lowercase, k=8))
        return f"{self.TMUX_SESSION_PREFIX}{suffix}"

    def _new_state_file(self) -> str:
        import random
        import string

        suffix = "".join(random.choices(string.ascii_lowercase, k=8))
        return f"{self.STATE_FILE_PREFIX}{suffix}.json"

    def _persist_state(self, state: dict[str, Any]) -> None:
        if not self.state_file:
            return
        payload = {
            "session_name": self.session_name,
            "state_file": self.state_file,
            "current_goal": self.current_goal,
            "current_level": self.current_level,
            "player_world_pos": state.get("player_world_pos"),
            "maps": {str(level): m.to_dict() for level, m in self.maps.items()},
            "previous_screen": self.previous_screen,
        }
        try:
            with open(self.state_file, "w") as handle:
                json.dump(payload, handle)
        except Exception as exc:  # pragma: no cover - defensive
            _log_error("_persist_state", exc)

    def _restore_state(self) -> None:
        import glob

        files = glob.glob(f"{self.STATE_FILE_PREFIX}*.json")
        if not files:
            return
        # the *most recently written* state: several files sit around whenever
        # games were started without nethack_stop, and the alphabetically
        # oldest one is a different, probably dead, game
        files.sort(key=os.path.getmtime)
        newest = files[-1]
        try:
            with open(newest) as handle:
                state = json.load(handle)
        except Exception as exc:
            _log_error("_restore_state: reading", exc)
            return

        session = state.get("session_name")
        if not session:
            return
        self.session_name = session
        self.state_file = newest
        self.current_goal = state.get("current_goal", "")
        self.current_level = int(state.get("current_level", 1))
        self.previous_screen = state.get("previous_screen")
        for level_str, map_data in (state.get("maps") or {}).items():
            try:
                self.maps[int(level_str)] = DungeonMap.from_dict(map_data)
            except Exception as exc:
                _log_error("_restore_state: map level", exc)

        if not self._session_alive():
            # the game died but the memory may still be useful
            print(
                f"[INFO] restored state for dead tmux session {session}",
                file=sys.stderr,
                flush=True,
            )


# ---------------------------------------------------------------------- #
#  MCP plumbing
# ---------------------------------------------------------------------- #

server = Server("nethack-mcp", version="0.2.0", instructions=SERVER_INSTRUCTIONS)
_client: NetHackClient | None = None
_fullscreen = False


def get_client() -> NetHackClient:
    """One client per server process; the game lives in tmux anyway."""
    global _client
    if _client is None:
        _client = NetHackClient()
    return _client


def _format_state(state: dict[str, Any]) -> str:
    if state.get("status") == "error":
        return f"ERROR: {state.get('error')}"
    parts = []
    if state.get("pager_diff"):
        parts += [
            "=== PAGER (skipped while the --More-- pages were drained) ===",
            state["pager_diff"],
            "",
        ]
    parts += ["=== SCREEN ===", state.get("screen", "")]
    if state.get("move_result"):
        parts += ["", "=== MOVE ===", state["move_result"]]
    if state.get("map_warning"):
        parts += ["", f"WARNING: {state['map_warning']}"]
    if state.get("exit_warning"):
        parts += ["", f"WARNING: {state['exit_warning']}"]
    if state.get("hints"):
        parts += ["", state["hints"]]
    header = f"=== MAP (level {state.get('level')}) ==="
    pos = state.get("player_world_pos")
    if pos:
        header = f"=== MAP (level {state.get('level')}, you at ({pos[0]},{pos[1]})) ==="
    parts += ["", header, state.get("map", "(no memory yet)").rstrip("\n")]
    if state.get("frontier"):
        parts += ["", "=== FRONTIER (?) ===", state["frontier"]]
    summary = state.get("map_summary") or {}
    if summary:
        stairs_down = summary.get("downstairs") or []
        if stairs_down:
            stair_text = ", ".join(f"({p[0]},{p[1]})" for p in stairs_down)
        else:
            stair_text = "NEVER SEEN"
        parts += [
            "",
            "remembered {r} tiles, visited {v}, {f} unrevealed reachable cells; "
            "stairs down {d}".format(
                r=summary.get("remembered_tiles", 0),
                v=summary.get("visited_tiles", 0),
                f=summary.get("frontier_cells", 0),
                d=stair_text,
            ),
        ]
    awaiting = state.get("awaiting")
    if awaiting:
        block = [f"=== AWAITING ({awaiting['kind']}) ==="]
        if awaiting.get("question"):
            block.append(awaiting["question"])
        block.append("safe to send: " + awaiting["reply"])
        parts += ["", "\n".join(block)]
    elif state.get("prompt"):
        parts += ["", f"NOTE: {state['prompt']}"]
    if state.get("diff"):
        parts += ["", "=== DIFF ===", state["diff"]]
    return "\n".join(parts)


def _tool(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    def tool(name: str, description: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> types.Tool:
        return types.Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
        )

    return [
        tool(
            "nethack_start",
            "Start a new NetHack game (answers the character-name prompts for you)",
            {
                "character": {
                    "type": "string",
                    "description": "Character name, default 'Hero'",
                }
            },
        ),
        tool(
            "nethack_move",
            "Send keystrokes to NetHack and return the screen, memory map and frontier. "
            "vi-keys: h j k l y u b n (uppercase to run), '.' wait, ',' pick up, ':' look.",
            {
                "command": {
                    "type": "string",
                    "description": "Keystrokes, e.g. 'h', 'jj', 'mh', 'Enter', '#inventory'",
                }
            },
            ["command"],
        ),
        tool(
            "nethack_state",
            "Current screen, memory map, frontier and hints",
        ),
        tool(
            "nethack_map",
            "Just the memory map of the current level, its frontier targets and rooms",
        ),
        tool(
            "nethack_diff",
            "Characters that changed on screen since the previous call",
        ),
        tool(
            "nethack_set_goal",
            "Record what you are currently trying to do (shown with every state)",
            {"goal": {"type": "string", "description": "Free-form goal text"}},
            ["goal"],
        ),
        tool("nethack_stop", "Stop the game and discard the maps"),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    client = get_client()
    try:
        if name == "nethack_start":
            character = arguments.get("character") or "Hero"
            state = await asyncio.to_thread(client.start_game, character)
            if _fullscreen:
                _show_fullscreen(state)
            return _tool(_format_state(state))

        if name == "nethack_move":
            command = arguments.get("command")
            if not command:
                return _tool("ERROR: 'command' is required")
            state = await asyncio.to_thread(client.send_command, command)
            if _fullscreen:
                _show_fullscreen(state)
            return _tool(_format_state(state))

        if name == "nethack_state":
            state = await asyncio.to_thread(client.get_state)
            if _fullscreen:
                _show_fullscreen(state)
            return _tool(_format_state(state))

        if name == "nethack_map":
            payload = await asyncio.to_thread(client.map_payload)
            if payload.get("status") == "error":
                return _tool(f"ERROR: {payload.get('error')}")
            body = [
                f"MAP level {payload['level']}  ({payload['player']})",
                payload["map"].rstrip("\n"),
                "",
                "FRONTIER (?) - unrevealed cells you can walk to:",
                payload["frontier"],
            ]
            summary = payload["summary"]
            if summary.get("rooms"):
                body.append("")
                body.append("ROOMS")
                for room in summary["rooms"]:
                    min_r, max_r, min_c, max_c = room["bounds"]
                    doors = ", ".join(f"({d[0]},{d[1]})" for d in room["doors"]) or "none"
                    body.append(
                        f"  room {room['id']}: rows {min_r}..{max_r} cols {min_c}..{max_c} "
                        f"({room['size']} floor) doors: {doors}"
                    )
            return _tool("\n".join(body))

        if name == "nethack_diff":
            state = await asyncio.to_thread(client.get_state)
            if not state.get("diff"):
                state["diff"] = "no changes since the previous read"
            if _fullscreen:
                _show_fullscreen(state)
            return _tool(_format_state(state))

        if name == "nethack_set_goal":
            client.current_goal = arguments.get("goal", "")
            client._persist_state(client.game_state or {})
            return _tool(f"Goal set: {client.current_goal}")

        if name == "nethack_stop":
            result = await asyncio.to_thread(client.stop_game)
            return _tool(json.dumps(result))

        return _tool(f"ERROR: unknown tool '{name}'")
    except Exception as exc:
        import traceback

        _log_error(f"tool {name}", exc)
        return _tool(f"Exception: {exc}\n{traceback.format_exc()}")


def _show_fullscreen(state: dict[str, Any]) -> None:
    """Redraw in place: home the cursor and overwrite, erasing each line's
    tail and whatever the previous frame left below - never clear the whole
    screen, so the watch does not flicker between frames."""
    out = ["\033[H"]
    out += [f"{line}\033[K\n" for line in _format_state(state).split("\n")]
    out.append("\033[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


async def _watch_loop(client: NetHackClient, interval: float = 1.0) -> None:
    """Fullscreen watch: redraw the screen and the memory map live, whether
    or not the agent has acted lately.  Reading merges the screen into memory
    (that is how the map grows while you watch) but never touches the diff
    baseline real tool calls compare against."""
    while True:
        try:
            state = await asyncio.to_thread(
                client.get_state, None, False  # expected_world, advance_baseline
            )
            _show_fullscreen(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - game simply not running yet
            _show_fullscreen(
                {"status": "playing", "screen": f"(no game to watch: {exc})", "level": None}
            )
        await asyncio.sleep(interval)


def _capabilities() -> types.ServerCapabilities:
    caps = server.get_capabilities(
        notification_options=NotificationOptions(),
        experimental_capabilities={},
    )
    caps.tools = types.ToolsCapability(listChanged=False)
    return caps


async def run() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="nethack-mcp",
                server_version="0.2.0",
                capabilities=_capabilities(),
                instructions=SERVER_INSTRUCTIONS,
            ),
        )


async def run_sse(port: int = 8000, host: str = "0.0.0.0", fullscreen: bool = False) -> None:
    """HTTP transport: /mcp (streamable) plus the legacy /sse endpoints."""
    import uvicorn
    from starlette.responses import PlainTextResponse

    global _fullscreen
    _fullscreen = fullscreen

    sse = mcp.server.sse.SseServerTransport("/messages/")

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        stateless=True,
    )

    async def handle_streamable_http(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="nethack-mcp",
                    server_version="0.2.0",
                    capabilities=_capabilities(),
                    instructions=SERVER_INSTRUCTIONS,
                ),
            )

    async def app(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/sse" and scope["method"] == "GET":
                await handle_sse(scope, receive, send)
                return
            if path == "/health" and scope["method"] == "GET":
                await PlainTextResponse("ok")(scope, receive, send)
                return
            if path == "/mcp":
                await handle_streamable_http(scope, receive, send)
                return
            if path.startswith("/messages/") and scope["method"] == "POST":
                await sse.handle_post_message(scope, receive, send)
                return
        await PlainTextResponse("Not found", status_code=404)(scope, receive, send)

    print(f"NetHack MCP server on http://{host}:{port}/mcp (and /sse)", flush=True)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    watch: asyncio.Task | None = None
    if fullscreen:
        watch = asyncio.create_task(_watch_loop(get_client()))
    async with session_manager.run():
        try:
            await uvicorn.Server(config).serve()
        finally:
            if watch is not None:
                watch.cancel()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="NetHack MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help=(
            "watch mode (SSE transport): keep redrawing screen + memory map live "
            "in this terminal, overwriting in place"
        ),
    )
    args = parser.parse_args()

    if args.fullscreen and args.transport != "sse":
        print(
            "[INFO] --fullscreen needs --transport sse: on stdio stdout carries the protocol",
            file=sys.stderr,
        )

    if args.transport == "sse":
        asyncio.run(run_sse(port=args.port, host=args.host, fullscreen=args.fullscreen))
    else:
        asyncio.run(run())


if __name__ == "__main__":
    main()
