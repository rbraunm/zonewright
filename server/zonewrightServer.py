import atexit
import datetime
import functools
import inspect
import io
import json
import sys
from pathlib import Path

import anyio.from_thread
import anyio.to_thread
import numpy
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict

import assetCatalog
import assetSheets
import assetSurvey
import blenderBridge
import eqCalibration
import eqEmitters
import eqgExport
import eqModels
import eqRaces
import eqRecording
import eqZones
import extensionCatalog
import machineProfile
import toolingLog
import toolingManifest
import toolingStatus
import toolingSync
import surveyFields
import zoneSources
import zoneSurvey

toolingRoot = toolingStatus.resolveToolingRoot()
bridge = blenderBridge.BlenderBridge(toolingRoot)
serverDirectory = Path(__file__).resolve().parent

server = MCPServer(
  "zonewright",
  instructions="Hands and eyes in a headless Blender for building EverQuest zones, plus the pinned tooling and a survey of the client's zones.",
)


def loadedServerSources():
  """Every source file this server process has imported from its own folder."""
  return sorted(Path(module.__file__).resolve() for module in list(sys.modules.values()) if getattr(module, "__file__", None) and Path(module.__file__).resolve().parent == serverDirectory)


def sourceSignatures(paths):
  return {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}


def requireCurrentServerCode():
  current = sourceSignatures(serverSourcePaths)
  changed = sorted(name for name, signature in current.items() if serverSourceFingerprint[name] != signature)
  if changed:
    raise ToolError(f"zonewright server code changed on disk since this server started ({', '.join(changed)}); reconnect with /mcp to load it")


def refuseUnknownArguments(tool):
  """The MCP SDK drops arguments a tool does not take, so a misspelled parameter would silently leave its default in place."""
  class StrictArguments(tool.fn_metadata.arg_model):
    model_config = ConfigDict(extra="forbid")
  tool.fn_metadata.arg_model = StrictArguments
  tool.parameters["additionalProperties"] = False


def guardedTool(**options):
  """server.tool that refuses unknown arguments and first refuses to run outdated server code."""
  def decorate(function):
    if inspect.iscoroutinefunction(function):
      @functools.wraps(function)
      async def guarded(*arguments, **keywordArguments):
        requireCurrentServerCode()
        return await function(*arguments, **keywordArguments)
    else:
      @functools.wraps(function)
      def guarded(*arguments, **keywordArguments):
        requireCurrentServerCode()
        return function(*arguments, **keywordArguments)
    server.tool(**options)(guarded)
    refuseUnknownArguments(server._tool_manager.get_tool(options.get("name", function.__name__)))
    return guarded
  return decorate


def progressReporter(context):
  def reportProgress(progress, total, message):
    anyio.from_thread.run(context.report_progress, progress, total, message)
  return reportProgress


async def runSync(context):
  await anyio.to_thread.run_sync(bridge.stopForSync)
  return await anyio.to_thread.run_sync(toolingSync.syncTooling, toolingRoot, progressReporter(context))


async def callBridge(context, command, arguments):
  return await anyio.to_thread.run_sync(bridge.call, command, arguments, progressReporter(context))


# The live dumps record dark elf females at height 5, the race default.
figureModelCode = "DAF"
figureHeight = 5.0
# The server and the live dumps give positions as (x, y, z); the zone files, and so Blender, hold them as (y, x, z): measured, every
# kind of placement lands on the zone geometry only that way. Headings run 512 to a turn; eqgame.exe's heading toward a point
# (0x4ef250) is 0 toward +y and 128 toward +x, which through the axis swap is a turn of +heading about Z for a model whose front is +X.
eqHeadingUnits = 512
standPose = {"animation": None, "variant": None, "frame": 0}


def eqModel(zone, model, source=None, appearance=None, animation=None):
  """Build or reuse the cache of a model the client loads in the zone (or, with no zone, at startup)."""
  try:
    return eqModels.buildModel(zoneSources.resolveClientRoot(), toolingRoot / "models", model, zone, source, appearance, animation)
  except ValueError as error:
    raise ToolError(str(error)) from error


def spawnModel(zone, model, height, newEngineZone, source=None, appearance=None, animation=None):
  """A character model posed by an animation frame, with the scale and avatarHeight the client gives a spawn of this height in the zone."""
  folder, details = eqModel(zone, model, source, appearance, animation or standPose)
  code = details["model"].upper()
  try:
    scale = eqRaces.spawnScale(code, height, newEngineZone)
    avatarHeight = eqRaces.avatarHeight(zoneSources.resolveClientRoot(), code, scale)
  except ValueError as error:
    raise ToolError(str(error)) from error
  return {"folder": str(folder), "height": height, "scale": scale, "avatarHeight": avatarHeight, "details": details}


async def zoneIsNewEngine(context):
  """The open zone's newEngineZone, which sets the scale the client draws its spawns at."""
  zone = await callBridge(context, "getZoneProperties", {})
  if "newEngineZone" not in zone:
    raise ToolError("Spawns need the zone's newEngineZone (its zone header's NewEngineZone; EQEmu sends false for every zone): set it with setZoneProperties")
  return bool(zone["newEngineZone"])


def placementFrame(location, headingDegrees, x, y, z, heading):
  """Blender location and turn about Z (counter-clockwise, degrees) from either Blender values or EQ's."""
  blenderGiven = location is not None or headingDegrees is not None
  eqGiven = any(value is not None for value in (x, y, z, heading))
  if blenderGiven == eqGiven:
    raise ToolError("Give location and headingDegrees (Blender; heading 0 = +Y, clockwise) or x, y, z, and heading (EQ, as the server and the live dumps give them)")
  if blenderGiven:
    if location is None or headingDegrees is None or len(location) != 3:
      raise ToolError("location [x, y, z] and headingDegrees go together")
    return list(location), 90 - headingDegrees
  if None in (x, y, z, heading):
    raise ToolError("x, y, z, and heading go together")
  return [y, x, z], heading * 360 / eqHeadingUnits


def modelSummary(details):
  """What a placement drew and from where, including anything the client data lacks."""
  definition = details["definition"]
  return {
    "model": details["model"], "kind": definition["kind"], "archive": definition["archive"], "linkedBy": definition["via"], "tier": definition["tier"],
    "pose": details["pose"], "pieces": details["pieces"], "unattached": details["unattached"], "swappedMaterials": details["swappedMaterials"],
    "missingTextures": details["missingTextures"], "droppedTriangles": details["droppedTriangles"], "particleCloudsNotDrawn": details["particleCloudsNotDrawn"],
  }


async def placeEQModel(context, folder, name, location, rotationDegrees, scale, avatarHeight, snapToGround, collection, details):
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": name, "location": location, "rotationDegrees": rotationDegrees, "scale": scale,
    "avatarHeight": avatarHeight, "snapToGround": snapToGround, "collection": collection,
  })
  return placed | {"source": modelSummary(details)}


def newRenderPath():
  rendersPath = toolingRoot / "renders"
  rendersPath.mkdir(parents=True, exist_ok=True)
  return rendersPath / (datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S-%f") + ".png")


def sortValue(row, sortPath):
  value = row
  for key in sortPath.split("."):
    if not isinstance(value, dict) or key not in value:
      return None
    value = value[key]
  return value


@guardedTool()
def getToolingStatus():
  """Compare installed Blender and extensions with the pins in toolingManifest.json, and report the bridge, runPython use, and the
  renderer's latest calibration against each screenshot (calibrateShot)."""
  return toolingStatus.getToolingStatus(toolingRoot) | {
    "machineProfile": machineProfile.profileStatus(toolingRoot),
    "bridge": bridge.status(),
    "runPython": toolingLog.countRunPython(toolingRoot),
    "calibration": eqCalibration.latestResults(toolingRoot / "calibration"),
  }


@guardedTool()
async def syncTooling(context: Context):
  """Make the tooling root match toolingManifest.json: install, upgrade, and remove Blender versions and extensions."""
  return await runSync(context)


@guardedTool()
async def profileMachine(context: Context):
  """Re-measure this machine for performance: CPU workers and the fastest hardware GPU backend for Blender. syncTooling runs this whenever the profile is missing or stale."""
  await anyio.to_thread.run_sync(bridge.stopForSync)
  return await anyio.to_thread.run_sync(machineProfile.profileMachine, toolingRoot, progressReporter(context))


@guardedTool()
async def addExtension(extensionID: str, context: Context, version: str | None = None):
  """Pin the newest extensions.blender.org release of an extension compatible with the pinned Blender, then sync."""
  await anyio.to_thread.run_sync(bridge.stopForSync)
  manifest = toolingManifest.loadManifest()
  pin = extensionCatalog.resolveExtension(extensionID, manifest["blender"]["version"])
  if version is not None and version != pin["version"]:
    raise ToolError(f"extensions.blender.org offers only {extensionID} {pin['version']} for Blender {manifest['blender']['version']}, not {version}")
  manifest["extensions"][extensionID] = pin
  toolingManifest.saveManifest(manifest)
  return await runSync(context)


@guardedTool()
async def removeExtension(extensionID: str, context: Context):
  """Unpin an extension, then sync to uninstall it."""
  await anyio.to_thread.run_sync(bridge.stopForSync)
  manifest = toolingManifest.loadManifest()
  if extensionID not in manifest["extensions"]:
    raise ToolError(f"Extension '{extensionID}' is not pinned; pinned: {sorted(manifest['extensions'])}")
  del manifest["extensions"][extensionID]
  toolingManifest.saveManifest(manifest)
  return await runSync(context)


@guardedTool()
async def surveyZones(context: Context, zones: list[str] | None = None, groups: list[str] | None = None, sortBy: str = "dimensions.footprint", limit: int | None = None, verifyHashes: bool = False):
  """Technical lane: measured field groups for the named zones (all when omitted), sorted descending by a dotted field path. Cached by file hash and group version; file hashes are reused while a file's size and modification time are unchanged unless verifyHashes."""
  groupNames = groups if groups is not None else ["dimensions"]
  zoneSurvey.validateMeasuredGroups(groupNames)
  if sortBy.split(".")[0] not in groupNames:
    raise ToolError(f"sortBy '{sortBy}' must start with one of the requested groups {groupNames}")
  clientRoot = zoneSources.resolveClientRoot()
  surveys = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, zones, groupNames, progressReporter(context), verifyHashes)
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


