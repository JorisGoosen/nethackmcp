#!/usr/bin/env python3
"""Start the NetHack MCP server."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from nethackmcp.server import main

if __name__ == "__main__":
    main()
