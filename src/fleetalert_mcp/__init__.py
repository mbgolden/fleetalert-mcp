"""FleetAlert MCP server.

Exposes the live FleetAlert AI demo (https://fleetalert.10finger.dev) to MCP
clients such as Claude Code and Claude Desktop, carrying FleetAlert's own
safety tiers through to the tools and adding the auth and rate limits the
public demo API does not have itself.
"""

__version__ = "0.3.0"