@guardedTool()
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


@guardedTool()
def getZoneNotes(zone: str):
  """Brewall map labels for a zone: place names for design notes, not geometry or scale."""
  clientRoot = zoneSources.resolveClientRoot()
  if not zoneSurvey.brewallMapPaths(clientRoot, zone.lower()):
    raise ToolError(f"No Brewall map files for zone '{zone}' in {clientRoot / 'maps' / 'Brewall'}")
  return {"zone": zone, "labels": zoneSurvey.readBrewallLabels(clientRoot, zone.lower())}


catalog = assetCatalog.AssetCatalog(toolingRoot)
assetKinds = "texture, model, light, emitter, or ecosystem"
findHelp = (
  " Filters, each optional: kind (" + assetKinds + "); text, words that must all appear in the id, name, description, usage, category,"
  " tags, or measured names and color; categories, any of; tags, {group: [terms]} with every term required; source, a zone name or a"
  " source key (zone:<name>, folder:<folder>, file:<path>); described, true for described assets only, false for undescribed only."
  " For textures, by what was measured: colors, any of the measured color names (" + ", ".join(assetSurvey.colorNames) + ");"
  " minimumSide in pixels; tiles, true for textures that repeat without a visible seam; usedOn, slope bands (flat, slope, steep,"
  " vertical, overhang) that together hold at least half of the texture's area where it is used most."
  " sortBy relevance (text matches in descriptions, then area), areaShare (most-used first), or name."
)


def catalogCall(function, *arguments):
  try:
    return function(*arguments)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error


@guardedTool()
async def surveyAssets(context: Context, zone: str | None = None, folder: str | None = None, path: str | None = None, refresh: bool = False):
  """Survey one source's graphical assets into the asset catalog's measured lane: a client zone (the textures of the archives it loads
  for its own geometry and objects, with how the zone uses each: area share, world units per texture repeat, slopes, shaders, paired
  textures; its placed models; its light styles; its emitters; an EQ terrain zone's ecosystems), a client folder of loose images
  (Resources/Sky, Resources/WaterSwap, Resources/Precipitation, EnvEmitterEffects), or an EQG zone archive at an absolute `path`, such as
  one exportZone wrote. Each texture is written out readable, with a thumbnail. Cached until the source's files change. Returns counts and
  the most-used textures; findAssets, viewTextures, viewModels, and getAsset read the rest, and describeAssets writes what they are."""
  if sum(value is not None for value in (zone, folder, path)) != 1:
    raise ToolError("Survey one source: a zone, a folder, or a path")
  clientRoot = zoneSources.resolveClientRoot()
  if zone is not None:
    zoneName = zone.lower()
    key, stamp = f"zone:{zoneName}", catalogCall(assetSurvey.clientZoneStamp, clientRoot, zoneName)
    surveyFunction = lambda: assetSurvey.surveyClientZone(clientRoot, toolingRoot / "models", catalog.root, zoneName)
  elif folder is not None:
    key, stamp = f"folder:{folder}", catalogCall(assetSurvey.looseFolderStamp, clientRoot, folder)
    surveyFunction = lambda: assetSurvey.surveyLooseFolder(clientRoot, catalog.root, folder)
  else:
    archivePath = Path(path)
    if not archivePath.is_absolute() or archivePath.suffix.lower() != ".eqg" or not archivePath.is_file():
      raise ToolError(f"'{path}' is not an absolute path to an existing .eqg file")
    key, stamp = f"file:{archivePath}", assetSurvey.zoneFileStamp(archivePath)
    surveyFunction = lambda: assetSurvey.surveyZoneFile(clientRoot, catalog.root, archivePath)
  reportProgress = progressReporter(context)
  await anyio.to_thread.run_sync(reportProgress, 0, 1, f"surveying {key}")
  surveyed = await anyio.to_thread.run_sync(catalogCall, catalog.survey, surveyFunction, key, stamp, refresh)
  await anyio.to_thread.run_sync(reportProgress, 1, 1, f"surveyed {key}")
  interpretations = catalog.interpretations()
  kinds = {}
  for assetID, facts in surveyed["assets"].items():
    counts = kinds.setdefault(facts["kind"], {"assets": 0, "described": 0})
    counts["assets"] += 1
    counts["described"] += assetID in interpretations
  used = sorted((asset for asset in catalog.assets().values() if key in asset["sources"] and (asset["sources"][key].get("uses") or {}).get("areaShare")), key=lambda asset: -asset["sources"][key]["uses"]["areaShare"])
  return {
    "source": key, "format": surveyed.get("format"), "archives": surveyed.get("archives"), "kinds": kinds, "problems": surveyed["problems"],
    "unreadable": [assetID for assetID, facts in surveyed["assets"].items() if "problem" in facts],
    "mostUsedTextures": [{"id": asset["id"], "described": asset["id"] in interpretations} | {field: asset["sources"][key]["uses"][field] for field in ("areaShare", "unitsPerRepeat", "slopeShares")} for asset in used[:15]],
  }


@guardedTool(description="Search the asset catalog: compact entries (measured facts and descriptions) for the assets that match." + findHelp)
def findAssets(
  kind: str | None = None, text: str | None = None, categories: list[str] | None = None, tags: dict | None = None, source: str | None = None,
  described: bool | None = None, colors: list[str] | None = None, minimumSide: int | None = None, tiles: bool | None = None,
  usedOn: list[str] | None = None, sortBy: str = "relevance", limit: int = 40,
):
  return catalogCall(catalog.find, kind, text, categories, tags, source, described, sortBy, limit, colors, minimumSide, tiles, usedOn)


@guardedTool()
def getAsset(id: str):
  """Everything the catalog holds for one asset: its measured facts, what differs in each source that holds it, and its description."""
  return catalogCall(catalog.entry, id)


@guardedTool()
def getAssetVocabulary():
  """The words the catalog describes assets with: asset kinds, each kind's categories, and the tag groups with their terms, each with
  its meaning. Descriptions must use these; extendAssetVocabulary adds a term when none fits."""
  return catalog.vocabulary()


@guardedTool()
def extendAssetVocabulary(group: str, term: str, meaning: str):
  """Add a term to the vocabulary when no existing one fits: group is a tag group's name or category:<kind>; term is one camelCase word."""
  return catalogCall(catalog.extendVocabulary, group, term, meaning)


@guardedTool()
def describeAssets(descriptions: list[dict]):
  """Write what assets are, all or none: [{id, category, tags {group: [terms]}, description (what it shows and how it reads), usage
  (where and how to use it: surfaces, scale, pairings, what to avoid), worldUnitsPerRepeat (textures: the repeat that reads right; the
  measured unitsPerRepeat is how the source zone used it), pairsWith (ids of assets it goes with: its normal map, transitions)}], in
  getAssetVocabulary's words. Each replaces its asset's earlier description."""
  return {"described": catalogCall(catalog.describe, descriptions)}


def sheetEntries(ids, kind, text, categories, tags, source, described, sortBy, limit, measuredFilters=(None, None, None, None)):
  if ids is not None and any(value is not None for value in (text, categories, tags, source, described, *measuredFilters)):
    raise ToolError("Give ids or filters, not both")
  if ids is not None:
    return [catalogCall(catalog.requireAsset, assetID) for assetID in ids]
  found = catalogCall(catalog.find, kind, text, categories, tags, source, described, sortBy, limit, *measuredFilters)["assets"]
  return [catalog.assets()[entry["id"]] for entry in found]


def writeSheetImage(cells, columns, legend, cellSide):
  outputPath = newRenderPath().with_suffix(".jpg")
  size = catalogCall(assetSheets.writeSheet, cells, columns, outputPath, cellSide)
  return [Image(data=outputPath.read_bytes(), format="jpeg"), {"outputPath": str(outputPath)} | size | {"cells": legend}]


