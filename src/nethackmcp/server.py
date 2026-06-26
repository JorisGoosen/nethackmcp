"""MCP server for playing NetHack."""

from __future__ import annotations

import asyncio
import json
import pty
import subprocess
import sys
import os
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

# ============================================================================
# Server instructions — shown to the AI client to guide usage
# ============================================================================

SERVER_INSTRUCTIONS = """\
You are connected to NetHack through this MCP server.

## NetHack Commands (short):

- **Direction**: h(←) j(↓) k(↑) l(→) y(↖) u(↗) b(↙) n(↘)
- **Move one step**: [direction]
- **Move far**: [DIR] (uppercase)
- **Move silent**: m+[direction]
- **Pick up**: ,
- **Drop**: d
- **Fight**: f+[dir]
- **Look**: :
- **Search**: s
- **Wait/Rest**: .
- **Inventory**: i
- **Equipment**: *
- **Status**: ^X (Ctrl+x)
- **Extended commands**: #command
- **Help**: ?

## Workflow:

1. Start a game with nethack_start
2. Use nethack_move to send commands
3. Check screen state with nethack_state to see what's happening
4. Stop with nethack_stop when done
"""


# ============================================================================
# NetHack client
# ============================================================================


class NetHackClient:
    """Handles communication with NetHack."""
    
    def __init__(self):
        self.process: Any = None
        self.game_state: dict = {}
        
    def start_game(self, character: str = "random") -> dict:
        """Start a new NetHack game with random character."""
        cmd = ["nethack", "-r"]
        
        # Use pty for proper terminal output (curses support)
        self.master_fd, self.slave_fd = pty.openpty()
        
        self.process = subprocess.Popen(
            cmd,
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            text=False,  # Binary mode for pty
            bufsize=0,
            close_fds=True
        )
        
        os.close(self.slave_fd)
        
        self.game_state = {
            "status": "started",
            "screen": "",
            "message": "Game started with random character. Waiting for screen..."
        }
        
        return self.game_state
    
    def send_command(self, command: str) -> dict:
        """Send a command to NetHack."""
        if not self.process:
            return {"error": "Game not started. Call start_game first."}
        
        # Write command to pty
        try:
            import select
            import time
            
            # Send command
            os.write(self.master_fd, (command + "\n").encode())
            
            # Read output from pty
            output_bytes = b""
            while True:
                readable, _, _ = select.select([self.master_fd], [], [], 2.0)
                if readable:
                    chunk = os.read(self.master_fd, 4096)
                    if chunk:
                        output_bytes += chunk
                        # Check if we see the prompt
                        if b">" in chunk:
                            break
                    else:
                        break
                else:
                    break
            
            # Decode and strip ANSI codes
            output = output_bytes.decode('utf-8', errors='replace')
            screen = self._strip_ansi(output)
            
            self.game_state = {
                "status": "playing",
                "command": command,
                "response": output,
                "screen": screen,
                "full_output": output
            }
            return self.game_state
        except Exception as e:
            return {"error": str(e)}
    
    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape codes from text."""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)
    
    def get_state(self) -> dict:
        """Get current game state."""
        return self.game_state
    
    def stop_game(self) -> dict:
        """Stop the current game."""
        if self.process:
            self.process.terminate()
            self.process = None
            if hasattr(self, 'master_fd'):
                os.close(self.master_fd)
            return {"status": "stopped"}
        return {"error": "No game running"}


server = Server("nethack-mcp")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available NetHack tools."""
    return [
        types.Tool(
            name="nethack_start",
            description="Start a new NetHack game with specified character",
            inputSchema={
                "type": "object",
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "Character name (e.g., 'human', 'elf', 'dwarf')"
                    }
                },
                "required": ["character"]
            }
        ),
        types.Tool(
            name="nethack_move",
            description="Send a move/command to NetHack (direction or action)\n\n## NetHack Commands (short):\n- Direction: h(←) j(↓) k(↑) l(→) y(↖) u(↗) b(↙) n(↘)\n- Move: [direction], Move far: [DIR], Move silent: m+[direction]\n- Pick up: ,  Drop: d  Fight: f+[dir]\n- Look: :  Search: s  Wait/Rest: .\n- Inventory: i  Equipment: *  Status: ^X\n- Extended: #command  Help: ?",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to send (movement keys, actions, or extended commands starting with #)"
                    }
                },
                "required": ["command"]
            }
        ),
        types.Tool(
            name="nethack_state",
            description="Get current game state",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="nethack_stop",
            description="Stop current NetHack game",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    """Call a NetHack tool."""
    client = NetHackClient()
    
    try:
        if name == "nethack_start":
            character = arguments.get("character", "human")
            result = await asyncio.to_thread(client.start_game, character)
            result["screen"] = result.get("full_output", "")
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "nethack_move":
            command = arguments.get("command")
            if not command:
                return [types.TextContent(type="text", text="Error: Command is required")]
            result = await asyncio.to_thread(client.send_command, command)
            result["screen"] = result.get("full_output", "")
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "nethack_state":
            state = await asyncio.to_thread(client.get_state)
            state["screen"] = state.get("full_output", "")
            return [types.TextContent(type="text", text=json.dumps(state, indent=2))]
        
        elif name == "nethack_stop":
            result = await asyncio.to_thread(client.stop_game)
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        
        else:
            return [types.TextContent(type="text", text=f"Error: Unknown tool: {name}")]
    
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def run() -> None:
    """Start the MCP server on stdio."""
    caps = server.get_capabilities(
        notification_options=NotificationOptions(),
        experimental_capabilities={},
    )
    caps.tools = types.ToolsCapability(listChanged=True)
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="nethack-mcp",
                server_version="0.1.0",
                capabilities=caps,
                instructions=SERVER_INSTRUCTIONS,
            ),
        )


def main() -> None:
    """CLI entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
