from mcp.server import MCPServer

import toolingStatus

toolingRoot = toolingStatus.resolveToolingRoot()

server = MCPServer(
  "zonewright",
  instructions="Reports on the pinned Blender install zonewright uses to build EverQuest zones.",
)


@server.tool()
def getToolingStatus():
  """Compare installed tooling (Blender so far) with the pinned versions in toolingManifest.json."""
  return toolingStatus.getToolingStatus(toolingRoot)


if __name__ == "__main__":
  server.run()