@guardedTool(description=(
  "Look at textures from the catalog on one numbered contact sheet: by ids, or by the findAssets filters (kind is texture). cellSide"
  " 128 fits 48 thumbnails; 256 fits 16 drawn from the full texture, for a close look. tiled repeats each two by two at half size so"
  " seams show; showAlpha draws each over a checkerboard by its alpha. Labels give the number, the name, the size, and * when described."
  + findHelp))
def viewTextures(
  ids: list[str] | None = None, text: str | None = None, categories: list[str] | None = None, tags: dict | None = None, source: str | None = None,
  described: bool | None = None, colors: list[str] | None = None, minimumSide: int | None = None, tiles: bool | None = None,
  usedOn: list[str] | None = None, sortBy: str = "relevance", limit: int = 40, tiled: bool = False, showAlpha: bool = False,
  columns: int = 8, cellSide: int = 128,
):
  assets = sheetEntries(ids, "texture", text, categories, tags, source, described, sortBy, limit, (colors, minimumSide, tiles, usedOn))
  if not assets:
    raise ToolError("No textures match")
  wrongKind = [asset["id"] for asset in assets if asset["kind"] != "texture" or "thumbnail" not in asset["measured"]]
  if wrongKind:
    raise ToolError(f"Not readable textures: {wrongKind}")
  interpretations = catalog.interpretations()
  cells = [{
    "image": asset["measured"]["thumbnail"] if cellSide <= assetSurvey.thumbnailSide else asset["measured"]["file"], "tiled": tiled, "showAlpha": showAlpha,
    "lines": [f"{number} {asset['name']}", f"{asset['measured']['width']}x{asset['measured']['height']}{' *' if asset['id'] in interpretations else ''}"],
  } for number, asset in enumerate(assets, start=1)]
  return writeSheetImage(cells, columns, [{"number": number, "id": asset["id"]} for number, asset in enumerate(assets, start=1)], cellSide)


@guardedTool(description=(
  "Look at models from the catalog on one numbered contact sheet (up to 48), each drawn as the client draws it in neutral daylight from"
  " three-quarters above: by ids, or by the findAssets filters (kind is model). Models of client zones only." + findHelp))
async def viewModels(
  context: Context, ids: list[str] | None = None, text: str | None = None, categories: list[str] | None = None, tags: dict | None = None,
  source: str | None = None, described: bool | None = None, sortBy: str = "relevance", limit: int = 24, columns: int = 6,
):
  assets = sheetEntries(ids, "model", text, categories, tags, source, described, sortBy, limit)
  if not assets:
    raise ToolError("No models match")
  folders = []
  for asset in assets:
    zones = [key.split(":", 1)[1] for key in asset["sources"] if key.startswith("zone:")]
    if asset["kind"] != "model" or not zones:
      raise ToolError(f"{asset['id']} is not a model of a client zone")
    zoneName = zones[0]
    # The client names an EQG model by its entry without the .mod extension.
    folder, _ = await anyio.to_thread.run_sync(eqModel, zoneName, asset["name"].removesuffix(".mod"), asset["sources"][f"zone:{zoneName}"].get("archive"))
    folders.append({"folder": str(folder)})
  outputFolder = toolingRoot / "renders" / "modelThumbnails"
  outputFolder.mkdir(parents=True, exist_ok=True)
  rendered = await callBridge(context, "renderModelThumbnails", {"models": folders, "outputFolder": str(outputFolder)})
  interpretations = catalog.interpretations()
  cells = [{
    "image": thumbnail["file"], "tiled": False, "showAlpha": False,
    "lines": [f"{number} {asset['name']}", f"{'x'.join(str(round(value)) for value in asset['measured'].get('size', []))}{' *' if asset['id'] in interpretations else ''}"],
  } for number, (asset, thumbnail) in enumerate(zip(assets, rendered["thumbnails"]), start=1)]
  return writeSheetImage(cells, columns, [{"number": number, "id": asset["id"]} for number, asset in enumerate(assets, start=1)], 256 if len(cells) <= assetSheets.cellSides[256] else 128)


@guardedTool()
async def runPython(context: Context, code: str):
  """Fallback only: run Python in the headless Blender's persistent namespace (bpy, bmesh, mathutils, math); set `result` to return a JSON value. Prefer a dedicated tool; every call is logged so repeated scripting becomes a tool."""
  toolingLog.recordRunPython(code)
  return await callBridge(context, "runPython", {"code": code})


@guardedTool()
async def newFile(context: Context, discardUnsavedChanges: bool = False):
  """Start an empty scene. Refuses when the open file has unsaved changes unless they are explicitly discarded."""
  return await callBridge(context, "newFile", {"discardUnsavedChanges": discardUnsavedChanges})


@guardedTool()
async def openFile(context: Context, path: str, discardUnsavedChanges: bool = False):
  """Open a .blend by absolute path. Refuses when the open file has unsaved changes unless they are explicitly discarded."""
  return await callBridge(context, "openFile", {"path": path, "discardUnsavedChanges": discardUnsavedChanges})


@guardedTool()
async def saveFile(context: Context, path: str | None = None):
  """Save the open file, or save it as an absolute path. Textures and libraries become relative paths; packed or generated images are refused."""
  return await callBridge(context, "saveFile", {"path": path})


@guardedTool()
async def getSceneSummary(context: Context, objectLimit: int = 200):
  """The open scene: file status, zone properties, objects (up to objectLimit), collections, cameras, materials, and images."""
  return await callBridge(context, "getSceneSummary", {"objectLimit": objectLimit})


@guardedTool()
async def setZoneProperties(
  context: Context,
  ambientColor: list[float] | None = None,
  specialAmbientColor: list[float] | None = None,
  bounceColor: list[float] | None = None,
  sunColor: list[float] | None = None,
  sunAzimuthDegrees: float | None = None,
  sunElevationDegrees: float | None = None,
  fogColor: list[float] | None = None,
  fogStart: float | None = None,
  fogEnd: float | None = None,
  fogDensity: float | None = None,
  newEngineZone: bool | None = None,
):
  """Set the zone's EQ properties stored in the .blend, in the client's lighting terms (docs/clientRendering.md): ambient, special
  ambient, bounce, and sun colors (0-1, raw as the client uses them); the direction toward the sun (azimuth 0 = +Y, clockwise;
  elevation -90 to 90); fog color, start, end (also the far clip), and density (the client's default is 0.33); and newEngineZone,
  the zone header's NewEngineZone, which sets the scale the client draws spawns at (the live dumps' zoneHeaders give it per zone;
  EQEmu sends false for every zone)."""
  updates = {
    "ambientColor": ambientColor, "specialAmbientColor": specialAmbientColor, "bounceColor": bounceColor, "sunColor": sunColor,
    "sunAzimuthDegrees": sunAzimuthDegrees, "sunElevationDegrees": sunElevationDegrees, "fogColor": fogColor, "fogStart": fogStart,
    "fogEnd": fogEnd, "fogDensity": fogDensity, "newEngineZone": newEngineZone,
  }
  given = {key: value for key, value in updates.items() if value is not None}
  if not given:
    raise ToolError(f"setZoneProperties needs at least one of {list(updates)}")
  return await callBridge(context, "setZoneProperties", {"updates": given})


@guardedTool()
async def renderView(context: Context, view: dict, shading: str = "client", bandHeight: float = 50.0):
  """Render the EQ preview of a view: {"camera": name}, {"eye": [x,y,z], "target": [x,y,z]}, or {"standAt": [x,y] or [x,y,z], "headingDegrees": h, "pitchDegrees": p} (on the highest ground at [x,y], or with z on the ground within 50 units below it, for caves and under overhangs; heading 0 = +Y, clockwise; eye 5.5 above the ground; adds a dark elf female of height 5, the race default, drawn as the client draws her in the zone (newEngineZone), walked ahead along the ground and facing the camera), or {"map": {"center": [x,y], "width": w}}: the layout from straight above, orthographic, north (+Y) up, `width` units across, without fog. shading "client" draws the zone as the client does; "layout" draws every surface unlit in a color for its height (green low through tan and brown to white high, across the scene's height range given in the result) in bands `bandHeight` units tall whose edges read as contours, darker facing away from a light in the northwest, without fog and out to the whole scene: for judging shape and layout."""
  outputPath = newRenderPath()
  figureModel = None
  zone = await callBridge(context, "getZoneProperties", {})
  # Without newEngineZone the preview's own check fails, naming it with any other missing zone property.
  if "standAt" in view and "newEngineZone" in zone:
    figure = await anyio.to_thread.run_sync(spawnModel, None, figureModelCode, figureHeight, bool(zone["newEngineZone"]))
    figureModel = {key: figure[key] for key in ("folder", "scale", "avatarHeight")}
  description = await callBridge(context, "renderView", {"view": view, "outputPath": str(outputPath), "figureModel": figureModel, "shading": shading, "bandHeight": bandHeight})
  return [Image(data=outputPath.read_bytes(), format="png"), description]


