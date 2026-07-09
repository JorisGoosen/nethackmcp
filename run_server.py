#!/usr/bin/env python3
"""Start the NetHack MCP server."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from nethackmcp.server import start_server


async def main():
    await start_server()


if __name__ == "__main__":
    asyncio.run(main())
