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
import zoneSurvey

toolingRoot = toolingStatus.resolveToolingRoot()
toolingLog.configureLogging(toolingRoot)

server = MCPServer(
  "zonewright",
  instructions="Manages the pinned Blender and extensions zonewright uses to build EverQuest zones.",
)


def progressReporter(context):
  def reportProgress(progress, total, message):
    anyio.from_thread.run(context.report_progress, progress, total, message)
  return reportProgress


async def runSync(context):
  return await anyio.to_thread.run_sync(toolingSync.syncTooling, toolingRoot, progressReporter(context))


def surveyRow(survey):
  row = {"zone": survey["zone"], "format": survey["format"], "brewallLabels": survey["brewallLabelCount"]}
  if "error" in survey:
    return row | {"error": survey["error"]}
  terrainBounds = survey["terrainBounds"]
  return row | {
    "terrainSize": terrainBounds["size"] if terrainBounds else None,
    "allGeometrySize": survey["allGeometryBounds"]["size"],
    "triangles": survey["triangleCount"],
    "textures": survey["textureCount"],
    "placements": survey["placementCount"],
  }


def footprint(row):
  size = row.get("terrainSize") or row.get("allGeometrySize")
  return size[0] * size[1] if size else -1


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


@server.tool()
async def surveyZones(context: Context, zones: list[str] | None = None):
  """Measure EverQuest zones from the client's actual zone files, largest terrain footprint first; results are cached."""
  clientRoot = zoneSurvey.resolveClientRoot()
  surveys = await anyio.to_thread.run_sync(zoneSurvey.surveyZones, clientRoot, toolingRoot, zones, progressReporter(context))
  rows = sorted((surveyRow(survey) for survey in surveys.values()), key=footprint, reverse=True)
  return {
    "units": "EQ units; terrainSize is the terrain model or grid, allGeometrySize includes backdrops and stray placements",
    "cachePath": str(toolingRoot / "survey" / "zoneSurvey.json"),
    "zones": rows,
  }


@server.tool()
def getZoneNotes(zone: str):
  """Brewall map labels for a zone: place names for design notes, not geometry or scale."""
  clientRoot = zoneSurvey.resolveClientRoot()
  if not zoneSurvey.brewallMapPaths(clientRoot, zone.lower()):
    raise ToolError(f"No Brewall map files for zone '{zone}' in {clientRoot / 'maps' / 'Brewall'}")
  return {"zone": zone, "labels": zoneSurvey.readBrewallLabels(clientRoot, zone.lower())}


if __name__ == "__main__":
  server.run()
