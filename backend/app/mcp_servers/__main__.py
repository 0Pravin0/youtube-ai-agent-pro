"""python -m app.mcp_servers <name>   (the packaged app runs: yta-engine --mcp-server <name>)"""
import sys

from app.mcp_servers.run import main

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
