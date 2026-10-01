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
import surveyFields
import zoneSources
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


def sortValue(row, sortPath):
  value = row
  for key in sortPath.split("."):
    if not isinstance(value, dict) or key not in value:
      return None
    value = value[key]
  return value


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
async def surveyZones(context: Context, zones: list[str] | None = None, groups: list[str] | None = None, sortBy: str = "dimensions.footprint", limit: int | None = None):
  """Technical lane: measured field groups for the named zones (all when omitted), sorted descending by a dotted field path. Cached by file hash and group version."""
  groupNames = groups if groups is not None else ["dimensions"]
  zoneSurvey.validateMeasuredGroups(groupNames)
  if sortBy.split(".")[0] not in groupNames:
    raise ToolError(f"sortBy '{sortBy}' must start with one of the requested groups {groupNames}")
  clientRoot = zoneSources.resolveClientRoot()
  surveys = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, zones, groupNames, progressReporter(context))
  rows = [{"variant": key} | survey for key, survey in surveys.items()]
  sortable = sorted((row for row in rows if isinstance(sortValue(row, sortBy), (int, float))), key=lambda row: sortValue(row, sortBy), reverse=True)
  unsortable = [row for row in rows if not isinstance(sortValue(row, sortBy), (int, float))]
  ordered = sortable + unsortable
  return {
    "units": "EQ units, Blender 1:1",
    "groups": {groupName: surveyFields.measuredGroups[groupName][0] for groupName in groupNames},
    "variantCount": len(rows),
    "rows": ordered[:limit] if limit is not None else ordered,
  }


@server.tool()
async def getZoneSurvey(context: Context, zone: str):
  """Everything surveyed for one zone, both lanes: every measured group brought up to date, plus cached interpretations."""
  clientRoot = zoneSources.resolveClientRoot()
  surveys = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, [zone.lower()], list(surveyFields.measuredGroups), progressReporter(context))
  cached = zoneSurvey.readSurvey(toolingRoot, list(surveys))
  return {
    "zone": zone.lower(),
    "brewallLabelCount": len(zoneSurvey.readBrewallLabels(clientRoot, zone.lower())),
    "variants": {key: survey | {"interpreted": cached[key]["interpreted"] if cached[key] else {}} for key, survey in surveys.items()},
  }


@server.tool()
def getZoneNotes(zone: str):
  """Brewall map labels for a zone: place names for design notes, not geometry or scale."""
  clientRoot = zoneSources.resolveClientRoot()
  if not zoneSurvey.brewallMapPaths(clientRoot, zone.lower()):
    raise ToolError(f"No Brewall map files for zone '{zone}' in {clientRoot / 'maps' / 'Brewall'}")
  return {"zone": zone, "labels": zoneSurvey.readBrewallLabels(clientRoot, zone.lower())}


if __name__ == "__main__":
  server.run()
