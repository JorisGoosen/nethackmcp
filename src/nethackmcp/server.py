"""MCP server for playing NetHack."""

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server import StdioServerParameters
import asyncio
import subprocess
import json
import re
from pathlib import Path
from typing import Any


class NetHackClient:
    """Handles communication with NetHack."""
    
    def __init__(self):
        self.process: subprocess.Process | None = None
        self.game_state: dict = {}
        
    def start_game(self, character: str = "human") -> dict:
        """Start a new NetHack game."""
        # NetHack command - adjust path as needed
        cmd = ["nethack", "-u", character]
        
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        # Initial screen state
        self.game_state = {
            "status": "started",
            "screen": "",
            "message": "Game started. Waiting for first screen..."
        }
        
        return self.game_state
    
    def send_command(self, command: str) -> dict:
        """Send a command to NetHack."""
        if not self.process:
            return {"error": "Game not started. Call start_game first."}
        
        # Send command
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        
        # Read ALL output until we see the prompt (">") indicating turn is over
        try:
            import time
            import select
            
            output_lines = []
            while True:
                # Wait for data with timeout
                readable, _, _ = select.select([self.process.stdout], [], [], 2.0)
                if readable:
                    line = self.process.stdout.readline()
                    if line:
                        output_lines.append(line)
                        # Stop when we see the command prompt (end of turn)
                        if line.rstrip().endswith(">"):
                            break
                    else:
                        break
                else:
                    # Timeout - something went wrong
                    break
            
            output = "".join(output_lines)
            self.game_state = {
                "status": "playing",
                "command": command,
                "response": output,
                "screen": self._extract_screen(output),
                "full_output": output
            }
            return self.game_state
        except Exception as e:
            return {"error": str(e)}
    
    def _extract_screen(self, output: str) -> str:
        """Extract screen state from output."""
        # This is simplified - real implementation would parse NetHack output
        return output
    
    def get_state(self) -> dict:
        """Get current game state."""
        return self.game_state
    
    def _read_initial_screen(self) -> str:
        """Read initial screen after starting game."""
        import time
        time.sleep(0.2)
        try:
            output_lines = []
            while True:
                try:
                    line = self.process.stdout.readline()
                    if line:
                        output_lines.append(line)
                    else:
                        break
                except:
                    break
            return "".join(output_lines)
        except:
            return ""
    
    def stop_game(self) -> dict:
        """Stop the current game."""
        if self.process:
            self.process.terminate()
            self.process = None
            return {"status": "stopped"}
        return {"error": "No game running"}


app = Server("nethack")


@app.list_tools()
async def list_tools() -> list[dict[str, Any]]:
    """List available tools for playing NetHack."""
    return [
        {
            "name": "nethack_start",
            "description": "Start a new NetHack game with specified character",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "Character name (e.g., 'human', 'elf', 'dwarf')"
                    }
                },
                "required": ["character"]
            }
        },
        {
            "name": "nethack_move",
            "description": "Send a move/command to NetHack (direction or action)\n\n## NetHack Commands (short):\n- Direction: h(←) j(↓) k(↑) l(→) y(↖) u(↗) b(↙) n(↘)\n- Move: [direction], Move far: [DIR], Move silent: m+[direction]\n- Pick up: ,  Drop: d  Fight: f+[dir]\n- Look: :  Search: s  Wait/Rest: .\n- Inventory: i  Equipment: *  Status: ^X\n- Extended: #command  Help: ?",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to send (movement keys, actions, or extended commands starting with #)"
                    }
                },
                "required": ["command"]
            }
        },
        {
            "name": "nethack_state",
            "description": "Get current game state",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "nethack_stop",
            "description": "Stop current NetHack game",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        }
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a NetHack tool."""
    client = NetHackClient()
    
    if name == "nethack_start":
        character = arguments.get("character", "human")
        result = await asyncio.to_thread(client.start_game, character)
        result["screen"] = client._read_initial_screen()
        return result
    
    elif name == "nethack_move":
        command = arguments.get("command")
        if not command:
            return {"error": "Command is required"}
        result = await asyncio.to_thread(client.send_command, command)
        result["screen"] = client._extract_screen(result.get("full_output", ""))
        return result
    
    elif name == "nethack_state":
        state = await asyncio.to_thread(client.get_state)
        state["screen"] = client._extract_screen(state.get("full_output", ""))
        return state
    
    elif name == "nethack_stop":
        return await asyncio.to_thread(client.stop_game)
    
    else:
        return {"error": f"Unknown tool: {name}"}


async def main():
    """Run the MCP server via stdio."""
    params = StdioServerParameters(
        command="python3",
        args=["-m", "nethackmcp.server"],
        env=None
    )
    
    await app.run(
        params,
        initialization_options=InitializationOptions(
            server_name="nethack",
            server_version="0.1.0",
            capabilities=app.get_capabilities()
        )
    )


def run():
    """Entry point for the CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
