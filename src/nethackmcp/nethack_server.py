#!/usr/bin/env python3
"""Standalone NetHack server that maintains state and provides diff functionality."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class NetHackServer:
    """Standalone NetHack server with state tracking."""
    
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.session_name = "nethack_standalone"
        self.previous_screen = None
        self.previous_stripped = None
        self._load_state()
    
    def _load_state(self):
        """Load state from file if it exists."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.previous_screen = state.get('previous_screen')
                    self.previous_stripped = state.get('previous_stripped')
            except Exception:
                pass
    
    def _save_state(self, current_screen: str, current_stripped: list[str]):
        """Save current screen as previous for next call."""
        state = {
            'previous_screen': current_screen,
            'previous_stripped': current_stripped
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except Exception:
            pass
    
    def _strip_ansi(self, text: str) -> tuple[str, str]:
        """Strip ANSI escape codes from text."""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        stripped = ansi_escape.sub('', text)
        return stripped, text
    
    def start_game(self) -> str:
        """Start a new NetHack game."""
        # Kill existing tmux session
        subprocess.run(['tmux', 'kill-session', '-t', self.session_name], 
                      capture_output=True)
        
        # Start new tmux session
        cmd = [
            'tmux', 'new-session', '-d', '-s', self.session_name, '-x', '80', '-y', '24',
            'bash', '-c', 'export NETHACKOPTIONS="color"; while true; do nethack; sleep 1; done'
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        time.sleep(2)
        
        return self.get_screen()
    
    def send_command(self, command: str) -> str:
        """Send a command to NetHack."""
        # Clean up the command for tmux
        special_keys = {
            'Enter': 'Enter',
            'Escape': 'Escape', 
            'Space': 'Space',
            'Tab': 'Tab',
            'Backspace': 'Backspace',
        }
        
        if command in special_keys:
            tmux_key = special_keys[command]
            subprocess.run(['tmux', 'send-keys', '-t', self.session_name, tmux_key],
                          check=True, capture_output=True)
        else:
            for char in command:
                subprocess.run(['tmux', 'send-keys', '-t', self.session_name, char],
                              check=True, capture_output=True)
            
            # Extended commands (starting with #) need Enter to execute
            if command.startswith("#"):
                subprocess.run(['tmux', 'send-keys', '-t', self.session_name, 'Enter'],
                              check=True, capture_output=True)
        
        time.sleep(0.5)
        return self.get_screen()
    
    def get_screen(self) -> str:
        """Get current screen output."""
        try:
            result = subprocess.run(
                ['tmux', 'capture-pane', '-t', self.session_name, '-p', '-J'],
                check=True, capture_output=True, text=True
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return "Error: tmux session not found"
    
    def get_screen_stripped(self) -> list[str]:
        """Get current screen as list of lines with ANSI removed."""
        raw = self.get_screen()
        stripped, _ = self._strip_ansi(raw)
        return stripped.split('\n')
    
    def get_diff_new(self) -> str:
        """Get diff showing new/changed characters."""
        current_screen = self.get_screen()
        current_stripped, _ = self._strip_ansi(current_screen)
        current_lines = current_stripped.split('\n')
        
        if self.previous_stripped is None:
            return "No previous screen available"
        
        return self._create_diff(current_lines, self.previous_stripped, show_new=True)
    
    def get_diff_disappeared(self) -> str:
        """Get diff showing characters that disappeared."""
        current_screen = self.get_screen()
        current_stripped, _ = self._strip_ansi(current_screen)
        current_lines = current_stripped.split('\n')
        
        if self.previous_stripped is None:
            return "No previous screen available"
        
        return self._create_diff(current_lines, self.previous_stripped, show_new=False)
    
    def _create_diff(self, current: list[str], previous: list[str], show_new: bool = True) -> str:
        """Create character-by-character diff."""
        max_lines = max(len(current), len(previous))
        max_width = max(
            max(len(line) for line in current) if current else 0,
            max(len(line) for line in previous) if previous else 0
        )
        
        diff_lines = []
        for row in range(max_lines):
            current_line = current[row] if row < len(current) else ""
            previous_line = previous[row] if row < len(previous) else ""
            
            diff_row = []
            for col in range(max_width):
                current_char = current_line[col] if col < len(current_line) else " "
                previous_char = previous_line[col] if col < len(previous_line) else " "
                
                if current_char != previous_char:
                    diff_row.append(current_char)
                else:
                    diff_row.append(" ")
            
            diff_lines.append("".join(diff_row).rstrip())
        
        return "\n".join(diff_lines)
    
    def run_interactive(self):
        """Run interactive mode for testing."""
        print("Starting NetHack server...")
        print("Commands: start, move <cmd>, state, diff_new, diff_disappeared, quit")
        
        while True:
            try:
                cmd = input("> ").strip()
                if not cmd:
                    continue
                
                if cmd == 'quit':
                    break
                elif cmd == 'start':
                    output = self.start_game()
                    print(output)
                elif cmd.startswith('move '):
                    command = cmd[5:]
                    output = self.send_command(command)
                    print(output)
                elif cmd == 'state':
                    output = self.get_screen()
                    print(output)
                elif cmd == 'diff_new':
                    output = self.get_diff_new()
                    print(output)
                elif cmd == 'diff_disappeared':
                    output = self.get_diff_disappeared()
                    print(output)
                else:
                    print(f"Unknown command: {cmd}")
            except KeyboardInterrupt:
                break
            except EOFError:
                break


def main():
    """Main entry point."""
    state_file = "/tmp/nethack_server_state.json"
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--interactive':
            server = NetHackServer(state_file)
            server.run_interactive()
        elif sys.argv[1] == 'start':
            server = NetHackServer(state_file)
            output = server.start_game()
            # Save state after start
            current_stripped = server.get_screen_stripped()
            current_screen = server.get_screen()
            server._save_state(current_screen, current_stripped)
            print(output)
        elif sys.argv[1] == 'move':
            if len(sys.argv) < 3:
                print("Error: move command requires argument")
                sys.exit(1)
            command = sys.argv[2]
            server = NetHackServer(state_file)
            output = server.send_command(command)
            # Save state after move
            current_stripped = server.get_screen_stripped()
            current_screen = server.get_screen()
            server._save_state(current_screen, current_stripped)
            print(output)
        elif sys.argv[1] == 'diff_new':
            server = NetHackServer(state_file)
            print(server.get_diff_new())
        elif sys.argv[1] == 'diff_disappeared':
            server = NetHackServer(state_file)
            print(server.get_diff_disappeared())
        else:
            print(f"Unknown command: {sys.argv[1]}")
            sys.exit(1)
    else:
        # Default to interactive mode
        server = NetHackServer(state_file)
        server.run_interactive()


if __name__ == '__main__':
    main()
