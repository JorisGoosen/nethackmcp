# NetHack MCP Server

An MCP server that lets an agent play NetHack.  It drives NetHack inside a tmux
session, reads the tty screen, and keeps a **memory map** of every level so the
agent knows where it has been and where there is still something to discover.

## Run

```bash
uv sync
uv run python run_server.py                       # stdio
uv run python run_server.py --transport sse --port 8000   # http, /mcp and /sse
```

Point a client at `http://host:8000/mcp` (streamable HTTP) or `/sse` (legacy).
Requires `nethack` and `tmux` on the host.

## Tools

| tool | what it does |
|---|---|
| `nethack_start` | start a game, answering the name/prompt screens for you |
| `nethack_move`  | send keystrokes; returns screen, memory map, frontier and hints |
| `nethack_state` | the same view without sending anything |
| `nethack_map`   | just the memory map, frontier routes and rooms |
| `nethack_diff`  | characters that changed on screen since the last read |
| `nethack_set_goal` | record what you are trying to do; shown with every state |
| `nethack_stop`  | kill the tmux session and discard the maps |

A pending `--More--` pager is drained automatically (with Space, since letters
do not dismiss it) before your command is sent, and the skipped message text is
reported back.

## Keys

The agent-facing command list lives in `SERVER_INSTRUCTIONS` in
`src/nethackmcp/server.py` and was checked against the command table
(`src/cmd.c`) and the tty symbol table (`include/defsym.h`) of the NetHack tree
next to this one.  This build plays with vi keys (`number_pad` off).  The easily
guessed ones: `e` eat but `E` engrave, `q` drink but `Q` choose ammo, `t` throw
but `T` take off armor, `F`+dir fights (`f`+dir fires the quiver), `Z` casts a
spell (`m` is the move-without-picking-up prefix), `_` travels, `#herecmdmenu`
lists what you can do here - and there is no `#knock`.

## The map

The map records **memory**, not the real level.

```
@  you            .  floor            #  cave floor / corridor
+  door, SHUT     '  doorway, open    ^  a trap we have spotted
<  stairs up      >  stairs down      }  water or lava
|  -  walls seen  X  solid (a bump proved it)
?  unrevealed, but reachable          (blank)  nothing remembered, nothing to find
```

Four rules make it trustworthy to navigate by:

1. **A blank is never a wall.** tty NetHack draws unexplored space and permanent
   rockwall both as blank, and a blank beside you can turn into cave floor the
   moment you step up to it.  So blanks are never stored as terrain.
2. **Adjacency settles a `?`.** When you stand next to an unrevealed tile it
   becomes real: whatever glyph is drawn there (`.`, `#`, `+`, `<`, `>`) or, if
   it stays blank, a confirmed `X`.  A tile showing a monster or item becomes
   walkable ground - nothing is ever drawn inside rockwall.
3. **Door state comes from the glyph** (`include/defsym.h`): `+` is a door that
   is *shut*, an *open* door is drawn as the wall it sits in (`|`/`-`) and a
   doorless doorway as `.`.  A position once seen as `+` stays a doorway forever
   and renders `+` while shut, `'` while open - so the map says whether you can
   walk through it or have to `o`pen it first.
4. **A `?` is only shown where you could actually go.** It must touch ground you
   can reach over remembered terrain, otherwise it is not an exploration target.
   Reachability also honours NetHack's rule that a diagonal step into or out of
   an intact doorway is refused, so a route never promises a move the game will
   not make.

`FRONTIER` lists, per unrevealed area, the tile to stand on, the steps and keys
to get there and the one key that pokes the area into reality:

```
1. 24 cell(s) next to (0,-1), 8 step(s): hhbhhbhy, poke with `b`
2. 19 cell(s) beyond the door at (-2,7), 0 step(s): (you are there), open it with `oj`
```

Sending route+probe as a single `nethack_move` therefore always learns
something.  Routes carry `o<dir><dir>` where they must open a shut door on the
way, and warn when the way in is locked or a trap lies on the path.

## Position tracking

World position is `dungeon_offset + screen_position`, origin where you first
appeared on a level.  The offset is re-derived on every screen by matching the
drawn terrain against remembered geometry:

* an offset is only usable when it contradicts nothing that is remembered, which
  stops a wrong alignment from smearing walls over the map;
* the keyboard has the last word when it is consistent - one vi-key is exactly
  one tile - so flat corridors with nothing distinctive to match cannot drift;
* the map window is *detected*, not assumed: its height changes with the message
  window, and because the map is centred on the player the whole dungeon shifts
  a row when a message clears.

Screens that are not the dungeon (menus, prompts, death screens) are recognised
and never read as terrain.

## Tests

```bash
uv run pytest tests/ -q
```

`tests/fixtures/level1_screen.txt` is a real 80x24 capture used to check window
detection, ingestion and scrolling.
