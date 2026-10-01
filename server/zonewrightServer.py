from mcp.server import MCPServer

import toolingInstall
import toolingStatus

toolingRoot = toolingStatus.resolveToolingRoot()

server = MCPServer(
  "zonewright",
  instructions="Reports on and installs the pinned Blender zonewright uses to build EverQuest zones.",
)


@server.tool()
def getToolingStatus():
  """Compare installed tooling (Blender so far) with the pinned versions in toolingManifest.json."""
  return toolingStatus.getToolingStatus(toolingRoot)


@server.tool()
def installBlender():
  """Install the Blender version pinned in toolingManifest.json; no-op if already installed, removes other versions after an upgrade."""
  return toolingInstall.installBlender(toolingRoot)


if __name__ == "__main__":
  server.run()