placementHelp = (
  " Position it with location [x, y, z] and headingDegrees (Blender: 0 = +Y, clockwise), or with x, y, z, and heading (0-512) exactly as"
  " the server and the live dumps give them: EQ (x, y, z) is Blender (y, x, z), and heading 0 faces EQ +y and 128 faces EQ +x, as"
  " eqgame.exe turns a spawn toward a point. zone names the zone whose archives the client"
  " loads (none searches only the global lists); the model is found through the client's own links (see findModel), never by name in an"
  " unrelated archive. source (\"archive\" or \"archive:entry\") takes a definition other than the first the client loads."
  " The result's source lists the archive and link used and anything the client data lacks (missingTextures draw magenta)."
)


@guardedTool()
async def findModel(model: str, zone: str | None = None):
  """Where the client finds an EverQuest model (an actor code like DAF, a door like POKDOOR500, an object like IT10800_ACTORDEF): every definition in the client's load order (its startup archives, then the zone's, then Resources/OnDemandResources.txt), unlinked archives that also define it, and which definition placement uses: the first loaded."""
  clientRoot = zoneSources.resolveClientRoot()

  def find():
    found = eqModels.findModel(clientRoot, toolingRoot / "models", model, zone)
    try:
      resolved = eqModels.resolveModel(clientRoot, toolingRoot / "models", model, zone)
    except ValueError as error:
      resolved = {"error": str(error)}
    return found | {"resolves": resolved}

  try:
    return await anyio.to_thread.run_sync(find)
  except ValueError as error:
    raise ToolError(str(error)) from error


@guardedTool(description=(
  "Place an EverQuest character (its actorDef code, such as DAF, PMA, or SWB) at its height (the spawn's size: the dumps' height, EQEmu's"
  " size), drawn at the scale the client gives that height in the open zone (eqgame.exe: height / 5 for WLD models and height / 6 for EQG"
  " models, with WLD models 1.3 times smaller in a newEngineZone zone and EQG models 1.3 times larger outside one; set newEngineZone with"
  " setZoneProperties). The model origin stands avatarHeight (moddat.ini's ROffset for the model, else 3.125, times the scale) above the"
  " ground below its position, as the client stands a spawn; with snapToGround false it sits at the position. Front faces its heading."
  " The result gives height, scale, and avatarHeight, which compare with the dumps' height and avatarHeight. Appearance as the client applies it: variation"
  " swaps the body piece, headType the head, textureSet the texture set (the dumps' textureType; -1 there means no override, so 0)."
  " Head looks take the dumps' terms. On a Luclin model faceStyle swaps the face materials, and hairStyle and hairColor, and facialHair"
  " and facialHairColor (255 for none), attach hair and beard items (IT<n> from the Luclin equipment archives) tinted by the client's"
  " color table; eyeColor1 colors both eyes with the client's CHR_EYE materials. A Drakkin takes its looks from the client's"
  " PlayerCustomization.txt row for its heritage: faceStyle and eyeColor1 (both eyes) lay face and eye textures, and hairStyle,"
  " facialHair, tattoo, and details attach hair, beard, tattoo, and facial"
  " attachment pieces, the first two tinted from the heritage's colors by hairColor and facialHairColor, the others by its base color;"
  " a value past the heritage's count is 0, as in the client. The result's source.unattached lists any piece the client would not"
  " attach either. The character is posed at"
  " animationFrame of animation: a code such as L01 or S03, the client's label such as WALK or WAVE, or an EQG name such as STND or"
  " NRUN; default P01, STAND STILL, which EQG models play as STND. A WLD model plays the client's animation for it: its own, else the"
  " one it borrows (a dark elf the elf's); animationVariant picks a lettered variant (A, B, ...) of a Luclin model's animation. An EQG"
  " model plays <name>_BA_1_<code>, a code playing the first EQG animation the client maps to it (S03 plays WAVE). The result's"
  " source.pose names the animation, where it came from, and its frame count and timing." + placementHelp
))
async def placeSpawn(
  context: Context, zone: str | None, model: str, name: str, height: float,
  location: list[float] | None = None, headingDegrees: float | None = None,
  x: float | None = None, y: float | None = None, z: float | None = None, heading: float | None = None,
  variation: int = 0, headType: int = 0, textureSet: int = 0, faceStyle: int = 0, hairStyle: int = 0, hairColor: int = 0,
  facialHair: int = 255, facialHairColor: int = 0, eyeColor1: int = 0, heritage: int = 0, tattoo: int = 0, details: int = 0,
  animation: str | None = None, animationVariant: str | None = None, animationFrame: int = 0,
  source: str | None = None, snapToGround: bool = True, collection: str | None = None,
):
  frameLocation, rotation = placementFrame(location, headingDegrees, x, y, z, heading)
  appearance = {
    "variation": variation, "headType": headType, "textureSet": textureSet, "faceStyle": faceStyle, "hairStyle": hairStyle, "hairColor": hairColor,
    "facialHair": facialHair, "facialHairColor": facialHairColor, "eyeColor1": eyeColor1, "heritage": heritage, "tattoo": tattoo, "details": details,
  }
  pose = {"animation": animation, "variant": animationVariant.upper() if animationVariant else None, "frame": animationFrame}
  spawn = await anyio.to_thread.run_sync(spawnModel, zone, model, height, await zoneIsNewEngine(context), source, appearance, pose)
  placed = await placeEQModel(context, spawn["folder"], name, frameLocation, rotation, spawn["scale"], spawn["avatarHeight"], snapToGround, collection, spawn["details"])
  return placed | {key: spawn[key] for key in ("height", "scale", "avatarHeight")}


@guardedTool(description=(
  "Place an EverQuest door (the dumps' doors: any server-placed model, from doors and lifts to teleport pads, books, and furniture) closed,"
  " model origin at its position, scaled by scaleFactor (percent, as the dumps and EQEmu give it; 100 draws the model as built)." + placementHelp
))
async def placeDoor(
  context: Context, zone: str | None, model: str, name: str,
  location: list[float] | None = None, headingDegrees: float | None = None,
  x: float | None = None, y: float | None = None, z: float | None = None, heading: float | None = None,
  scaleFactor: float = 100, source: str | None = None, collection: str | None = None,
):
  if scaleFactor <= 0:
    raise ToolError(f"scaleFactor must be positive, got {scaleFactor}")
  frameLocation, rotation = placementFrame(location, headingDegrees, x, y, z, heading)
  folder, details = await anyio.to_thread.run_sync(eqModel, zone, model, source)
  return await placeEQModel(context, folder, name, frameLocation, rotation, scaleFactor / 100, 0, False, collection, details)


@guardedTool(description=(
  "Place an EverQuest ground object (the dumps' ground spawns: tradeskill containers such as kilns and looms, dropped items, housing"
  " pieces; names like IT10800_ACTORDEF), model origin at its position, scaled by scale." + placementHelp
))
async def placeObject(
  context: Context, zone: str | None, model: str, name: str,
  location: list[float] | None = None, headingDegrees: float | None = None,
  x: float | None = None, y: float | None = None, z: float | None = None, heading: float | None = None,
  scale: float = 1, source: str | None = None, collection: str | None = None,
):
  if scale <= 0:
    raise ToolError(f"scale must be positive, got {scale}")
  frameLocation, rotation = placementFrame(location, headingDegrees, x, y, z, heading)
  folder, details = await anyio.to_thread.run_sync(eqModel, zone, model, source)
  return await placeEQModel(context, folder, name, frameLocation, rotation, scale, 0, False, collection, details)


def zoneModel(zone):
  try:
    return eqZones.buildZone(zoneSources.resolveClientRoot(), toolingRoot / "models", zone)
  except ValueError as error:
    raise ToolError(str(error)) from error


async def placeZone(context, zone, collection):
  folder, details = await anyio.to_thread.run_sync(zoneModel, zone)
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": zone, "location": [0, 0, 0], "rotationDegrees": 0, "scale": 1, "avatarHeight": 0,
    "snapToGround": False, "collection": collection,
  })
  stampKeys = ("zoneCacheFormat", "modelCacheFormat", "indexFormat", "archives", "listingFingerprint", "zone", "textureSources", "lit", "minimum", "maximum")
  return placed | {"source": {key: value for key, value in details.items() if key not in stampKeys}}


def bridgeLights(lights):
  return [{"name": light["name"], "position": list(light["position"]), "color": list(light["color"]), "radius": light["radius"]} for light in lights]


def bridgeEmitters(emitters):
  """Emitters for the bridge; a few client lists leave a name empty, and a Blender object needs one, so those are named for their definition."""
  return [{key: list(value) if key == "position" else value for key, value in emitter.items()} | {"name": emitter["name"] or f"emitter{emitter['definition']}"} for emitter in emitters]


async def placeZoneEnvironment(context, zone, lights, emitters):
  """A placed zone's lights and emitters, each set in its own collection named for the zone; None for what is not read."""
  placed = {"lights": None if lights is None else 0, "emitters": 0}
  if lights:
    placed["lights"] = (await callBridge(context, "placeLights", {"lights": bridgeLights(lights), "collection": f"{zone} lights"}))["lights"]
  if emitters:
    placed["emitters"] = (await callBridge(context, "placeEmitters", {"emitters": bridgeEmitters(emitters), "collection": f"{zone} emitters"}))["emitters"]
  return placed


