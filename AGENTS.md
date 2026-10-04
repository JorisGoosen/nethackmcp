# AGENTS.md

An MCP server that plays NetHack: it drives the game in a tmux session, reads the
tty screen, and keeps a memory map the *playing agent* can navigate by.
Requires `nethack` and `tmux` on the host; Python >= 3.10 via `uv`.

## Commands

```bash
uv sync
uv run pytest tests -q                                  # whole suite
uv run pytest tests/test_mapping.py::test_name -q       # one test
uv run ruff check src tests --ignore BLE001,PLR0402     # lint gate
uv run python run_server.py                             # stdio transport
uv run python run_server.py --transport sse --port 8000 # http: /mcp, /sse, /health
```

The repo ships **no ruff config**: `BLE001`/`PLR0402` come from machine-level
settings and the 7 hits they produce are pre-existing (blind `except` in the tmux
plumbing, `import mcp.types as types`). Run the command above; do not "fix" those
in unrelated work.

## Shape

- `src/nethackmcp/map.py` - the whole mapping model, pure and I/O-free: `Tile`,
  `DungeonMap`, and the alignment scorer (`tile_matches`, `score_offset`,
  `solve_offset`). Correctness lives here and is tested offline against real
  captures. Keep it that way.
- `src/nethackmcp/server.py` - `NetHackClient` (tmux capture, keystroke sending,
  pager/prompt handling), the MCP surface (`@server.list_tools()` plus the
  matching branch in the call handler; `_format_state` builds what the player
  reads), and `SERVER_INSTRUCTIONS`.

There are **two audiences**. The README describes the server to a human;
`SERVER_INSTRUCTIONS` in `server.py` is the system prompt handed to the agent
*playing* the game. Change map semantics and you must update both - the player
reasons with that text and nothing else.

## NetHack facts: check the source, do not recall them

`../NetHack` is a full source tree. `include/defsym.h` is the glyph table,
`src/cmd.c` the `extcmdlist`, `src/hack.c` the movement rules. Verified the hard
way, and all surprising:

- `+` is a door that is **shut**. An **open** door is drawn with the wall it sits
  in: `-` for a vertical wall, `|` for a horizontal one - the same characters
  walls use, so only memory can tell them apart. A doorless doorway is `.`.
- `#` is cave corridor *and* iron bars *and* a tree. `-` `|` `/` `\` `*` `(` `)`
  are also transient beam / explosion / swallow glyphs, drawn **over rockwall
  too**: they say nothing about the tile.
- `%` is a food item, `k` a kobold, `:` a lizard - a monster or item glyph is
  proof of ground, but only because it cannot live in rockwall. The `@` glyph
  is **not**: shopkeepers, pirates and human-shaped statues draw `@` too
  (`MONSYM` HUMAN), and `I` (last *heard* position) is drawn over rockwall and
  proves nothing.
- `#force` only ever forces the lock of a **box at your feet** (`doforce()` in
  `src/lock.c` - it asks `[ynq]`, needs a proper melee weapon, and carries a
  literal TODO about doors). There is **no** direction prompt to answer. A
  locked door is opened by kicking (`^D`+dir).
- `#terrain` exists in this build (`doterrain` in `src/cmd.c`): it draws the
  level as the game itself remembers it - the cross-check when map and world
  disagree.
- `#` is this build's extended-command key; there is **no** `#knock`.
- `--More--` is dismissed by Space only - letters do nothing, so a movement key
  sent while the pager is up is lost. The server drains it before sending, and
  walks multi-key routes one verified key at a time so a message mid-route can
  no longer eat the tail.
- NetHack refuses a **diagonal** step into or out of an intact doorway
  (`has_door_leaf()` gating `step_ok()`), so routes must not plan one. It also
  refuses to walk onto a tile an `@` stands on: a step towards a visible `@` is
  a bump, never an exchange of places.

## Live verification

The map is worth checking against a real game, not only fixtures: start the
server on :8000 and drive it through the tools.

- The game lives in tmux as `nethack_*`; the server persists to
  `/tmp/nethack_state_<session>.json` (`maps`, `previous_screen`,
  `session_name`). That file is the fastest way to debug one bad screen offline -
  load it and re-run the scorer instead of replaying moves.
- Restart with `fuser -k 8000/tcp`. Never `pkill -f`/`pgrep -f`: the pattern
  matches the agent's own command line.
- Restarting the server keeps the game and its maps. **Deleting the state file
  throws away a live game** - do it deliberately, not to debug.
- Capture several moves per tool call when sweeping (a route string is one call),
  and assert the invariants below in code rather than reading renders.

tmux `capture-pane -p` **trims each row's trailing blanks**, so rows have ragged
lengths and "past the end of a row" is a real blank (rockwall), not unknown.
Tests that simulate scrolling pad to a fixed grid first - see `scroll()` in
`tests/test_server_screen.py`.

To merge a screen the way the server does, adopt the depth first: `update_map`
does **not** switch levels, `get_state` sets `current_level` from `Dlvl:` before
calling it. The `load()` helper mirrors this.

## Invariants (each one has a test)

1. A blank is never stored as a wall.
2. A `?` next to the player resolves the moment it is next to the player - into
   drawn terrain, into ground under an occupant, or into confirmed `X`.
   A *guessed* tile that shows blank next to the player retires the guess first
   (the occupant was a transient effect over rockwall) and then confirms `X`;
   a stale guess never counts as a conflict against an alignment.
3. Nothing is `?` unless a reachable path leads adjacent to it.
4. **The screen decides the alignment; the keys only say where to start looking.**
   Search rings outward from the key-implied position and the previous offset, a
   candidate must cover enough remembered memory to be trusted, and a clean fit
   wins - but a *tie keeps the offset already held*: two symmetric readings must
   never flip the map between them (the D3 shop brick). When the map would have
   to *move* onto an alignment another reading explains equally well, the merge
   reports `alignment_status: ambiguous` and touches no memory.
5. Remembered terrain is not erased by a blank. A disagreement is reported to the
   player and the screen is merged without overwriting memory (memory is only
   rewritten when conflicts are zero).
6. Each level keeps its own map and origin; a level never seen anchors at the
   player and keeps that origin as the viewport slides.
7. **The hero is the `@` that memory and continuity agree is moving.** Every
   `@` in the window is offered to the merge as a candidate; the reading kept
   is the one that contradicts memory least, and among equals the one on the
   committed alignment at the position the keys imply. A step towards an `@`
   that was already drawn there is a bump - the game refuses the tile and the
   map must not put the hero on it. `I` is not an occupant (sound reaches
   through rockwall) and is never remembered as ground.
8. Multi-key routes are verified one key at a time: nothing is typed after a
   key the game did not execute, and the MOVE note counts what was consumed.

## How to work here

- When screen and map disagree, **write the comparison instead of eyeballing
  pane dumps**. Counting columns off a raw capture by hand produced several
  false alarms; a ten-line script over the state file found the real cause in one
  run.
- When a driver or probe script misbehaves, suspect the script before the
  product. A stalling frontier loop was a broken regex, not a broken map.
- A single-tile conflict on a guessed tile is a known weak signal: note it, keep
  the session's goal moving, and do not let one tile become the task.
- Prefer one more test to one more live sweep: the fixtures are real captures, so
  a regression you can pin down usually pins down in `tests/` too.
