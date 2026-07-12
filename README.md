# NetHack MCP Server

MCP server for playing NetHack via AI interaction.

## Installation

```bash
uv sync
```

## Usage

```bash
uv run python run_server.py
```

## Tools

- `nethack_start` - Start a new game
- `nethack_move` - Send moves/commands
- `nethack_state` - Get game state
- `nethack_stop` - Stop game
- `nethack_set_goal` - Set AI player goal
- `nethack_diff` - View screen changes since last move