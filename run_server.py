#!/usr/bin/env python3
"""Start the NetHack MCP server.

Usage:
    python run_server.py                          # stdio mode (default)
    python run_server.py --transport sse          # SSE on 0.0.0.0:8000
    python run_server.py --transport sse --port 9000 --host 127.0.0.1
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from nethackmcp.server import main

if __name__ == "__main__":
    main()