def readEmitterList(path):
  if path is None or not path.is_file():
    return []
  try:
    return eqEmitters.parseEmitters(path.read_text(encoding="latin1"), path.name)
  except ValueError as error:
    raise ToolError(str(error)) from error


@guardedTool()
async def importZone(context: Context, zone: str, collection: str | None = None):
  """Bring a client zone into the open scene as one object named for it, drawn as the client draws it, with the vertex colors and normals
  the client lights it by: a classic (WLD) zone's region meshes and the objects its objects.wld places, or an EQ terrain zone's tiles
  (each ecosystem's cover and detail textures blended as the client blends them) and the objects and object groups its tiles place on
  the ground, or an EQG (EQGZ) zone's terrain and placed models (the loose .zon beside the archive when the client has one, as it
  loads it), with baked light where its count fits each model. It keeps the zone file's coordinates, which the scene shares (Blender
  x, y are the server's y, x). The zone's lights (classic and EQG zones) come in as point lights in "<zone> lights" and its emitters as
  empties in "<zone> emitters", as placeLights and placeEmitters make them."""
  placed = await placeZone(context, zone, collection)
  clientRoot = zoneSources.resolveClientRoot()
  try:
    lights = await anyio.to_thread.run_sync(eqZones.zoneLights, clientRoot, zone)
    emitters = readEmitterList(eqEmitters.emitterListPath(clientRoot, zone))
  except ValueError as error:
    raise ToolError(str(error)) from error
  return placed | await placeZoneEnvironment(context, zone, lights, emitters)


zoneFileSourceKeys = ("zoneCacheFormat", "modelCacheFormat", "sha256", "textureSources", "lit", "minimum", "maximum")


@guardedTool()
async def importZoneFile(context: Context, path: str, collection: str | None = None):
  """Bring an EQG zone archive outside the client, such as one exportZone wrote, into the open scene as one object named for its zone
  (the file name), drawn as importZone draws the client's EQG zones: its terrain and placed models, with baked light where its count
  fits each model; and its lights and the emitters of the <zone>_EnvironmentEmitters.txt beside it, as importZone brings them."""
  archivePath = Path(path)
  if not archivePath.is_absolute() or archivePath.suffix.lower() != ".eqg" or not archivePath.is_file():
    raise ToolError(f"'{path}' is not an absolute path to an existing .eqg file")
  try:
    folder, details = await anyio.to_thread.run_sync(eqZones.buildZoneFile, toolingRoot / "models", archivePath)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": archivePath.stem.lower(), "location": [0, 0, 0], "rotationDegrees": 0, "scale": 1, "avatarHeight": 0,
    "snapToGround": False, "collection": collection,
  })
  try:
    lights = await anyio.to_thread.run_sync(eqZones.zoneFileLights, archivePath)
  except ValueError as error:
    raise ToolError(str(error)) from error
  emitters = readEmitterList(archivePath.parent / f"{archivePath.stem}_EnvironmentEmitters.txt")
  environment = await placeZoneEnvironment(context, archivePath.stem.lower(), lights, emitters)
  return placed | {"source": {key: value for key, value in details.items() if key not in zoneFileSourceKeys}} | environment


@guardedTool()
async def exportZone(context: Context, path: str):
  """Write the open scene as an EQG zone archive at `path`, an absolute path ending in <zone>.eqg, the zone's short name in lowercase
  letters and digits. The `terrain` collection's meshes become the zone's terrain; every other rendered mesh becomes a model placed at
  its object's transform (copies sharing a mesh and without modifiers share one model) and every collection instance a model of its
  collection's meshes; a placed object takes one uniform scale. Materials must come from createMaterial: diffuse and normal map export
  as Opaque_MaxCB1.fx, diffuse only as Opaque_MaxC1.fx, a cutout (diffuse only) as Chroma_MPLBasicAT.fx. DDS textures are stored
  unchanged, others as uncompressed DDS with power-of-two sides. Point lights placed with placeLight go into the .zon; emitters placed with
  placeEmitter go into <zone>_EnvironmentEmitters.txt beside the archive (the client reads that list loose from its own folder). No baked
  light is written yet."""
  archivePath = Path(path)
  if not archivePath.is_absolute() or archivePath.suffix != ".eqg" or not archivePath.parent.is_dir():
    raise ToolError(f"'{path}' is not an absolute .eqg path in an existing folder")
  zone = archivePath.stem
  if not eqgExport.zoneNamePattern.match(zone):
    raise ToolError(f"Zone name '{zone}' must be lowercase letters and digits, as the client's zone short names are")
  collected = await callBridge(context, "collectZoneExport", {"outputFolder": str(toolingRoot / "exports" / zone), "zoneName": zone})
  try:
    data, summary = await anyio.to_thread.run_sync(eqgExport.zoneArchive, collected)
    emitterListPath = archivePath.parent / f"{zone}_EnvironmentEmitters.txt"
    emitterList = eqEmitters.emitterListText(collected["emitters"]) if collected["emitters"] else None
    archivePath.write_bytes(data)
    # A list left from an earlier export would place emitters this scene no longer has.
    if emitterList is None:
      emitterListPath.unlink(missing_ok=True)
    else:
      emitterListPath.write_bytes(emitterList.encode("latin1"))
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  return summary | {"path": str(archivePath), "emitterList": str(emitterListPath) if emitterList is not None else None}


def passArrays(passes):
  """Render passes as arrays, their colors no longer premultiplied by coverage as a transparent render stores them."""
  arrays = {}
  for name, path in passes.items():
    array = numpy.load(path)
    coverage = array[..., 3:4]
    array[..., :3] = numpy.where(coverage > 0, array[..., :3] / numpy.maximum(coverage, 1e-6), 0)
    arrays[name] = array
  return arrays


# How far from a screenshot's camera calibrateShot places what a recording shows.
recordingReach = 1000.0
# The recording writes -1 for a look value a spawn does not set (a non-Drakkin's tattoo, a WLD model's head override); the client reads
# such indexes as past their count, which draws 0.
recordedLookKeys = ("variation", "headType", "faceStyle", "hairStyle", "hairColor", "facialHair", "facialHairColor", "eyeColor1", "heritage", "tattoo", "details")


def recordedAppearance(look):
  appearance = {key: max(int(look[key]), 0) for key in recordedLookKeys}
  return appearance | {"textureSet": max(int(look["textureType"]), 0)}


