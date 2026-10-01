import anyio.from_thread
import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

import extensionCatalog
import toolingLog
import toolingManifest
import toolingStatus
import toolingSync

toolingRoot = toolingStatus.resolveToolingRoot()
toolingLog.configureLogging(toolingRoot)

server = MCPServer(
  "zonewright",
  instructions="Manages the pinned Blender and extensions zonewright uses to build EverQuest zones.",
)


async def runSync(context):
  def reportProgress(progress, total, message):
    anyio.from_thread.run(context.report_progress, progress, total, message)
  return await anyio.to_thread.run_sync(toolingSync.syncTooling, toolingRoot, reportProgress)


@server.tool()
def getToolingStatus():
  """Compare installed Blender and extensions with the pins in toolingManifest.json."""
  return toolingStatus.getToolingStatus(toolingRoot)


@server.tool()
async def syncTooling(context: Context):
  """Make the tooling root match toolingManifest.json: install, upgrade, and remove Blender versions and extensions."""
  return await runSync(context)


@server.tool()
async def addExtension(extensionID: str, context: Context, version: str | None = None):
  """Pin the newest extensions.blender.org release of an extension compatible with the pinned Blender, then sync."""
  manifest = toolingManifest.loadManifest()
  pin = extensionCatalog.resolveExtension(extensionID, manifest["blender"]["version"])
  if version is not None and version != pin["version"]:
    raise ToolError(f"extensions.blender.org offers only {extensionID} {pin['version']} for Blender {manifest['blender']['version']}, not {version}")
  manifest["extensions"][extensionID] = pin
  toolingManifest.saveManifest(manifest)
  return await runSync(context)


@server.tool()
async def removeExtension(extensionID: str, context: Context):
  """Unpin an extension, then sync to uninstall it."""
  manifest = toolingManifest.loadManifest()
  if extensionID not in manifest["extensions"]:
    raise ToolError(f"Extension '{extensionID}' is not pinned; pinned: {sorted(manifest['extensions'])}")
  del manifest["extensions"][extensionID]
  toolingManifest.saveManifest(manifest)
  return await runSync(context)


if __name__ == "__main__":
  server.run()
