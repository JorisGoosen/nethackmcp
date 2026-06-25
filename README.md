# NetHack MCP Server

MCP server voor het spelen van NetHack via AI-interactie.

## Installatie

```bash
uv sync
```

## Gebruik

```bash
uv run python src/nethackmcp.py
```

## Capabilities

- `nethack_start` - Start een nieuw spel
- `nethack_move` - Stuur moves/commands
- `nethack_state` - krijg game state
- `nethack_stop` - Stop spel