async def placeRecorded(context, recordingPath, liveDumpsPath, atMilliseconds, near, radius, collection):
  """Place the NPCs, doors, ground items, and placed objects a recording shows in its zone at a moment (within radius of near, a Blender
  location, when given); each one that cannot be drawn is listed with why instead."""
  try:
    zone = await anyio.to_thread.run_sync(eqRecording.zoneAt, recordingPath, atMilliseconds)
    sizes = await anyio.to_thread.run_sync(eqRecording.propSizes, liveDumpsPath, zone)
  except (OSError, ValueError, KeyError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error

  def inReach(x, y, z):
    return near is None or float(numpy.linalg.norm(numpy.array([y, x, z]) - numpy.array(near))) <= radius

  placed, notPlaced = [], []
  newEngine = await zoneIsNewEngine(context)
  for npc in zone["npcs"]:
    if not inReach(npc["x"], npc["y"], npc["z"]):
      continue
    look = npc["appearance"]
    try:
      spawn = await anyio.to_thread.run_sync(spawnModel, zone["zone"], look["actorDef"], npc["height"], newEngine, None, recordedAppearance(look), None)
      location, rotation = placementFrame(None, None, npc["x"], npc["y"], npc["z"], npc["heading"])
      await placeEQModel(context, spawn["folder"], npc["name"], location, rotation, spawn["scale"], spawn["avatarHeight"], True, collection, spawn["details"])
      placed.append({"kind": "npc", "name": npc["name"], "model": look["actorDef"]})
    except ToolError as error:
      notPlaced.append({"kind": "npc", "name": npc["name"], "model": look["actorDef"], "reason": str(error)})
  for prop in zone["props"]:
    if not inReach(prop["x"], prop["y"], prop["z"]):
      continue
    name = f"{prop['kind']}{prop['id']}_{prop['name']}"
    scale, reason = eqRecording.propSize(sizes, prop)
    if reason is not None:
      notPlaced.append({"kind": prop["kind"], "name": name, "model": prop["name"], "reason": reason})
      continue
    try:
      folder, details = await anyio.to_thread.run_sync(eqModel, zone["zone"], prop["name"])
      location, rotation = placementFrame(None, None, prop["x"], prop["y"], prop["z"], prop["heading"])
      await placeEQModel(context, folder, name, location, rotation, scale, 0, False, collection, details)
      placed.append({"kind": prop["kind"], "name": name, "model": prop["name"]})
    except ToolError as error:
      notPlaced.append({"kind": prop["kind"], "name": name, "model": prop["name"], "reason": str(error)})
  return {"zone": zone["zone"], "server": zone["server"], "placed": placed, "notPlaced": notPlaced}


@guardedTool()
async def placeRecording(
  context: Context, recordingPath: str, liveDumpsPath: str, at: str, near: list[float] | None = None, radius: float | None = None,
  collection: str | None = None,
):
  """Place what a live behavior recording (MQ2PeridotLive's peridotLiveBehavior_<server>_<zone>_<instance>_<character>_<start>.txt) shows
  in its zone at a moment: at is local time as the recording's start line writes it (YYYYMMDD-HHMMSS). NPCs stand where they last moved
  to, in their latest look (without equipment, which is not drawn yet) and the stand pose; doors (drawn closed), ground items, and placed
  objects take their latest state, with the scales (and ground items' tilts) the recording lacks from the live dumps' master folder
  (liveDumpsPath, holding doors.tsv and ground.tsv). Players are left out: the recording
  character is the camera. near ([x, y, z], Blender) and radius limit it to what stands within reach. Set the zone's newEngineZone first
  (setZoneProperties), as for placeSpawn. Each one the client data cannot draw (live-only models are common) is listed with why."""
  if (near is None) != (radius is None):
    raise ToolError("near and radius go together")
  try:
    start = await anyio.to_thread.run_sync(eqRecording.recordingStart, recordingPath)
    moment = datetime.datetime.strptime(at, eqRecording.timeFormat)
  except (OSError, ValueError) as error:
    raise ToolError(str(error)) from error
  return await placeRecorded(context, recordingPath, liveDumpsPath, (moment - start).total_seconds() * 1000, near, radius, collection)


@guardedTool()
async def calibrateShot(
  context: Context, screenshotPath: str, zone: str, newEngineZone: bool, fogColor: list[float] | None = None, fogStart: float | None = None,
  fogEnd: float | None = None, fogDensity: float | None = None, recordingPath: str | None = None, liveDumpsPath: str | None = None,
  discardUnsavedChanges: bool = False,
):
  """Calibrate the renderer against a live client screenshot named <zone>,<loc y>,<loc x>,<loc z>,<compass heading>,<pitch>.jpg (see the
  calibrate-renderer skill). Opens a new file (discarding unsaved changes only with discardUnsavedChanges), imports zone (the client's
  zone file name), renders the screenshot's view, fits the scene light (ambient, sun, bounce, sun direction) that best explains the
  screenshot under the client's lighting, renders with it, and returns the screenshot beside the render. Give the fog from the zone
  header (the live dumps' zoneHeaders; density 0 for a zone whose FogOnOff is 0), all four values; with none given, the fog's start,
  end, and color are fitted with the light, at the client's density. Give recordingPath, a live behavior recording of the zone running
  when the screenshot was taken, and liveDumpsPath to place what stood within recordingReach of the camera at that moment (see
  placeRecording). Each run is
  kept under the tooling root's calibration folder with its fit and mean pixel difference, and getToolingStatus lists the latest per
  screenshot."""
  try:
    shot = eqCalibration.parseShotName(screenshotPath)
  except ValueError as error:
    raise ToolError(str(error)) from error
  if not Path(screenshotPath).is_file():
    raise ToolError(f"No screenshot at {screenshotPath}")
  fogGiven = [value is not None for value in (fogColor, fogStart, fogEnd, fogDensity)]
  if any(fogGiven) and not all(fogGiven):
    raise ToolError("Give all of fogColor, fogStart, fogEnd, and fogDensity (the zone header's fog), or none to fit the fog")
  view = eqCalibration.shotView(shot)
  await callBridge(context, "newFile", {"discardUnsavedChanges": discardUnsavedChanges})
  imported = await placeZone(context, zone, None)
  neutral = {"ambientColor": [1, 1, 1], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0, 0, 0], "sunAzimuthDegrees": 0, "sunElevationDegrees": 45}
  if all(fogGiven):
    environment = {"fogColor": fogColor, "fogStart": fogStart, "fogEnd": fogEnd, "fogDensity": fogDensity, "newEngineZone": newEngineZone}
  else:
    environment = {"fogColor": [0, 0, 0], "fogStart": 0, "fogEnd": 100000, "fogDensity": 0, "newEngineZone": newEngineZone}
  await callBridge(context, "setZoneProperties", {"updates": neutral | environment})
  recorded = None
  if (recordingPath is None) != (liveDumpsPath is None):
    raise ToolError("recordingPath and liveDumpsPath go together")
  if recordingPath is not None:
    try:
      start = await anyio.to_thread.run_sync(eqRecording.recordingStart, recordingPath)
    except (OSError, ValueError) as error:
      raise ToolError(str(error)) from error
    # The screenshot's file time is when the client took it.
    taken = datetime.datetime.fromtimestamp(Path(screenshotPath).stat().st_mtime)
    recorded = await placeRecorded(context, recordingPath, liveDumpsPath, (taken - start).total_seconds() * 1000, view["eye"], recordingReach, None)
  runFolder = toolingRoot / "calibration" / Path(screenshotPath).stem / datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
  runFolder.mkdir(parents=True, exist_ok=True)
  measured = passArrays((await callBridge(context, "renderPasses", {"view": view, "outputFolder": str(runFolder), "passNames": ["lit", "base", "normal", "baked", "share", "distance"]}))["passes"])
  height, width = measured["lit"].shape[:2]
  screen = await anyio.to_thread.run_sync(eqCalibration.screenshotPixels, screenshotPath, width, height)
  givenFog = {"fogStart": fogStart, "fogEnd": fogEnd, "fogDensity": fogDensity} if all(fogGiven) else None
  try:
    fit = await anyio.to_thread.run_sync(eqCalibration.fitScene, screen, measured, givenFog)
  except ValueError as error:
    raise ToolError(str(error)) from error
  lightingKeys = ("ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "sunAzimuthDegrees", "sunElevationDegrees")
  if givenFog is None:
    environment |= {key: fit[key] for key in ("fogColor", "fogStart", "fogEnd", "fogDensity")}
  await callBridge(context, "setZoneProperties", {"updates": {key: fit[key] for key in lightingKeys} | environment})
  rendered = passArrays((await callBridge(context, "renderPasses", {"view": view, "outputFolder": str(runFolder), "passNames": ["lit"]}))["passes"])["lit"]
  background = numpy.concatenate([numpy.array(environment["fogColor"]), [1.0]])
  composited = rendered * rendered[..., 3:4] + background * (1 - rendered[..., 3:4])
  image, difference = eqCalibration.comparison(screen, numpy.concatenate([composited[..., :3], rendered[..., 3:4]], axis=2))
  comparePath = runFolder / "compare.png"
  image.save(comparePath)
  result = {
    "screenshot": Path(screenshotPath).name, "zone": zone, "view": view, "fit": fit, "meanPixelDifference": difference, "environment": environment,
    "comparePath": str(comparePath), "time": runFolder.name,
  }
  (runFolder / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
  preview = io.BytesIO()
  image.resize((image.width * 2 // 3, image.height * 2 // 3)).save(preview, format="PNG")
  return [Image(data=preview.getvalue(), format="png"), result | {"import": imported["source"], "recorded": recorded}]


@guardedTool()
async def pick(context: Context, view: dict, pixel: list[int]):
  """What is under a pixel ([x, y] from the top-left of the 960x540 render) of a view: object, world position, normal, material, distance."""
  return await callBridge(context, "pick", {"view": view, "pixel": pixel})


selectorHelp = (
  " A selector picks part of a mesh by world position or surface: {\"all\": true}, {\"sphere\": {\"center\": [x,y,z], \"radius\": r}},"
  " {\"box\": {\"minimum\": [x,y,z], \"maximum\": [x,y,z]}}, {\"cylinder\": {\"center\": [x,y], \"radius\": r, \"bottom\": z, \"top\": z}},"
  " {\"facing\": {\"direction\": [x,y,z], \"withinDegrees\": d}}, {\"slope\": {\"minimumDegrees\": a, \"maximumDegrees\": b}} (0 flat, 90 vertical, over 90 overhanging), {\"height\": {\"minimum\": z, \"maximum\": z}}, {\"nearPath\": {\"path\": [[x,y,z], ...], \"radius\": r}} (horizontal distance), {\"material\": name}, {\"vertexGroup\": name}, {\"insideObject\": closedMeshName},"
  " {\"and\": [selectors]}, {\"or\": [selectors]}, {\"not\": selector}. Shapes test vertex positions, or face centers for face operations."
  " A selector that matches nothing is an error."
)
allSelector = {"all": True}


@guardedTool()
async def createPrimitive(
  context: Context, kind: str, name: str, size: list[float], location: list[float],
  rotationDegrees: list[float] = [0, 0, 0], collection: str | None = None, segments: int | None = None, divisions: list[int] | None = None,
):
  """Block out a mesh: plane, grid, cube, cylinder, cone, or sphere built to exactly `size` [x, y, z] (z ignored for plane and grid), origin at its base center (center for flat shapes). Round shapes need `segments`; a grid needs `divisions` [x, y]."""
  return await callBridge(context, "createPrimitive", {"kind": kind, "name": name, "size": size, "location": location, "rotationDegrees": rotationDegrees, "collection": collection, "segments": segments, "divisions": divisions})


@guardedTool()
async def createTerrainGrid(context: Context, name: str, size: list[float], spacing: float, location: list[float], collection: str | None = None):
  """A flat terrain grid of `size` [x, y] with a vertex every `spacing` units (EQ terrain uses 8 to 24), centered on `location`, ready to sculpt."""
  return await callBridge(context, "createTerrainGrid", {"name": name, "size": size, "spacing": spacing, "location": location, "collection": collection})


@guardedTool()
async def transformObjects(
  context: Context, names: list[str], translate: list[float] | None = None, rotateDegrees: list[float] | None = None, scale: list[float] | None = None,
  location: list[float] | None = None, rotationDegrees: list[float] | None = None,
):
  """Move, rotate, or scale objects: relative (translate, rotateDegrees about world axes, scale factors) or absolute (location, rotationDegrees); not both forms of one channel."""
  return await callBridge(context, "transformObjects", {"names": names, "translate": translate, "rotateDegrees": rotateDegrees, "scale": scale, "location": location, "rotationDegrees": rotationDegrees})


@guardedTool()
async def duplicateObjects(context: Context, names: list[str], offset: list[float], linkData: bool = False):
  """Copy objects, offset from the originals; linkData shares the mesh instead of copying it. Returns original to copy names."""
  return await callBridge(context, "duplicateObjects", {"names": names, "offset": offset, "linkData": linkData})


@guardedTool()
async def joinObjects(context: Context, names: list[str], into: str):
  """Merge meshes into one object, for example a trunk and canopy into one tree; `into` keeps its name, origin, and transform, and the others are removed."""
  return await callBridge(context, "joinObjects", {"names": names, "into": into})


@guardedTool()
async def deleteObjects(context: Context, names: list[str]):
  """Delete objects; meshes left with no users are removed too."""
  return await callBridge(context, "deleteObjects", {"names": names})


@guardedTool()
async def organize(context: Context, renames: dict[str, str] | None = None, parents: dict[str, str | None] | None = None, collections: dict[str, str] | None = None):
  """Rename objects (old to new, applied first), then set parents (child to parent, or null to clear; world transform kept) and move objects into collections (created if missing), using the new names."""
  return await callBridge(context, "organize", {"renames": renames, "parents": parents, "collections": collections})


@guardedTool()
async def getObjectDetail(context: Context, name: str):
  """One object in depth: transform, world bounds, parent, collections, modifiers; for meshes the vertex, face, and triangle counts, faces per material, UV layers, world units per texture repeat, vertex groups."""
  return await callBridge(context, "getObjectDetail", {"name": name})


@guardedTool()
async def measure(context: Context, points: list[list[float]], snapToSurface: bool = False):
  """Points and the distances, horizontal distances, height changes, and slopes between consecutive ones; snapToSurface drops each point onto the surface below it first (one point gives a surface height)."""
  return await callBridge(context, "measure", {"points": points, "snapToSurface": snapToSurface})


@guardedTool(description="Move the selected vertices of a mesh by `offset` [x, y, z] world units. With `falloff` {center, radius, curve: constant|linear|smooth|sharp} the move fades with distance from the center; this is the precise, fine-detail edit. With shaping passes, the move goes into the active pass." + selectorHelp)
async def moveVertices(context: Context, objectName: str, selector: dict, offset: list[float], falloff: dict | None = None):
  return await callBridge(context, "moveVertices", {"objectName": objectName, "selector": selector, "offset": offset, "falloff": falloff})


@guardedTool()
async def sculptAtPoint(
  context: Context, objectName: str, mode: str, center: list[float], radius: float, strength: float,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1,
):
  """Sculpt a mesh within `radius` of `center`: raise, lower, or crease (strength in units, along the region's average normal or `direction`); smooth or flatten (strength a fraction 0 to 1; smooth repeats `iterations` times). Falloff curve: constant, linear, smooth, sharp. With shaping passes, the change goes into the active pass."""
  return await callBridge(context, "sculptAtPoint", {"objectName": objectName, "mode": mode, "center": center, "radius": radius, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations})


@guardedTool()
async def sculptAlongPath(
  context: Context, objectName: str, mode: str, path: list[list[float]], strength: float, radius: float | None = None, radii: list[float] | None = None,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1, profile: list[list[float]] | None = None, conformRim: bool | None = None,
):
  """Sculpt along a polyline path [[x,y,z], ...] within `radius`, or within `radii` (one per path point, the stroke widening or narrowing evenly between them, so one stroke carves a canyon that pinches to a gorge): raise, lower, crease, smooth, flatten as in sculptAtPoint; carve, which cuts vertically down to the path's own heights shaped by `profile` [[lateralFraction, heightAboveFloor], ...] from 0 (center) to 1 (edge); or fill, which raises ground up to such a profile (a mesa: a flat cap, a cliff, a slope at the base). carve and fill strength is a fraction, and their path can be a single point (a pit or a butte). With conformRim (carve only, on by default; needs rising profile heights), vertices just outside the cut slide onto the rim contour so the edge follows the profile rather than the grid; the mesh's open edge stays put. Results count foldedFaces: faces the move turned over, a sign it was too strong for the mesh's spacing. With shaping passes, the change goes into the active pass."""
  return await callBridge(context, "sculptAlongPath", {"objectName": objectName, "mode": mode, "path": path, "radius": radius, "radii": radii, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations, "profile": profile, "conformRim": conformRim})


@guardedTool()
async def addShapingPass(context: Context, objectName: str, name: str):
  """Add a named shaping pass to a mesh and make it active: vertex moves and sculpting go into the active pass, which can later be
  turned up or down, muted, removed, or collapsed, so a shaping step is revised without redoing the others. Tools that change faces
  (delete, extrude, inset, bevel, subdivide, cut, decimate, join) refuse while a mesh has passes; collapse them first."""
  return await callBridge(context, "addShapingPass", {"objectName": objectName, "name": name})


@guardedTool()
async def setShapingPass(context: Context, objectName: str, name: str, strength: float | None = None, muted: bool | None = None, makeActive: bool = False):
  """Change a shaping pass: its strength (-1 to 2: 0.5 halves what it holds, -1 inverts it), muted, or makeActive so shaping goes into it."""
  return await callBridge(context, "setShapingPass", {"objectName": objectName, "name": name, "strength": strength, "muted": muted, "makeActive": makeActive})


@guardedTool()
async def removeShapingPass(context: Context, objectName: str, name: str):
  """Remove a shaping pass and what it holds; the other passes keep theirs."""
  return await callBridge(context, "removeShapingPass", {"objectName": objectName, "name": name})


@guardedTool()
async def collapseShapingPasses(context: Context, objectName: str):
  """Make a mesh's shape as it is seen (its passes combined) its new base and drop the passes, so its faces can change again."""
  return await callBridge(context, "collapseShapingPasses", {"objectName": objectName})


@guardedTool(description=(
  "Roughen the selected vertices of a mesh with fractal noise: bumps about `featureSize` units across, moving vertices `amplitude` units"
  " as a typical (root mean square) move, the largest about three times that, along each vertex's normal (`direction` normal: sideways on a wall, so cliffs break up too) or straight up. `octaves` (1-8) add finer"
  " noise, each twice as fine and `roughness` times as strong. The same `seed` gives the same noise. `fadeDistance` ramps the effect"
  " in from the selection's edge so a mask leaves no step. Use it in its own shaping pass, coarse first (large featureSize, few octaves),"
  " then finer, turning each pass up or down after looking." + selectorHelp))
async def roughen(
  context: Context, objectName: str, featureSize: float, amplitude: float, octaves: int = 4, roughness: float = 0.5, seed: int = 0,
  direction: str = "normal", selector: dict = allSelector, fadeDistance: float = 0.0,
):
  return await callBridge(context, "roughen", {
    "objectName": objectName, "featureSize": featureSize, "amplitude": amplitude, "octaves": octaves, "roughness": roughness, "seed": seed,
    "direction": direction, "selector": selector, "fadeDistance": fadeDistance,
  })


@guardedTool(description=(
  "Warp the selected vertices of a mesh (the result counts foldedFaces, faces turned over: too much warp for the spacing): move them by smooth noise about `featureSize` units across, `amplitude` units as a typical (root"
  " mean square) move and the largest about three times that, so round"
  " and straight shapes (a sculpted cone hill, a carved channel) stop being regular. `plane` horizontal keeps heights and bends the shape"
  " sideways; surface moves along the surface; full moves in every direction. The same `seed` gives the same warp; `fadeDistance` ramps"
  " it in from the selection's edge. Use it in its own shaping pass." + selectorHelp))
async def warp(
  context: Context, objectName: str, featureSize: float, amplitude: float, seed: int = 0, plane: str = "horizontal",
  selector: dict = allSelector, fadeDistance: float = 0.0,
):
  return await callBridge(context, "warp", {
    "objectName": objectName, "featureSize": featureSize, "amplitude": amplitude, "seed": seed, "plane": plane, "selector": selector,
    "fadeDistance": fadeDistance,
  })


@guardedTool(description="Delete the selected faces of a mesh, with edges and vertices left unused; for example the terrain inside a rock that should form its own cave floor ({\"insideObject\": \"rockName\"})." + selectorHelp)
async def deleteFaces(context: Context, objectName: str, selector: dict):
  return await callBridge(context, "deleteFaces", {"objectName": objectName, "selector": selector})


@guardedTool(description="Extrude the selected faces of a mesh by `distance` units along their average normal, or along `direction`." + selectorHelp)
async def extrudeFaces(context: Context, objectName: str, selector: dict, distance: float, direction: list[float] | None = None):
  return await callBridge(context, "extrudeFaces", {"objectName": objectName, "selector": selector, "distance": distance, "direction": direction})


@guardedTool(description="Inset the selected faces of a mesh as one region by `thickness`, pushed in or out by `depth`." + selectorHelp)
async def insetFaces(context: Context, objectName: str, selector: dict, thickness: float, depth: float = 0.0):
  return await callBridge(context, "insetFaces", {"objectName": objectName, "selector": selector, "thickness": thickness, "depth": depth})


@guardedTool(description="Bevel the edges whose two vertices are both selected, by `width` units in `segments` steps; `minimumAngleDegrees` limits it to edges at least that sharp." + selectorHelp)
async def bevelEdges(context: Context, objectName: str, selector: dict, width: float, segments: int = 1, minimumAngleDegrees: float | None = None):
  return await callBridge(context, "bevelEdges", {"objectName": objectName, "selector": selector, "width": width, "segments": segments, "minimumAngleDegrees": minimumAngleDegrees})


@guardedTool(description="Subdivide the selected faces of a mesh with `cuts` cuts per edge, adding detail where it is needed." + selectorHelp)
async def subdivide(context: Context, objectName: str, cuts: int, selector: dict = allSelector):
  return await callBridge(context, "subdivide", {"objectName": objectName, "selector": selector, "cuts": cuts})


@guardedTool()
async def booleanCut(context: Context, objectName: str, cutterName: str, operation: str = "DIFFERENCE", keepCutter: bool = False):
  """Apply a boolean (DIFFERENCE, UNION, INTERSECT) of a cutter mesh to a mesh, for cave mouths and openings; the cutter is deleted unless keepCutter."""
  return await callBridge(context, "booleanCut", {"objectName": objectName, "cutterName": cutterName, "operation": operation, "keepCutter": keepCutter})


@guardedTool()
async def decimate(context: Context, objectName: str, ratio: float):
  """Reduce a mesh to roughly `ratio` (0 to 1) of its triangles."""
  return await callBridge(context, "decimate", {"objectName": objectName, "ratio": ratio})


@guardedTool()
async def cleanupMesh(context: Context, objectName: str, mergeDistance: float = 0.01, recalculateNormals: bool = True):
  """Merge vertices closer than mergeDistance, dissolve degenerate geometry, and make face normals consistent."""
  return await callBridge(context, "cleanupMesh", {"objectName": objectName, "mergeDistance": mergeDistance, "recalculateNormals": recalculateNormals})


@guardedTool()
async def placeLights(context: Context, lights: list[dict], collection: str | None = "lights"):
  """Place zone lights: point lights [{name, position [x,y,z], color [r,g,b] 0-1 as the client stores it, radius (reach in units)}] in
  `collection`. exportZone writes them into the zone's .zon; the preview does not draw lights yet. The asset catalog's light styles
  (findAssets kind light) give the colors and radii client zones use for torches, braziers, fill light, and the rest."""
  return await callBridge(context, "placeLights", {"lights": lights, "collection": collection})


@guardedTool()
async def placeEmitters(context: Context, emitters: list[dict], collection: str | None = "emitters"):
  """Place particle emitters: [{name, position [x,y,z], definition (the client emitter definition index), lifespan (the list's lifespan
  field; 4000000 on most of the client's emitters)}] as empties in `collection`. exportZone writes them to <zone>_EnvironmentEmitters.txt
  beside the archive; the preview does not draw them yet. The asset catalog (findAssets kind emitter) says what each definition shows
  and under which names client zones place it."""
  return await callBridge(context, "placeEmitters", {"emitters": [emitter | {"alwaysVisible": None} for emitter in emitters], "collection": collection})


@guardedTool()
async def createMaterial(context: Context, name: str, diffuseTexture: str, normalTexture: str | None = None, cutout: bool = False, alphaThreshold: float = 0.5):
  """A Phase 1 material: diffuse texture, optional normal map, no shine; cutout makes the diffuse alpha a hard alpha test for foliage
  cards. A texture is an absolute path or a catalog texture id (texture/<name>@<hash>), which uses the catalog's extracted file."""

  def texturePath(texture):
    if texture is None or not texture.startswith("texture/"):
      return texture
    asset = catalogCall(catalog.requireAsset, texture)
    if "file" not in asset["measured"]:
      raise ToolError(f"{texture} has no readable file: {asset['measured'].get('problem')}")
    return asset["measured"]["file"]

  return await callBridge(context, "createMaterial", {"name": name, "diffuseTexture": texturePath(diffuseTexture), "normalTexture": texturePath(normalTexture), "cutout": cutout, "alphaThreshold": alphaThreshold})


@guardedTool(description="Assign a material to the selected faces of a mesh, adding a material slot if needed." + selectorHelp)
async def assignMaterial(context: Context, objectName: str, materialName: str, selector: dict = allSelector):
  return await callBridge(context, "assignMaterial", {"objectName": objectName, "materialName": materialName, "selector": selector})


@guardedTool(description="Project UVs onto the selected faces from world positions so one texture repeat spans `worldUnitsPerRepeat` units: `planar` along `direction`, or `box`, which projects each face along its dominant axis so steep faces do not stretch." + selectorHelp)
async def projectUVs(context: Context, objectName: str, method: str, worldUnitsPerRepeat: float, selector: dict = allSelector, direction: list[float] | None = None):
  return await callBridge(context, "projectUVs", {"objectName": objectName, "method": method, "worldUnitsPerRepeat": worldUnitsPerRepeat, "selector": selector, "direction": direction})


@guardedTool()
async def placeOnSurface(context: Context, objectNames: list[str], at: list[list[float]] | None = None, alignToNormal: bool = False, surfaceObjects: list[str] | None = None, offset: float = 0.0):
  """Drop objects onto the surface below `at` points (or below their own origins, cast from just above), optionally tilted to the surface normal and restricted to surfaceObjects."""
  return await callBridge(context, "placeOnSurface", {"objectNames": objectNames, "at": at, "alignToNormal": alignToNormal, "surfaceObjects": surfaceObjects, "offset": offset})


@guardedTool()
async def scatterInRegion(
  context: Context, sourceObject: str, region: dict, density: float, minimumSpacing: float, collection: str,
  yawRangeDegrees: list[float] = [0, 360], scaleRange: list[float] = [1, 1], alignToNormal: bool = False,
  maximumSlopeDegrees: float = 90, surfaceObjects: list[str] | None = None, castFromHeight: float | None = None, seed: int = 0,
  avoidObjects: list[str] | None = None, avoidClearance: float = 0.0,
):
  """Scatter linked copies of an object over a region ({"circle": {center, radius}} or {"polygon": [[x,y], ...]}): `density` per 10,000 square units, at least `minimumSpacing` apart, random yaw and scale within ranges, dropped onto surfaces from `castFromHeight` (default just above the scene), skipped where steeper than maximumSlopeDegrees or inside or within avoidClearance of any avoidObjects. Deterministic for a seed."""
  return await callBridge(context, "scatterInRegion", {
    "sourceObject": sourceObject, "region": region, "density": density, "minimumSpacing": minimumSpacing, "yawRangeDegrees": yawRangeDegrees,
    "scaleRange": scaleRange, "alignToNormal": alignToNormal, "maximumSlopeDegrees": maximumSlopeDegrees, "surfaceObjects": surfaceObjects,
    "castFromHeight": castFromHeight, "seed": seed, "collection": collection, "avoidObjects": avoidObjects, "avoidClearance": avoidClearance,
  })


@guardedTool()
async def markAsset(context: Context, collectionName: str):
  """Mark a collection as an asset so other files can link it with linkKitAsset; kit libraries are .blend files of marked collections."""
  return await callBridge(context, "markAsset", {"collectionName": collectionName})


@guardedTool()
async def linkKitAsset(
  context: Context, kitPath: str, assetName: str, instanceName: str, location: list[float],
  rotationDegrees: list[float] = [0, 0, 0], scale: list[float] = [1, 1, 1], collection: str | None = None,
):
  """Link a collection marked as an asset in a kit .blend (absolute path) and place an instance of it."""
  return await callBridge(context, "linkKitAsset", {"kitPath": kitPath, "assetName": assetName, "instanceName": instanceName, "location": location, "rotationDegrees": rotationDegrees, "scale": scale, "collection": collection})



serverSourcePaths = loadedServerSources()
serverSourceFingerprint = sourceSignatures(serverSourcePaths)


def main():
  toolingLog.configureLogging(toolingRoot)
  atexit.register(bridge.stop)
  server.run()


if __name__ == "__main__":
  main()
