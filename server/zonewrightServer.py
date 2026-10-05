import atexit
import datetime
import functools
import hashlib
import inspect
import io
import json
import math
import os
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
import checkpoints
import conceptComparison
import emitterAssets
import eqAxes
import eqCalibration
import eqCubeMaps
import eqEmitterDefinitions
import eqEmitters
import eqgExport
import eqModels
import eqRaces
import eqRecording
import eqSky
import eqTextures
import eqZones
import extensionCatalog
import machineProfile
import planDrawing
import playerScale
import skyDrawing
import toolingLog
import viewSheets
import toolingManifest
import toolingStatus
import toolingSync
import surveyFields
import zoneInterpretation
import zoneSources
import zoneSurvey

toolingRoot = toolingStatus.resolveToolingRoot()
bridge = blenderBridge.BlenderBridge(toolingRoot)
serverDirectory = Path(__file__).resolve().parent
zoneSurveySkillPath = serverDirectory.parent / ".claude" / "skills" / "zone-survey" / "SKILL.md"

server = MCPServer(
  "zonewright",
  instructions=(
    "Hands and eyes in a headless Blender for building EverQuest zones, plus the pinned tooling, a survey of the client's zones, and a"
    " catalog of its graphical assets.\n\nBuild zones as a 3D environment artist does, not as a generator. Plan by intent: divide the"
    " zone into regions (createRegion) from the concept and give each its own intent before shaping or surfacing it, and work region by"
    " region at any scale. Decide by hand: a slope or height recipe belongs to a region, as the client's terrain ecosystems do (each"
    " painted area its own palette and thresholds, targeted height bands, softened edges), and generators (warp, roughen, scatter) work"
    " inside an area you chose; never one hard rule and one palette across the whole zone. Paint surfacing into layers by region and"
    " stroke (paintSurface) and shape its edges deliberately (editSurface). Building a zone is visual iteration: iterate rough to fine and judge"
    " every pass and every review in pictures (renderView at eye height, from above, and close on the part being worked from several sides);"
    " measure, walkRoute, compareWithClientZones, and scripts check what a picture shows and never replace it. Take back what does not work"
    " (passes, resetRegion, rebuildRegion, clearRegion, eraseSurface): removing is a way of adding. Before a risky change, keep a"
    " recovery point (saveCheckpoint); restoreCheckpoint is for recovering from a wreck, not for iterating."
    " Steer by the EQ worlds: compareWithClientZones against reference zones, and the catalog's measured use of each asset. The"
    " author-zone skill and docs/zoneWorkflow.md in the zonewright repository hold the procedure."
  ),
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
    return list(location), eqAxes.turnFromHeading(headingDegrees)
  if None in (x, y, z, heading):
    raise ToolError("x, y, z, and heading go together")
  # The server and the live dumps give positions in the server's axes: measured, every kind of placement lands on the zone geometry
  # only through eqAxes' swap.
  return eqAxes.zoneFromServer([x, y, z]), eqAxes.turnFromEQHeading(heading)


def modelSummary(details):
  """What a placement drew and from where, including anything the client data lacks."""
  definition = details["definition"]
  return {
    "model": details["model"], "kind": definition["kind"], "archive": definition["archive"], "linkedBy": definition["via"], "tier": definition["tier"],
    "pose": details["pose"], "pieces": details["pieces"], "unattached": details["unattached"], "swappedMaterials": details["swappedMaterials"],
    "missingTextures": details["missingTextures"], "droppedTriangles": details["droppedTriangles"], "particleCloudsNotDrawn": details["particleCloudsNotDrawn"],
  }


async def placeEQModel(context, folder, name, location, rotationDegrees, scale, avatarHeight, snapToGround, collection, details, clientContent):
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": name, "location": location, "rotationDegrees": rotationDegrees, "scale": scale,
    "avatarHeight": avatarHeight, "snapToGround": snapToGround, "collection": collection, "clientContent": clientContent,
  })
  return placed | {"source": modelSummary(details)}


async def resolveSky(sky):
  """A sky {type, weather, hour, minute} resolved against the client's sky files (eqSky.skyState)."""
  try:
    return await anyio.to_thread.run_sync(eqSky.skyState, zoneSources.resolveClientRoot(), toolingRoot / "sky", sky)
  except (OSError, ValueError) as error:
    raise ToolError(str(error)) from error


async def zoneSky(zone):
  """The open zone's sky state for a preview, or None when it draws none."""
  return await resolveSky(zone["sky"]) if zone.get("sky", skyDrawing.noSky) != skyDrawing.noSky else None


async def previewEmitterAssets():
  """The client's environment emitter definitions and textures, prepared for previews to draw emitters from, or None without a client
  (a preview of a scene holding emitters then refuses)."""
  if not os.environ.get("EVERQUEST_CLIENT"):
    return None
  try:
    return str(await anyio.to_thread.run_sync(emitterAssets.prepareAssets, zoneSources.resolveClientRoot(), toolingRoot))
  except (OSError, ValueError) as error:
    raise ToolError(str(error)) from error


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
async def surveyZones(context: Context, zones: list[str] | None = None, groups: list[str] | None = None, sortBy: str | None = None, limit: int | None = None, verifyHashes: bool = False):
  """Technical lane: measured field groups (dimensions when omitted) for the named zones (all when omitted), in zone name order, or
  sorted descending by sortBy, a dotted field path into one of the requested groups (rows without a number there last). Names that are
  no zone in the client are listed in unknownZones, and the known ones answered. Cached by file hash and group version; file hashes are
  reused while a file's size and modification time are unchanged unless verifyHashes."""
  groupNames = groups if groups is not None else ["dimensions"]
  zoneSurvey.validateMeasuredGroups(groupNames)
  if sortBy is not None and sortBy.split(".")[0] not in groupNames:
    raise ToolError(f"sortBy '{sortBy}' sorts by the {sortBy.split('.')[0]} group, which this call does not request (groups {groupNames}); request it too, or sort by a field of a requested group")
  clientRoot = zoneSources.resolveClientRoot()
  zoneNames = [zone.lower() for zone in zones] if zones is not None else None
  surveys, unknownZones = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, zoneNames, groupNames, progressReporter(context), verifyHashes)
  rows = [{"variant": key} | survey for key, survey in surveys.items()]
  if sortBy is not None:
    sortable = sorted((row for row in rows if isinstance(sortValue(row, sortBy), (int, float))), key=lambda row: sortValue(row, sortBy), reverse=True)
    rows = sortable + [row for row in rows if not isinstance(sortValue(row, sortBy), (int, float))]
  return {
    "units": "EQ units, Blender 1:1",
    "groups": {groupName: surveyFields.measuredGroups[groupName][0] for groupName in groupNames},
    "variantCount": len(rows),
    "unknownZones": unknownZones,
    "rows": rows[:limit] if limit is not None else rows,
  }


@guardedTool()
async def getZoneSurvey(context: Context, zone: str):
  """Everything surveyed for one zone, both lanes: every measured group of each of its variants, brought up to date, and its
  interpretation (of the variant importZone draws) with its state: "current"; "stale", with staleBecause naming the zone files changed
  and the procedure version raised since it was recorded (still returned, for what it is worth, and never current again until it is
  recorded anew with recordZoneInterpretation); or "none"."""
  clientRoot = zoneSources.resolveClientRoot()
  zoneName = zone.lower()
  surveys, unknownZones = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, [zoneName], list(surveyFields.measuredGroups), progressReporter(context))
  if unknownZones:
    raise ToolError(f"'{zoneName}' is not a zone in {clientRoot}")
  try:
    interpreted = await anyio.to_thread.run_sync(zoneInterpretation.interpretationState, clientRoot, toolingRoot, zoneSurveySkillPath, zoneName)
  except ValueError as error:
    raise ToolError(str(error)) from error
  return {"zone": zoneName, "brewallLabelCount": len(zoneSurvey.readBrewallLabels(clientRoot, zoneName)), "variants": surveys, "interpreted": interpreted}


@guardedTool()
async def recordZoneInterpretation(zone: str, interpretation: dict):
  """Keep Claude's interpretation of a zone, written by the zone-survey skill's interpretive procedure from screenshots and the measured
  groups: {zoneType (one of the skill's zone types), character {description, tags (camelCase terms)}, areas [{name, center [x, y, z],
  where, what, connections [{to (another area's name, or zone:<short name> for a zone line), by}]}], landmarks [{name, location
  [x, y, z], what, significance}], definingCharacteristics [statements], screenshots [{path (an absolute PNG from renderView,
  renderSketch, or renderSection), view (map, oblique, eyeLevel, or section), caption, shows [the areas and landmarks it shows]}],
  structuredValues (optional) {camelCaseName: {value, unit, how}}}, positions in the scene's coordinates. It needs a map view, an
  oblique and an eyeLevel view of every area, and an oblique or eyeLevel view of every landmark; a refusal lists every problem at once
  and keeps nothing. It is of the variant importZone draws, kept under the tooling root (survey/interpretations/<zone>) with copies of
  its screenshots, that variant's source files' SHA-256, and the procedure's version, replacing the zone's earlier one; getZoneSurvey
  returns it, stale once the files or the version change."""
  clientRoot = zoneSources.resolveClientRoot()
  try:
    return await anyio.to_thread.run_sync(zoneInterpretation.recordInterpretation, clientRoot, toolingRoot, zoneSurveySkillPath, zone.lower(), interpretation)
  except ValueError as error:
    raise ToolError(str(error)) from error


comparedMeasures = {
  "dimensions.footprint": "Ground area the terrain spans, square units.",
  "construction.terrainTriangles": "Triangles in the terrain.",
  "construction.terrainTrianglesPer10kSquareUnits": "Terrain triangle density: how finely the ground is modeled.",
  "construction.terrainTextures": "Distinct textures on the terrain.",
  "construction.terrainIslandTriangleShare": "Share of terrain triangles in material regions of one or two triangles: speckle rather than surfaced areas.",
  "construction.terrainSteepShare": "Share of terrain area steeper than 50 degrees: cliffs and walls modeled into the ground.",
  "construction.steepOnTerrainShare": "Share of all steep area that is terrain rather than placed models.",
  "construction.terrainPaintedShare": "Share of terrain area painted from palette maps or blended in the shader.",
  "construction.placedTriangles": "Triangles of the placed models, each counted at every placement (the zone's export gives the same total).",
  "content.placementCount": "Objects placed on the terrain.",
}
comparedFormats = ("wld", "eqgz", "eqtzp")


def sceneGeometry(collected):
  """The scene's triangles shaped as the zone survey's geometry, so surveyFields measures them as it measures a client zone."""
  arrays = numpy.load(collected["arrays"])
  triangles, isObject = arrays["triangles"], arrays["triangleIsObject"]
  terrainVertices = arrays["vertices"][numpy.unique(triangles[~isObject])]
  return {
    "vertices": arrays["vertices"], "triangles": triangles, "triangleTextures": arrays["triangleTextures"], "triangleIsObject": isObject,
    "triangleSurfaces": numpy.zeros(len(triangles), dtype=numpy.int8), "triangleUVAreas": numpy.full(len(triangles), numpy.nan),
    "trianglePainted": numpy.zeros(len(triangles), dtype=bool), "textureNames": collected["textureNames"],
    "terrainBounds": (terrainVertices.min(0), terrainVertices.max(0)), "tileShape": None, "format": "eqgz",
  }


def percentileRank(values, value):
  below = sum(1 for other in values if other < value)
  equal = sum(1 for other in values if other == value)
  return round(100 * (below + equal / 2) / len(values))


@guardedTool()
async def compareWithClientZones(context: Context, formats: list[str] = ["eqgz"], zones: list[str] | None = None):
  """Measure the open scene's zone as the zone survey measures the client's (its terrain collection as the terrain, every other rendered
  mesh and collection instance as placed on it, a placed building's nested pieces included) and place each measure among the client's
  zones of the given formats (wld, eqgz, eqtzp; EQG zones by default, the 2011-era target), or among the named `zones` (such as the
  references a zone is modeled on): the zone's value, the client zones' 10th, 25th, 50th, 75th, and 90th percentiles, and the zone's
  percentile among them. `construction.placedTriangles` sets the triangles of every placement of every placed model (kit pieces,
  buildings, spans, wall sections; the same total checkExport gives) beside the client's (classic zones' placed objects are not
  measured). Use it after each pass to steer by how the client's own zones are built rather than by taste alone."""
  unknown = sorted(set(formats) - set(comparedFormats))
  if unknown or not formats:
    raise ToolError(f"formats are among {list(comparedFormats)}, got {formats}")
  collected = await callBridge(context, "collectConstruction", {"outputPath": str(toolingRoot / "review" / "construction.npz")})
  geometry = sceneGeometry(collected)
  frames = surveyFields.triangleFrames(geometry)
  ours = {"dimensions": surveyFields.measureDimensions(geometry, frames), "construction": surveyFields.measureConstruction(geometry, frames), "content": {"placementCount": collected["placements"]}}
  clientRoot = zoneSources.resolveClientRoot()
  zoneNames = [zone.lower() for zone in zones] if zones is not None else None
  try:
    rows, unknownZones = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, zoneNames, ["dimensions", "content", "construction"], progressReporter(context))
  except ValueError as error:
    raise ToolError(str(error)) from error
  if unknownZones:
    raise ToolError(f"Not zones in {clientRoot}: {unknownZones}")
  clientRows = [row for row in rows.values() if row["format"] in formats and "error" not in row]
  measures = {}
  for measure, meaning in comparedMeasures.items():
    group, field = measure.split(".")
    values = sorted(row[group][field] for row in clientRows if row[group].get(field) is not None)
    value = ours[group][field]
    measures[measure] = {"zone": value, "clientZones": len(values), "meaning": meaning} | ({
      label: values[min(len(values) - 1, int(len(values) * share))] for label, share in (("p10", 0.1), ("p25", 0.25), ("median", 0.5), ("p75", 0.75), ("p90", 0.9))
    } | {"percentile": percentileRank(values, value) if value is not None else None} if values else {})
  return {"formats": formats, "clientZones": sorted({row["zone"] for row in clientRows}) if zones is not None else len(clientRows), "textures": collected["textureNames"], "measures": measures}


@guardedTool()
async def getZoneNotes(context: Context, zone: str, text: str | None = None):
  """Brewall map labels for a zone: place names for design notes, never geometry or scale. Those on the zone (within the plan extent
  of the variant importZone draws, as the survey's dimensions give it) come by map layer, each with the scene position it roughly marks
  (scene x, y = map -y, -x) in whole units; those off it (a map's legend and credits, at made-up positions beside the zone) are counted
  and named. text keeps only the labels holding each of its words as a word of their own, case aside: "to" finds the zone lines'
  destinations ("to Blightfire Moors")."""
  clientRoot = zoneSources.resolveClientRoot()
  zoneName = zone.lower()
  if not zoneSurvey.brewallMapPaths(clientRoot, zoneName):
    raise ToolError(f"No Brewall map files for zone '{zone}' in {clientRoot / 'maps' / 'Brewall'}")
  try:
    variant = eqZones.drawnVariant(clientRoot, zoneName)[0]
  except ValueError as error:
    raise ToolError(f"Zone '{zone}' has Brewall maps, but its labels cannot be placed against the zone: {error}") from error
  surveys, _ = await anyio.to_thread.run_sync(zoneSurvey.surveyMeasured, clientRoot, toolingRoot, [zoneName], ["dimensions"], progressReporter(context))
  if "error" in surveys[variant]:
    raise ToolError(f"Zone '{zone}' ({variant}) cannot be measured, so its labels cannot be placed against it: {surveys[variant]['error']}")
  dimensions = surveys[variant]["dimensions"]
  if dimensions["terrainMinimum"] is None:
    raise ToolError(f"Zone '{zone}' ({variant}) has no terrain whose extent its labels could be placed against")
  minimum, maximum = dimensions["terrainMinimum"][:2], dimensions["terrainMaximum"][:2]
  return {"zone": zoneName, "variant": variant, "extent": {"minimum": minimum, "maximum": maximum}} | zoneSurvey.zoneNotes(zoneSurvey.readBrewallLabels(clientRoot, zoneName), minimum, maximum, text)


catalog = assetCatalog.AssetCatalog(toolingRoot, serverDirectory.parent / "catalog")
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


def callReportingFailures(function, *arguments):
  try:
    return function(*arguments)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error


def sourceSummary(key, surveyed):
  """Counts per asset kind for a surveyed source, its problems, and its most-used textures."""
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


@guardedTool()
async def surveyAssets(
  context: Context, zones: list[str] | None = None, allZones: bool = False, folder: str | None = None, path: str | None = None, refresh: bool = False,
):
  """Survey graphical assets into the asset catalog's measured lane: client zones (`zones`, or `allZones`; stale ones are surveyed in
  parallel, and a zone the parsers cannot read is reported with why), each with the textures of the archives it loads
  for its own geometry and objects, with how the zone uses each: area share, world units per texture repeat, slopes, shaders, paired
  textures; its placed models; its light styles; its emitters; an EQ terrain zone's ecosystems), a client folder of loose images
  (Resources/Sky, Resources/WaterSwap, Resources/Precipitation, EnvEmitterEffects), or an EQG zone archive at an absolute `path`, such as
  one exportZone wrote. Each texture is written out readable, with a thumbnail, under the tooling root; a client source's measurements
  are kept in the repository's catalog folder to be committed. Cached until the source's files change. For one source, returns its counts
  and most-used textures; for many zones, what became of each. findAssets, viewTextures, viewModels, and getAsset read the rest, and
  describeAssets writes what they are."""
  if sum((zones is not None, allZones, folder is not None, path is not None)) != 1:
    raise ToolError("Survey zones, allZones, a folder, or a path")
  clientRoot = zoneSources.resolveClientRoot()
  if zones is not None or allZones:
    zoneNames = sorted({variant["zone"] for variant in zoneSources.discoverZones(clientRoot).values()}) if allZones else [zone.lower() for zone in zones]
    outcomes = await anyio.to_thread.run_sync(callReportingFailures, catalog.surveyZones, clientRoot, toolingRoot / "models", zoneNames, refresh, progressReporter(context))
    if len(zoneNames) == 1 and "error" not in outcomes[zoneNames[0]]:
      key = f"zone:{zoneNames[0]}"
      return sourceSummary(key, json.loads(catalog.sourcePath(key).read_text(encoding="utf-8")))
    return {
      "zones": len(outcomes), "surveyed": sum("surveyed" in outcome for outcome in outcomes.values()), "current": sum("current" in outcome for outcome in outcomes.values()),
      "errors": {zoneName: outcome["error"] for zoneName, outcome in outcomes.items() if "error" in outcome},
    }
  if folder is not None:
    key, sourcePaths = f"folder:{folder}", callReportingFailures(assetSurvey.looseFolderPaths, clientRoot, folder)
    surveyFunction = lambda: assetSurvey.surveyLooseFolder(clientRoot, catalog.cacheRoot, folder)
  else:
    archivePath = Path(path)
    if not archivePath.is_absolute() or archivePath.suffix.lower() != ".eqg" or not archivePath.is_file():
      raise ToolError(f"'{path}' is not an absolute path to an existing .eqg file")
    key, sourcePaths = f"file:{archivePath}", assetSurvey.zoneFileSourcePaths(archivePath)
    surveyFunction = lambda: assetSurvey.surveyZoneFile(clientRoot, catalog.cacheRoot, archivePath)
  reportProgress = progressReporter(context)
  await anyio.to_thread.run_sync(reportProgress, 0, 1, f"surveying {key}")
  surveyed = await anyio.to_thread.run_sync(callReportingFailures, catalog.survey, surveyFunction, key, sourcePaths, refresh)
  await anyio.to_thread.run_sync(reportProgress, 1, 1, f"surveyed {key}")
  return sourceSummary(key, surveyed)


@guardedTool(description="Search the asset catalog: compact entries (measured facts and descriptions) for the assets that match." + findHelp)
def findAssets(
  kind: str | None = None, text: str | None = None, categories: list[str] | None = None, tags: dict | None = None, source: str | None = None,
  described: bool | None = None, colors: list[str] | None = None, minimumSide: int | None = None, tiles: bool | None = None,
  usedOn: list[str] | None = None, sortBy: str = "relevance", limit: int = 40,
):
  return callReportingFailures(catalog.find, kind, text, categories, tags, source, described, sortBy, limit, colors, minimumSide, tiles, usedOn)


@guardedTool()
def getAsset(id: str):
  """Everything the catalog holds for one asset: its measured facts, what differs in each source that holds it, and its description."""
  return callReportingFailures(catalog.entry, id)


@guardedTool()
def getAssetVocabulary():
  """The words the catalog describes assets with: asset kinds, each kind's categories, and the tag groups with their terms, each with
  its meaning. Descriptions must use these; extendAssetVocabulary adds a term when none fits."""
  return catalog.vocabulary()


@guardedTool()
def extendAssetVocabulary(group: str, term: str, meaning: str):
  """Add a term to the vocabulary when no existing one fits: group is a tag group's name or category:<kind>; term is one camelCase word."""
  return callReportingFailures(catalog.extendVocabulary, group, term, meaning)


@guardedTool()
def describeAssets(descriptions: list[dict]):
  """Write what assets are, all or none: [{id, category, tags {group: [terms]}, description (what it shows and how it reads), usage
  (where and how to use it: surfaces, scale, pairings, what to avoid), worldUnitsPerRepeat (textures: the repeat that reads right; the
  measured unitsPerRepeat is how the source zone used it), pairsWith (ids of assets it goes with: its normal map, transitions)}], in
  getAssetVocabulary's words. Each replaces its asset's earlier description."""
  return {"described": callReportingFailures(catalog.describe, descriptions)}


def sheetEntries(ids, kind, text, categories, tags, source, described, sortBy, limit, measuredFilters=(None, None, None, None)):
  if ids is not None and any(value is not None for value in (text, categories, tags, source, described, *measuredFilters)):
    raise ToolError("Give ids or filters, not both")
  if ids is not None:
    return [callReportingFailures(catalog.requireAsset, assetID) for assetID in ids]
  found = callReportingFailures(catalog.find, kind, text, categories, tags, source, described, sortBy, limit, *measuredFilters)["assets"]
  return [catalog.assets()[entry["id"]] for entry in found]


def writeSheetImage(cells, columns, legend, cellSide):
  outputPath = newRenderPath().with_suffix(".jpg")
  size = callReportingFailures(assetSheets.writeSheet, cells, columns, outputPath, cellSide)
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


async def openWorkFile(context, path, discardUnsavedChanges):
  """Open a .blend and draw its water's environments: each water material's lookup image, which is not part of the work file, made
  again under this machine's tooling root from the material's cube map where it is missing, and the material pointed at it. Water whose
  cube map file is missing mirrors nothing and is listed (environmentMapsMissing)."""
  opened = await callBridge(context, "openFile", {"path": path, "discardUnsavedChanges": discardUnsavedChanges})
  lookups, missing = {}, []
  for entry in (await callBridge(context, "environmentLookups", {}))["lookups"]:
    if not os.path.isfile(entry["cubeMap"]):
      missing.append({"material": entry["material"], "cubeMap": entry["cubeMap"]})
      continue
    present = os.path.isfile(entry["lookup"])
    expected = environmentLookupPath(entry["cubeMap"])
    if not (present and os.path.normcase(entry["lookup"]) == os.path.normcase(os.path.normpath(expected))):
      lookups[entry["material"]] = expected
  if lookups:
    await callBridge(context, "restoreEnvironmentLookups", {"lookups": lookups})
    opened = (await callBridge(context, "getStatus", {})) | {"environmentLookupsMade": sorted(lookups)}
  return opened | ({"environmentMapsMissing": missing} if missing else {})


@guardedTool()
async def openFile(context: Context, path: str, discardUnsavedChanges: bool = False):
  """Open a .blend by absolute path. Refuses when the open file has unsaved changes unless they are explicitly discarded. A water
  material's environment lookup, kept under the tooling root rather than in the work file, is made again from its cube map when missing
  (environmentLookupsMade); a cube map whose file is missing is listed (environmentMapsMissing), and that water mirrors nothing."""
  return await openWorkFile(context, path, discardUnsavedChanges)


@guardedTool()
async def saveFile(context: Context, path: str | None = None, replaceExisting: bool = False):
  """Save the open file, or save it as an absolute path; a path holding another file is written over only with replaceExisting. Textures and libraries become relative paths; packed or generated images are refused."""
  return await callBridge(context, "saveFile", {"path": path, "replaceExisting": replaceExisting})


async def openWorkFilePath(context, toolName):
  """The open work file's path; a scene never saved has no work file to keep checkpoints of."""
  status = await callBridge(context, "getStatus", {})
  if status["filePath"] is None:
    raise ToolError(f"{toolName} works on the open work file, and no work file is open: open it (openFile), or save this scene as a new work file (saveFile with a path)")
  return status["filePath"]


async def checkpointOpenFile(context, label):
  callReportingFailures(checkpoints.validateLabel, label)
  workFile = await openWorkFilePath(context, "saveCheckpoint")
  await callBridge(context, "saveFile", {"path": None, "replaceExisting": False})
  return await anyio.to_thread.run_sync(callReportingFailures, checkpoints.saveCheckpoint, toolingRoot, workFile, label)


@guardedTool()
async def saveCheckpoint(context: Context, label: str):
  """Save the open work file, then keep a copy of it as a recovery point under the tooling root (never beside the work file), named for the UTC time and `label`. Refuses a scene never saved or one that cannot be saved. For recovering from a wreck, not for iterating: passes, layers, and the region tools take work back."""
  return await checkpointOpenFile(context, label)


@guardedTool()
async def listCheckpoints(context: Context, path: str | None = None):
  """The checkpoints of the open work file, or of the work file at `path` (absolute; it need not exist now): name, label, UTC time, and size, and their total size."""
  workFile = path if path is not None else await openWorkFilePath(context, "listCheckpoints")
  return await anyio.to_thread.run_sync(callReportingFailures, checkpoints.listCheckpoints, toolingRoot, workFile)


@guardedTool()
async def restoreCheckpoint(context: Context, name: str, discardUnsavedChanges: bool = False):
  """Put a checkpoint back over the work file it was saved from, then open that file. The open file must be that work file, or nothing (as after a reconnect or a crash). Its unsaved changes are saved first unless discardUnsavedChanges; then the work file as it stands on disk is kept as a checkpoint labelled "before restore <time of the checkpoint restored>" (restore that to take the restore back). The copy goes in under a temporary name, so a failed restore leaves the work file as it was."""
  checkpointPath, workFile = callReportingFailures(checkpoints.findCheckpoint, toolingRoot, name)
  status = await callBridge(context, "getStatus", {})
  if status["filePath"] is not None and not checkpoints.samePath(status["filePath"], workFile):
    raise ToolError(f"Checkpoint '{name}' was saved from '{workFile}', but the open file is '{status['filePath']}'; a checkpoint is restored only over its own work file, whose textures are on paths relative to it: open '{workFile}' first")
  if status["unsavedChanges"] and not discardUnsavedChanges:
    if status["filePath"] is None:
      raise ToolError("The open scene was never saved and has changes the restore would drop: save it as a new work file (saveFile with a path), or pass discardUnsavedChanges true")
    try:
      await callBridge(context, "saveFile", {"path": None, "replaceExisting": False})
    except ToolError as error:
      raise ToolError(f"restoreCheckpoint saves the open work file's changes before keeping it, and saving failed; pass discardUnsavedChanges true to give those changes up.\n\n{error}") from error
  stamp = callReportingFailures(checkpoints.checkpointNameParts, checkpointPath)[0]
  beforeRestore = None
  if os.path.isfile(workFile):
    beforeRestore = await anyio.to_thread.run_sync(callReportingFailures, checkpoints.saveCheckpoint, toolingRoot, workFile, f"before restore {stamp}")
  await anyio.to_thread.run_sync(callReportingFailures, checkpoints.restoreOver, checkpointPath, workFile)
  reopened = await openWorkFile(context, workFile, True)
  return reopened | {"restored": name, "beforeRestore": beforeRestore["name"] if beforeRestore is not None else None}


@guardedTool()
def deleteCheckpoints(names: list[str]):
  """Delete the named checkpoints, and only those; none is ever deleted otherwise. Refuses, deleting none, when any name is unknown."""
  return callReportingFailures(checkpoints.deleteCheckpoints, toolingRoot, names)


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
  fogOn: bool | None = None,
  minClip: float | None = None,
  maxClip: float | None = None,
  newEngineZone: bool | None = None,
  sky: dict | str | None = None,
  safePoint: list[float] | None = None,
  underworld: float | None = None,
  shortName: str | None = None,
):
  """Set the zone's EQ properties stored in the .blend, in the client's lighting terms (docs/clientRendering.md): ambient, special
  ambient, bounce, and sun colors (0-1, raw as the client uses them); the direction toward the sun (azimuth 0 = +Y, clockwise;
  elevation -90 to 90); fog color, start, end, and density (the client's default is 0.33); fogOn, whether the client fogs at all (the
  zone row's ztype is not 0; Highpass Hold's is 0, so it draws no fog); minClip and maxClip, the zone row's clip distances (the far
  clip runs from the minimum to the maximum with the player's clip slider; previews draw it at the maximum, and a fog end that
  reaches it is pulled in to 15% short of the way from the fog start, as the client does); newEngineZone,
  the zone header's NewEngineZone, which sets the scale the client draws spawns at (the live dumps' zoneHeaders give it per zone;
  EQEmu sends false for every zone); and sky, the client sky the zone draws: {type, weather, hour, minute}. type is the sky type the
  client looks up, the zone's short name unless the server overrides it; a type sky.ini lacks gets the client's 'default' sky, as
  every zone without its own does. weather defaults to the type's DefaultWeather. hour and minute place the sun, at its highest at
  12:00 (a midday screenshot after the server's '#set time 12' matches 13:00). Previews draw the sky behind the zone and take the
  ambient, bounce, sun color and direction, and fog color from it, so those cannot be set by hand while it is set, and setting it
  drops any set before; sky "none" states that the zone draws no sky (its zone row's sky 0, as about a third of the client's EQG
  zones have), so previews show the fog color where nothing is drawn and the light and fog color are set by hand. safePoint [x, y, z, headingDegrees] is where players arrive in the zone (the zone
  row's safe point; heading 0 = +Y, clockwise) and underworld the height below it under which the client puts a falling player back;
  a game export needs both, with ground the zone ships under the safe point above the underworld. shortName is the zone's short name
  (1 to 31 lowercase letters and digits): a zone line whose target is it leads back into this zone, a teleport whose landing
  getEntries lists. The result gives how the client resolves the sky and the light it supplies."""
  updates = {
    "ambientColor": ambientColor, "specialAmbientColor": specialAmbientColor, "bounceColor": bounceColor, "sunColor": sunColor,
    "sunAzimuthDegrees": sunAzimuthDegrees, "sunElevationDegrees": sunElevationDegrees, "fogColor": fogColor, "fogStart": fogStart,
    "fogEnd": fogEnd, "fogDensity": fogDensity, "fogOn": fogOn, "minClip": minClip, "maxClip": maxClip, "newEngineZone": newEngineZone, "sky": sky,
    "safePoint": safePoint, "underworld": underworld, "shortName": shortName,
  }
  given = {key: value for key, value in updates.items() if value is not None}
  if not given:
    raise ToolError(f"setZoneProperties needs at least one of {list(updates)}")
  if isinstance(sky, str) and sky != skyDrawing.noSky:
    raise ToolError(f"sky is {{type, weather, hour, minute}} or \"{skyDrawing.noSky}\", got '{sky}'")
  stored = await callBridge(context, "setZoneProperties", {"updates": given})
  resolved = await zoneSky(stored["zone"])
  return stored | {"sky": None if resolved is None else {key: resolved[key] for key in ("chain", "dayFraction", "lightFrom", "environment")}}


async def zoneFigureModel(zone):
  """The scale figure's model as the zone draws her."""
  # Without newEngineZone the preview's own check fails, naming it with any other missing zone property.
  if "newEngineZone" not in zone:
    return None
  figure = await anyio.to_thread.run_sync(spawnModel, None, figureModelCode, figureHeight, bool(zone["newEngineZone"]))
  return {key: figure[key] for key in ("folder", "scale", "avatarHeight")}


async def scaleFigureModel(zone, view):
  """The scale figure's model for a view that may stand her (standAt, or a review camera saved from such a view), else None."""
  return await zoneFigureModel(zone) if {"standAt", "camera"} & set(view) else None


@guardedTool()
async def renderView(
  context: Context, view: dict, shading: str = "client", bandHeight: float = 50.0, guides: bool = True, swimVolumes: bool = False, labels: list[str] | None = None,
  liquidTime: float | None = None,
):
  """Render the EQ preview of a view: {"camera": name} (a review camera saved from a standAt view stands the scale figure again where she stood, her ground found as figureAt's is; one matched to concept art renders at the art's aspect and its own field of view), {"eye": [x,y,z], "target": [x,y,z]}, or {"standAt": [x,y] or [x,y,z], "headingDegrees": h, "pitchDegrees": p} (on the highest ground players stand on at [x,y], never what they pass through (as walkRoute), or with z on the ground found from 3 above z down to 50 below it, for caves, under overhangs, and on ledges; heading 0 = +Y, clockwise; eye 5.5 above the ground, or, where water stands over that, a unit over the water's surface, swimming; adds a dark elf female of height 5, the race default, drawn as the client draws her in the zone (newEngineZone), walked ahead along the ground and facing the camera, or stood by hand facing the camera with "figureAt": [x,y] or [x,y,z] in the view, its ground found as standAt's is, on a ledge or ramp too narrow to walk her ahead on), or {"map": {"center": [x,y], "width": w}}: the layout from straight above, orthographic, the game's north (+X) up and east (-Y) right as the in-game map draws, `width` units across (along y), without fog. {"frame": {"objects": [names], "headingDegrees": h, "pitchDegrees": p}} looks at the named meshes, collection instances, or structures (all their parts) from that heading and pitch, standing back so they fit (its result's eye and target reproduce that camera). shading "client" draws the zone as the client does, its point lights and particle emitters with it (the result's pointLights counts the lights and the objects they light, and emitters the emitters drawn, their particles, and those not drawn, grouped by why); "relief" is layout's drawing in quiet greys (the base renderSketch draws plans over); "layout" draws every surface unlit in a color for its height (green low through tan and brown to white high, across the scene's height range given in the result) in bands `bandHeight` units tall whose edges read as contours, darker facing away from a light in the northwest, without fog and out to the whole scene: for judging shape and layout; "coverage" draws only what exportZone would export, each face in the color of its export check status (checkExport): black where it cannot export, red for zero texture area, brown for a blockout material, yellow for texture stretched or squeezed, orange where the base material shows, magenta along a ground border without a transition strip, grey when fine, and blue wherever a face is seen from its back, lit from the northwest as layout is, softer so no shaded face reads as black, and without fog; the result counts the exported faces by status. The value shadings draw every mesh (each part of a collection instance as placed, and the scale figure) from a value of its own, lit softly from the northwest, without fog, the result giving the scale: "objects" draws each object in its own flat color, the ones the view shows most of first (blue, orange, green, red, purple, yellow, cyan, magenta, lime, pink, teal, lavender, brown, olive, then grey for the rest), with a legend of the objects the view shows, each with its color and share of the view; "curvature" draws convex forms warm (orange), concave cool (blue), and flat neutral grey, a ridge or trough curved to a radius of 16 at half color and sharper ones fuller, from the bend of the edges around each vertex (so where two meshes meet without sharing edges, as a rock sunk into the ground, there is none); "triangleDensity" draws each face's triangles per 10,000 square units of its own area over fixed decades, blue 1, cyan 10, green 100, yellow 1,000, red 10,000 (the client's EQG terrains run from 8 to 5,083, 244 at the median), with the range the view shows; "texelDensity" draws each face's texture pixels per world unit (its diffuse texture's pixels over the area its texture coordinates spread them across) blue lowest through cyan, green, and yellow to red highest across the range the view shows (the result gives it and each color's value), dark grey where a face has no diffuse texture or texture coordinates: coverage's stretch check compares a face with its own material's usual scale, texelDensity compares materials with each other. labels [names] writes each named object's (or structure's, all its parts together) name on the view by its place (where its middle projects when the object shows there, else the middle of what shows of it), marked with a white dot, but only for the objects the view shows; the result lists the places and the named objects it does not show (an object hidden from renders, a guide with guides off, or one that is not a mesh or collection instance is refused). Guides (plot outlines, sketch massing) draw unless guides is false, and with them, in every shading, the view is tinted red where the boundaries (walls, lids, floors) stand, which the client never draws, a wall as a slab thick enough to show from above, and green where the zone lines stand, seen through the water but hidden behind and under the ground; with swimVolumes, the view is tinted where the swim volumes stand (cyan water, magenta lava), each box seen through the water, so its top shows evenly under a surface it meets or lies just below, but hidden behind and under the ground. Liquids draw as they stand at effect time 0, or at `liquidTime` seconds on the client's effect clock, each layer scrolled by its slides as the client's effects scroll it (time modulo 100): two views a second or two apart show which way and how fast a fall or river moves (liquid materials made before previews scrolled are refused, to be made again); emitters draw at the same moment of their steady state either way."""
  outputPath = newRenderPath()
  zone = await callBridge(context, "getZoneProperties", {})
  description = await callBridge(context, "renderView", {
    "view": view, "outputPath": str(outputPath), "figureModel": await scaleFigureModel(zone, view), "shading": shading, "bandHeight": bandHeight,
    "guides": guides, "sky": await zoneSky(zone), "swimVolumes": swimVolumes, "labels": labels, "emitters": await previewEmitterAssets(),
    "liquidTime": liquidTime,
  })
  if labels is not None:
    await anyio.to_thread.run_sync(viewSheets.writeNames, outputPath, description["labels"]["shown"])
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
  placed = await placeEQModel(context, spawn["folder"], name, frameLocation, rotation, spawn["scale"], spawn["avatarHeight"], snapToGround, collection, spawn["details"], "spawn")
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
  return await placeEQModel(context, folder, name, frameLocation, rotation, scaleFactor / 100, 0, False, collection, details, "door")


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
  return await placeEQModel(context, folder, name, frameLocation, rotation, scale, 0, False, collection, details, "object")


def zoneModel(zone):
  try:
    return eqZones.buildZone(zoneSources.resolveClientRoot(), toolingRoot / "models", zone)
  except ValueError as error:
    raise ToolError(str(error)) from error


async def placeZone(context, zone, collection):
  folder, details = await anyio.to_thread.run_sync(zoneModel, zone)
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": zone, "location": [0, 0, 0], "rotationDegrees": 0, "scale": 1, "avatarHeight": 0,
    "snapToGround": False, "collection": collection, "clientContent": "zone",
  })
  stampKeys = ("zoneCacheFormat", "modelCacheFormat", "indexFormat", "archives", "listingFingerprint", "zone", "textureSources", "lit", "minimum", "maximum")
  return placed | {"source": {key: value for key, value in details.items() if key not in stampKeys}}


def bridgeLights(lights):
  return [{"name": light["name"], "position": list(light["position"]), "color": list(light["color"]), "radius": light["radius"]} for light in lights]


def bridgeEmitters(emitters):
  """Emitters for the bridge; a few client lists leave a name empty, and a Blender object needs one, so those are named for their definition."""
  return [{key: list(value) if key == "position" else value for key, value in emitter.items()} | {"name": emitter["name"] or f"emitter{emitter['definition']}"} for emitter in emitters]


async def placeZoneEnvironment(context, zone, lights, emitters, clientContent):
  """A placed zone's lights and emitters, each set in its own collection named for the zone; None for what is not read."""
  placed = {"lights": None if lights is None else 0, "emitters": 0}
  if lights:
    placed["lights"] = (await callBridge(context, "placeLights", {"lights": bridgeLights(lights), "collection": f"{zone} lights", "clientContent": clientContent}))["lights"]
  if emitters:
    placed["emitters"] = (await callBridge(context, "placeEmitters", {"emitters": bridgeEmitters(emitters), "collection": f"{zone} emitters", "clientContent": clientContent}))["emitters"]
  return placed


async def placeZoneLineGuides(context, zone, found, clientContent):
  """A placed zone's zone lines (eqZones.zoneLineBoxes) as guides in "<zone> zone lines", and the tilted ones listed apart."""
  placed = await callBridge(context, "placeZoneLineGuides", {
    "zoneLines": found["zoneLines"], "collection": f"{zone} zone lines", "clientContent": clientContent,
  }) if found["zoneLines"] else {"zoneLines": []}
  return placed | {"zoneLinesTilted": found["zoneLinesTilted"]}


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
  empties in "<zone> emitters", as placeLights and placeEmitters make them. An EQG zone's zone-line regions come in as zone-line guides in
  "<zone> zone lines", as placeZoneLine makes them, named as the zone file names them (the number the client reads from the name) and
  turned about Z as it turns them, with no target (the zone file never says where one leads; the server's zone points do); getZoneLines
  lists them and plans and views draw them. Those with a tilt field set, whose reading is untraced, are listed in zoneLinesTilted, not
  placed. A classic or EQ terrain zone's zone lines are not read (zoneLines None). Water whose environment map is not a DDS cube map,
  which the client cannot load and so draws without a reflection, is drawn so and its maps listed in source.environmentMapsNotCube."""
  placed = await placeZone(context, zone, collection)
  clientRoot = zoneSources.resolveClientRoot()
  try:
    lights = await anyio.to_thread.run_sync(eqZones.zoneLights, clientRoot, zone)
    emitters = readEmitterList(eqEmitters.emitterListPath(clientRoot, zone))
    zoneLines = await anyio.to_thread.run_sync(eqZones.zoneLines, clientRoot, zone)
  except ValueError as error:
    raise ToolError(str(error)) from error
  environment = await placeZoneEnvironment(context, zone, lights, emitters, "zone")
  return placed | environment | (await placeZoneLineGuides(context, zone, zoneLines, "zone") if zoneLines is not None else {"zoneLines": None, "zoneLinesTilted": None})


zoneFileSourceKeys = ("zoneCacheFormat", "modelCacheFormat", "sha256", "textureSources", "lit", "minimum", "maximum")


@guardedTool()
async def importZoneFile(context: Context, path: str, collection: str | None = None):
  """Bring an EQG zone archive outside the client, such as one exportZone wrote, into the open scene as one object named for its zone
  (the file name), drawn as importZone draws the client's EQG zones: its terrain and placed models, with baked light where its count
  fits each model, and the triangles it lets players through marked so players pass through them wherever ground is looked for; its lights and the emitters of the
  <zone>_EnvironmentEmitters.txt beside it, as importZone brings them; and its player boundaries as reference: the terrain's invisible
  walls as one boundary in "<zone> boundaries" and its ATP_ regions as zone-line guides in "<zone> zone lines", as importZone brings a
  client zone's (no targets; tilted ones listed in zoneLinesTilted, not placed). The result counts each model's passable triangles."""
  archivePath = Path(path)
  if not archivePath.is_absolute() or archivePath.suffix.lower() != ".eqg" or not archivePath.is_file():
    raise ToolError(f"'{path}' is not an absolute path to an existing .eqg file")
  try:
    folder, details = await anyio.to_thread.run_sync(eqZones.buildZoneFile, toolingRoot / "models", archivePath)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  placed = await callBridge(context, "placeModel", {
    "modelFolder": str(folder), "name": archivePath.stem.lower(), "location": [0, 0, 0], "rotationDegrees": 0, "scale": 1, "avatarHeight": 0,
    "snapToGround": False, "collection": collection, "clientContent": "zoneFile",
  })
  try:
    lights = await anyio.to_thread.run_sync(eqZones.zoneFileLights, archivePath)
  except ValueError as error:
    raise ToolError(str(error)) from error
  emitters = readEmitterList(archivePath.parent / f"{archivePath.stem}_EnvironmentEmitters.txt")
  environment = await placeZoneEnvironment(context, archivePath.stem.lower(), lights, emitters, "zoneFile")
  try:
    boundaries = await anyio.to_thread.run_sync(eqZones.zoneFileBoundaries, archivePath)
  except ValueError as error:
    raise ToolError(str(error)) from error
  zone = archivePath.stem.lower()
  walls = {"boundary": None} if boundaries["walls"] is None else await callBridge(context, "placeImportedBoundaries", {"zone": zone, "walls": boundaries["walls"]})
  return placed | {"source": {key: value for key, value in details.items() if key not in zoneFileSourceKeys}} | environment | walls | await placeZoneLineGuides(
    context, zone, boundaries, "zoneFile",
  ) | {"passableTriangles": boundaries["passableTriangles"]}


def groupedExclusions(excluded, shownPerReason=25):
  """Left-out objects by reason, each reason with its count and up to shownPerReason of its objects."""
  groups = {}
  for entry in excluded:
    groups.setdefault(entry["reason"], []).append(entry["object"])
  return [{"reason": reason, "count": len(names), "objects": names[:shownPerReason]} for reason, names in sorted(groups.items())]


exportChecksHelp = (
  " purpose is \"test\" (quick renders and test loads) or \"game\" (files for the EQ client and server). Both refuse the hard errors:"
  " the file unsaved; objects export cannot take (a non-mesh in the terrain collection, or among a placed collection's members, to any"
  " depth, anything but meshes and collection instances; a placed model scaled unevenly; model names that collide); a placed kit piece or"
  " building, or a structure, whose kit file or collection cannot be found; two materials that would share one name in the archive (a"
  " kit's material and the zone's own of one name export apart, the kit's as <name>_<kit file stem>);"
  " faces with no material or a material createMaterial or createLiquidMaterial did not"
  " make; a missing, packed, generated, or other-drive image, a non-DDS image whose sides are not powers of two, DDS data under another"
  " extension, two images that would share one DDS name, a cutout with a normal map; meshes without texture coordinates; a liquid"
  " material on anything but a water body; a mesh with surfacing layers whose modifiers change its faces; swim volumes a zone file"
  " cannot hold (as getSwimVolumes lists them); boundaries that are not meshes or have no faces; zone lines a zone file cannot hold (a name not ATP_<number>_<label>,"
  " a turned box, a box without size, region names that clash once lowercased); housing the server would refuse. A game export also"
  " refuses blockout materials (createMaterial blockout) on exported faces, pools and rivers whose swimming is undecided or changed"
  " since their boxes were accepted, missing view values (fogOn, minClip, maxClip, sky or sky \"none\" stated, and the fog's start, end, and density"
  " when it is on), a missing safe point or underworld, a safe point over no ground the zone ships above the underworld (an imported"
  " reference zone under it is none), zone lines without a target or sharing a number, a zone without regions, regions whose access is"
  " undecided (getRegions), a stored entry off its footing (getEntries), structures laid on ground or a kit that has changed since (stale: editStructure lays them again), and, until reach mapping"
  " exists, any zone: containment cannot be checked yet. A test export lists all of these but"
  " containment as findings. Findings, never refusals, for both: texture coverage, each with where it lies: the base material showing where no unmuted"
  " surfacing layer covers a face (a cave's lining, in its own materials, is not ground under the base); ground borders on the terrain where two ground materials meet, walkable ground on at least one side,"
  " with no paintTransition strip between them, as a length per border and its stretches (each a center, bounds, and length; a cave's"
  " lining meeting the ground is no border); texture stretched or squeezed (a texel lying over 2x longer one way than the other"
  " in the world, or world units per repeat over 2x off, either way, the material's area-weighted median, `usualRepeat`); zero texture"
  " area; and back faces (faces wound against the rest of their surface: a closed surface faces out, a terrain sheet up)."
  " Each failure and finding names the object (and the placed objects that place it), the material, the image, and the face count, with"
  " the faces' connected pieces (center and face count, largest first); renderView shading \"coverage\" draws the same statuses."
  " `coverage` counts the exported faces by status; `excluded` lists what is not the zone's own geometry by reason (guides, plot borders,"
  " regions, entries, anything hidden from renders, placed client content); `toConfirm` lists shipped meshes with shaping passes off or surfacing"
  " layers muted, which leave the zone as if never made, and caves and defined passes whose ground moved since they were made"
  " (stale; a game export refuses them, and caves broken by a change outside their guards refuse both), and stale structures with why"
  " (ground, kit, missing); `swim` the undecided and changed pools and rivers; `boundaries` and `zoneLines`"
  " what goes into the terrain as invisible walls and into the .zon as zone lines; `structures` each structure's models (file,"
  " triangles, placements) and triangles (one laid as ground has its triangles in the terrain and no model), and `placedTriangles`"
  " the triangles of every placement of every placed model, which compareWithClientZones sets beside the client's zones."
)


def exportTarget(path):
  archivePath = Path(path)
  if not archivePath.is_absolute() or archivePath.suffix != ".eqg" or not archivePath.parent.is_dir():
    raise ToolError(f"'{path}' is not an absolute .eqg path in an existing folder")
  zone = archivePath.stem
  if not eqgExport.zoneNamePattern.match(zone):
    raise ToolError(f"Zone name '{zone}' must be lowercase letters and digits, as the client's zone short names are")
  return archivePath, zone


def replaceExportFiles(archivePath, archiveBytes, sideContents):
  """Put an export's files in the last export's places: each written whole under a temporary name, then the side files (removed where
  the content is None) and the archive last. A failure at any point puts every file back as it was and leaves no temporary one."""
  contents = sideContents | {archivePath: archiveBytes}
  temporaries = {target: target.with_name(target.name + ".partial") for target, content in contents.items() if content is not None}
  previous, placed = {}, []
  try:
    for target, temporary in temporaries.items():
      temporary.write_bytes(contents[target])
    for target in sideContents:
      if target.exists():
        previous[target] = target.with_name(target.name + ".previous")
        target.replace(previous[target])
    for target in sideContents:
      if target in temporaries:
        temporaries[target].replace(target)
        placed.append(target)
    temporaries[archivePath].replace(archivePath)
  except OSError:
    for target in placed:
      target.unlink()
    for target, kept in previous.items():
      kept.replace(target)
    raise
  finally:
    for temporary in temporaries.values():
      temporary.unlink(missing_ok=True)
  for kept in previous.values():
    kept.unlink()


def exportReport(report):
  return {key: report[key] for key in ("purpose", "failures", "findings", "coverage")} | {
    "excluded": groupedExclusions(report["excluded"]), "toConfirm": report["toConfirm"], "swim": report["swim"],
    "boundaries": report["boundaries"], "zoneLines": report["zoneLines"], "structures": report["structures"], "placedTriangles": report["placedTriangles"],
  }


@guardedTool(description=(
  "Run every check exportZone runs for a purpose on the open scene, writing nothing, and list every failure (what would stop the export)"
  " and every finding at once. `path` is where the archive would go (an absolute <zone>.eqg path, the zone's short name in lowercase"
  " letters and digits)." + exportChecksHelp
))
async def checkExport(context: Context, path: str, purpose: str):
  archivePath, zone = exportTarget(path)
  report = await callBridge(context, "checkZoneExport", {"purpose": purpose})
  return {"path": str(archivePath), "zone": zone} | exportReport(report)


@guardedTool(description=(
  "Write the saved scene as an EQG zone archive at `path`, an absolute path ending in <zone>.eqg, the zone's short name in lowercase"
  " letters and digits, after the checks for its purpose (checkExport): any failure refuses the export and lists them all, and nothing is"
  " written. Every file is written whole under a temporary name first; then the side files take the last export's places and the"
  " archive goes in last, and a failure at any point puts every file back as it was, so a refused or failed export never replaces the"
  " last good archive or its side files and leaves no partial file. The `terrain` collection's meshes become the zone's terrain;"
  " every other rendered mesh becomes a model placed at its object's transform (copies sharing a mesh and without modifiers share one"
  " model) and every collection instance a model of its collection's meshes and, to any depth, of the collections its members instance."
  " So a kit piece is one model however often placed, a placed building one model per part shared by its placements and its plinth one"
  " of its own, a bridge, flight, or walkway one model (or terrain, laid in the terrain collection), and a wall its section piece's"
  " model, one model per distinct shear, and its post's. createMaterial materials export as the client's shaders:"
  " diffuse and normal map as Opaque_MaxCB1.fx, diffuse only as Opaque_MaxC1.fx, a cutout (diffuse only) as Chroma_MPLBasicAT.fx;"
  " createLiquidMaterial materials as its water, waterfall, and lava shaders with their values. DDS textures are stored unchanged, others"
  " as uncompressed DDS. The swim volumes go into the .zon as AWT_ (water) and ALV_ (lava) regions as they stand (export derives none)"
  " and the zone lines as ATP_ regions, unturned. The boundaries (placeBoundaryWall, placeBoundaryPlane) go into the terrain as"
  " triangles without a material, which the client never draws but collides with (flag 0), a wall's facing both ways and a lid's down"
  " and a floor's up, as they face in the scene; triangles of liquid materials, cutout"
  " materials, objects marked passable (markPassable), and faces flagged passable (a span's ropes and rails) are flagged 0x1, which the"
  " client lets players through."
  " Point lights placed with placeLights go into the .zon; emitters placed with placeEmitters go into <zone>_EnvironmentEmitters.txt"
  " beside the archive (the client reads that list loose from its own folder). A zone with housing (setZoneHousing) also gets"
  " <zone>_housing.json beside the archive, its plots as Peridot's plot content gives them (address, border door, center and heading in"
  " the server's axes, size across and along, price, upkeep, item capacity, pets, features) with their border doors (OBP_LOTSQUARE or"
  " OBP_GUILDSQUARE, open type 160, in the server's axes and EQ heading), and <zone>_assets.txt naming stonesquare.eqg, which holds the"
  " border models. Lists an earlier export left that this scene no longer has are removed. No baked light is written yet." + exportChecksHelp
))
async def exportZone(context: Context, path: str, purpose: str):
  archivePath, zone = exportTarget(path)
  checked = await callBridge(context, "collectZoneExport", {"outputFolder": str(toolingRoot / "exports" / zone), "zoneName": zone, "purpose": purpose})
  report = checked["report"]
  if report["failures"]:
    raise ToolError(
      f"exportZone ({purpose}) refused, nothing written: {len(report['failures'])} failure(s); checkExport lists the findings too\n"
      + json.dumps(report["failures"], indent=1)
    )
  collected = checked["collected"]
  try:
    data, summary = await anyio.to_thread.run_sync(eqgExport.zoneArchive, collected)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  housing = collected["housing"]
  hasPlots = housing is not None and bool(housing["plots"])
  emitterListPath = archivePath.parent / f"{zone}_EnvironmentEmitters.txt"
  housingPath = archivePath.parent / f"{zone}_housing.json"
  assetListPath = archivePath.parent / f"{zone}_assets.txt"
  # A list left from an earlier export would place emitters or plots this scene no longer has, so a list with nothing to say is removed.
  sideContents = {
    emitterListPath: eqEmitters.emitterListText(collected["emitters"]).encode("latin1") if collected["emitters"] else None,
    housingPath: json.dumps({"zone": zone} | housing, indent=1).encode("ascii") if hasPlots else None,
    assetListPath: "".join(f"{archive}\r\n" for archive in housing["assets"]).encode("latin1") if hasPlots else None,
  }
  try:
    await anyio.to_thread.run_sync(replaceExportFiles, archivePath, data, sideContents)
  except OSError as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  return summary | {"path": str(archivePath)} | exportReport(report) | {
    "emitterList": str(emitterListPath) if sideContents[emitterListPath] is not None else None,
    "housing": None if housing is None else {"role": housing["housing"]["role"], "plots": len(housing["plots"]), "file": str(housingPath) if hasPlots else None, "assetList": str(assetListPath) if hasPlots else None},
  }


def passArrays(passes):
  """Render passes as arrays, their colors no longer premultiplied by coverage as a transparent render stores them."""
  arrays = {}
  for name, path in passes.items():
    array = numpy.load(path)
    coverage = array[..., 3:4]
    array[..., :3] = numpy.where(coverage > 0, array[..., :3] / numpy.maximum(coverage, 1e-6), 0)
    arrays[name] = array
  return arrays


# calibrateShot draws out to this, past any fog it fits or is given.
calibrationClip = 1000000.0
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
      await placeEQModel(context, spawn["folder"], npc["name"], location, rotation, spawn["scale"], spawn["avatarHeight"], True, collection, spawn["details"], "spawn")
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
      await placeEQModel(context, folder, name, location, rotation, scale, 0, False, collection, details, "door" if prop["kind"] == "door" else "object")
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
  # The fog is used as given or fitted, without the client's pull-in at the far clip: calibration views stay well inside it.
  if all(fogGiven):
    environment = {"fogColor": fogColor, "fogStart": fogStart, "fogEnd": fogEnd, "fogDensity": fogDensity, "fogOn": True, "maxClip": calibrationClip, "newEngineZone": newEngineZone}
  else:
    environment = {"fogColor": [0, 0, 0], "fogStart": 0, "fogEnd": 100000, "fogDensity": 0, "fogOn": True, "maxClip": calibrationClip, "newEngineZone": newEngineZone}
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
  measured = passArrays((await callBridge(context, "renderPasses", {"view": view, "outputFolder": str(runFolder), "passNames": ["lit", "base", "normal", "baked", "share", "distance"], "sky": None}))["passes"])
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
  rendered = passArrays((await callBridge(context, "renderPasses", {"view": view, "outputFolder": str(runFolder), "passNames": ["lit"], "sky": None}))["passes"])["lit"]
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
  return await callBridge(context, "pick", {"view": view, "pixel": pixel, "sky": await zoneSky(await callBridge(context, "getZoneProperties", {}))})


selectorHelp = (
  " A selector picks part of a mesh by world position or surface: {\"all\": true}, {\"sphere\": {\"center\": [x,y,z], \"radius\": r}},"
  " {\"box\": {\"minimum\": [x,y,z], \"maximum\": [x,y,z]}}, {\"cylinder\": {\"center\": [x,y], \"radius\": r, \"bottom\": z, \"top\": z}},"
  " {\"facing\": {\"direction\": [x,y,z], \"withinDegrees\": d}}, {\"slope\": {\"minimumDegrees\": a, \"maximumDegrees\": b}} (0 flat, 90 vertical, over 90 overhanging), {\"height\": {\"minimum\": z, \"maximum\": z}}, {\"nearPath\": {\"path\": [[x,y,z], ...], \"radius\": r}} (horizontal distance), {\"material\": name}, {\"vertexGroup\": name}, {\"insideObject\": closedMeshName}, {\"region\": regionName} (inside a region createRegion made),"
  " {\"noise\": {\"featureSize\": f, \"share\": s, \"seed\": n}} (patches about f across covering about the fraction s of the surface, for breaking up one material with another),"
  " {\"underWater\": waterBodyName} (under a pool or river's surface or on it: its bed; a face only when all its corners are), {\"nearWater\": {\"water\": name, \"distance\": d}} (out of the water within d in plan of its waterline, where the mesh meets the surface: wet banks; a face the waterline crosses or meets, so bed and banks leave no gap, or all of whose corners lie within d),"
  " both following whole faces: for a bed and a bank band that end exactly on the waterline and d out from it, cut the mesh along those lines first (cutContours with waterline and levels [0, d]; carveWaterBed cuts the waterline itself),"
  " {\"cave\": caveName} or {\"cave\": true} (the lining of a cave cutCave made, or of every cave: surface tools leave linings out unless their selector names the cave),"
  " {\"and\": [selectors]}, {\"or\": [selectors]}, {\"not\": selector}. Shapes test vertex positions, or face centers for face operations."
  " A selector that matches nothing is an error. Masks such as slope and height pick within an area you chose (a region, a stroke);"
  " a recipe belongs to a region, not to the whole zone."
)
allSelector = {"all": True}
caveSurfaceHelp = (
  " A cave's lining (cutCave) is left out unless the selector names the cave ({\"cave\": name}); a stroke that reaches a lining is kept"
  " with its cave and painted again each time the cave is cut."
)
caveShapingHelp = (
  " Around a cave (cutCave) its lining stays where it is (caveLiningLeft counts the lining vertices left alone) and the vertices where it"
  " meets the ground follow that ground, so its mouth stays sealed."
)
caveFacesHelp = (
  " Refused on a mesh holding caves (cutCave), whose take-back needs the faces around them as they were cut: take each back (removeCave,"
  " which returns its definition), make the change, and cut it again."
)


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
async def createRockFromOutline(
  context: Context, name: str, outline: list[list[float]], top: float, underside: dict, flare: list[list[float]] | None = None,
  spacing: float = 16.0, ground: str | None = None, collection: str | None = None,
):
  """Build rock that has an underside, which ground shaped from above cannot have (a natural arch or bridge, an overhanging lip, a
  ledge off a wall), drawn as the rock around it is: a closed outline [[x, y], ...] in plan with straight runs and jogs, its faces
  dropping sheer from a flat top at height `top` to its underside. `underside` {"axis": [[x, y], [x, y]], "profile": [[distance,
  height], ...]} gives the underside's height by distance along the axis from its first point, smooth between the points without
  overshooting them and held beyond its ends: for an arch, the opening as seen from the side, its crown high and its ends low in the
  walls. `flare` [[distanceInsideFace, rise], ...] curves the underside up toward the faces, as an arch's vault opens out at each
  side. Run the outline well into the rock it grows from; with `ground` (the terrain it meets), the top tucks just under that ground
  wherever the ground stands as high, so the ground runs on across it with no lip or gap and the hidden parts stay inside the rock.
  Meshed every `spacing` units on a world grid, its faces in rows at the same heights; into the terrain collection (or `collection`)
  so it exports as terrain. Returns its thinnest point and how many vertices tucked under the ground."""
  return await callBridge(context, "createRockFromOutline", {
    "name": name, "outline": outline, "top": top, "underside": underside, "flare": flare, "spacing": spacing, "ground": ground,
    "collection": collection,
  })


@guardedTool()
async def transformObjects(
  context: Context, names: list[str], translate: list[float] | None = None, rotateDegrees: list[float] | None = None, scale: list[float] | None = None,
  location: list[float] | None = None, rotationDegrees: list[float] | None = None,
):
  """Move, rotate, or scale objects: relative (translate, rotateDegrees about world axes, scale factors) or absolute (location, rotationDegrees); not both forms of one channel. A water body, built from what it is made from, only translates: what it is made from moves and it is built again against the ground there, its sprays with it (and the feet of falls landing in a moved pool or river); editWater turns or reshapes one.
  A placed kit piece turns only about the vertical and keeps scale 1: a tilt or scale of one is refused, moving none."""
  return await callBridge(context, "transformObjects", {"names": names, "translate": translate, "rotateDegrees": rotateDegrees, "scale": scale, "location": location, "rotationDegrees": rotationDegrees})


@guardedTool()
async def duplicateObjects(context: Context, names: list[str], offset: list[float], linkData: bool = False):
  """Copy objects, with everything parented under them, offset from the originals; linkData shares the meshes instead of copying them
  (a copied mesh takes its copy's name). A water body's copy is built anew from what the body is made from moved by the offset (seed
  and level, path, lip and bottom, outlines, strokes, spray points), against the ground there, with its sprays. Returns original to copy
  names, children included."""
  return await callBridge(context, "duplicateObjects", {"names": names, "offset": offset, "linkData": linkData})


@guardedTool()
async def joinObjects(context: Context, names: list[str], into: str):
  """Merge meshes into one object, for example a trunk and canopy into one tree; `into` keeps its name, origin, and transform, its mesh
  takes its name (the model name zone export writes), and the others are removed."""
  return await callBridge(context, "joinObjects", {"names": names, "into": into})


@guardedTool()
async def deleteObjects(context: Context, names: list[str]):
  """Delete objects; meshes left with no users are removed too, and a water body's spray emitters (sprayWater) go with it. The feet of
  the falls that land in a deleted pool or river are sprayed again on what then lies under them (fallFeetSprayedAgain); the delete is
  refused, before anything goes, where one cannot stand there (ripple rings left on dry ground: take them off the fall first)."""
  return await callBridge(context, "deleteObjects", {"names": names})


@guardedTool()
async def organize(context: Context, renames: dict[str, str] | None = None, parents: dict[str, str | None] | None = None, collections: dict[str, str] | None = None):
  """Rename objects (old to new, applied first; a mesh only that object uses takes the new name too, as zone export names models by
  their mesh, and a water body's spray emitters theirs, named for it), then set parents (child to parent, or null to clear; world transform kept) and move objects into collections (created
  if missing), using the new names."""
  return await callBridge(context, "organize", {"renames": renames, "parents": parents, "collections": collections})


@guardedTool()
async def getObjectDetail(context: Context, name: str):
  """One object in depth: transform (rotation as XYZ Euler degrees whatever its rotation mode), size, world bounds (for a collection
  instance, its instanced meshes'; null when it instances none), parent, collections, modifiers; for meshes the mesh's name, the
  vertex, face, and triangle counts, faces per material, UV layers, world units per texture repeat, vertex groups, shaping passes,
  surfacing layers, its defined passes (graded routes, dressed facades, the plots graded on it, in the order made) and its caves
  (cutCave), each with whether its ground moved since it was made (stale: grading it again would move it, or the ground within the
  cave's reach moved, and how far), which regradeTerrain or editCave puts right. A placed kit piece gives kitPiece: its kit, piece,
  kind, module, passable, fingerprint, facing, and its sockets in the world, each joined to another's or free; a kit piece's own mesh
  gives kitPiece: its record (kind, sockets, passable, openings), size, module, triangles, each material's texture and measured repeat,
  seams, and fingerprint. A structure's part gives structurePart: its structure, kind, and role (span, section, or post; a building's
  part by name: exterior, interior, roof, plinth). A placed piece gathered into a prefab gives prefabPart: its prefab and part."""
  return await callBridge(context, "getObjectDetail", {"name": name})


@guardedTool()
async def measure(context: Context, points: list[list[float]], snapToSurface: bool = False):
  """Points and the distances, horizontal distances, height changes, and slopes between consecutive ones; snapToSurface drops each
  point first onto the surface players stand on below it, as walkRoute does (rendered meshes and collection instances; not water,
  faces players pass through, guides, regions, spawns, or doors; undersides, and ground inside a solid such as a rock sunk into it, are passed through, while ground
  under one-sided cover such as a roof plane or leaf cards is stood on). One point gives a surface height."""
  return await callBridge(context, "measure", {"points": points, "snapToSurface": snapToSurface})


steepestWalkableDegrees = math.degrees(math.acos(playerScale.walkableNormalZ))


@guardedTool(description=(
  "Walk a route as a player would, over what the client collides with (rendered meshes and collection instances, and the"
  " boundaries, never drawn; not water, which is waded or swum, nor faces players pass through: liquid and cutout materials, objects"
  " marked passable, faces an imported zone file flags passable; nor guides, regions, spawns, or doors, taken as open; ground inside a"
  " solid is no footing, ground under one-sided cover such as a roof plane is): from its first point, following the footing underfoot"
  " past each point of `path` [[x, y, z], ...], of the saved review route named `route` (saveReviewRoute), or of the walk line of the"
  " bridge, flight, or walkway named `route` (its centerline at deck height, run on 5 past each end that stands on footing), whose"
  " heights only need to be within a step of the footing (so a route can run over an arch or under it). Judged for the player of"
  f" playerScale, {playerScale.playerHeight:g} units tall, who walks faces up to {steepestWalkableDegrees:.1f} degrees and steps up"
  f" {playerScale.stepHeight:g} (both measured in the RoF2 client), in half-unit strides whatever `sampleSpacing`, which sets only the"
  " profile's rows (give `path` or `route`, not both; renderRouteStrip shows the walk in pictures). Returns the length walked, the"
  " steepest face stood on, the steepest grade climbed or descended between two profile rows (steepestGrade: a stair's level treads"
  " stand at 0 but climb at its pitch), the narrowest footing (how far it runs to each side before a drop of more than a player's"
  " height, a wall, a step too high, or a face too steep; null beyond 60), the lowest headroom, the deepest water over the footing;"
  " `problems`, everything that stops a player, each once over the stretch it covers: blocked (a boundary across the way at half a"
  f" player's height, where it stands), rise (a wall or step over {playerScale.stepHeight:g} in the way, its height, how far up its face"
  f" stays steeper than {steepestWalkableDegrees:.1f}, a plane's as much as a block's; null past 60), drop (no footing within 60 below), steep (a"
  f" face over {steepestWalkableDegrees:.1f} climbed, its steepest), headroom (under {playerScale.playerHeight:g}, its lowest); after a boundary, a"
  " rise, or a drop the walk takes up again where the route's own heights find footing (resumesAt, null if never); `oneWay`, ways down a"
  f" player cannot climb back: ledge (a drop over a step, its height) and steep (a face over {steepestWalkableDegrees:.1f} descended); walkable when"
  " there are no problems; and a profile along the way (each row with the water depth over its footing, or null). Use it on decks,"
  " ramps, ledges, and the ways into an area."
))
async def walkRoute(context: Context, path: list[list[float]] | None = None, route: str | None = None, sampleSpacing: float = 4.0):
  return await callBridge(context, "walkRoute", {"path": path, "route": route, "sampleSpacing": sampleSpacing})


@guardedTool(description="Move the selected vertices of a mesh by `offset` [x, y, z] world units. With `falloff` {center, radius, curve: constant|linear|smooth|sharp} the move fades with distance from the center; this is the precise, fine-detail edit. With shaping passes, the move goes into the active pass." + caveShapingHelp + selectorHelp)
async def moveVertices(context: Context, objectName: str, selector: dict, offset: list[float], falloff: dict | None = None):
  return await callBridge(context, "moveVertices", {"objectName": objectName, "selector": selector, "offset": offset, "falloff": falloff})


@guardedTool()
async def sculptAtPoint(
  context: Context, objectName: str, mode: str, center: list[float], radius: float, strength: float,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1,
):
  """Sculpt a mesh within `radius` of `center`: raise, lower, or crease (strength in units, along the region's average normal or `direction`); smooth or flatten (strength a fraction 0 to 1; smooth repeats `iterations` times). Falloff curve: constant, linear, smooth, sharp. With shaping passes, the change goes into the active pass. Around a cave (cutCave) its lining stays where it is (caveLiningLeft counts the lining vertices left alone) and the vertices where it meets the ground follow that ground."""
  return await callBridge(context, "sculptAtPoint", {"objectName": objectName, "mode": mode, "center": center, "radius": radius, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations})


@guardedTool()
async def sculptAlongPath(
  context: Context, objectName: str, mode: str, path: list[list[float]], strength: float, radius: float | None = None, radii: list[float] | None = None,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1, profile: list[list[float]] | None = None, conformRim: bool | None = None, conformBreaks: bool = False,
):
  """Sculpt along a polyline path [[x,y,z], ...] within `radius`, or within `radii` (one per path point, the stroke widening or narrowing evenly between them, so one stroke carves a canyon that pinches to a gorge): raise, lower, crease, smooth, flatten as in sculptAtPoint; carve, which cuts vertically down to the path's own heights shaped by `profile` [[lateralFraction, heightAboveFloor], ...] from 0 (center) to 1 (edge); or fill, which raises ground up to such a profile (a mesa: a flat cap, a cliff, a slope at the base). carve and fill strength is a fraction, and their path can be a single point (a pit or a butte). With conformRim (carve only, on by default; needs rising profile heights), vertices just outside the cut slide onto the rim contour so the edge follows the profile rather than the grid; the mesh's open edge stays put. With conformBreaks (carve and fill), the vertices nearest each break of the profile slide onto its contour first wherever the stroke shapes them, so stepped profiles (strata, ledges, terraces) make clean lines along the path instead of zigzags across the grid. carve and fill then triangulate the cells they shaped along the contours, as followContours does (splitCells, turnedDiagonals), and put back where the plain cut leaves them any snapped vertices that leave their cell no diagonal facing up (keptOffContours). Results count foldedFaces: faces the move turned over, a sign it was too strong for the mesh's spacing. With shaping passes, the change goes into the active pass. Around a cave (cutCave) its lining stays where it is (caveLiningLeft counts the lining vertices left alone), the vertices where it meets the ground follow that ground, and none of its vertices slides sideways onto a break or rim."""
  return await callBridge(context, "sculptAlongPath", {"objectName": objectName, "mode": mode, "path": path, "radius": radius, "radii": radii, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations, "profile": profile, "conformRim": conformRim, "conformBreaks": conformBreaks})


@guardedTool()
async def sculptOutline(
  context: Context, objectName: str, mode: str, outline: list[list[float]], base: float, profile: list[list[float]], strength: float = 1.0,
  conformBreaks: bool = True,
):
  """Carve or fill ground by distance from a closed outline [[x, y], ...] drawn in plan, for forms an artist draws rather than
  sweeps: a cliff line with straight runs and sharp jogs, a jointed slot, an angular mesa or terrace. `profile` [[signedDistance,
  heightAboveBase], ...] gives the height above `base` by distance from the outline, positive inside it and negative outside, the
  distances rising; deeper inside than its last distance it holds the last height, and farther outside than its first it leaves the
  ground alone. carve lowers ground above the profile, fill raises ground below it, `strength` a fraction. Every ledge runs parallel to
  the outline: its corners stay angular inside it (within half an edge) and round outside it. With conformBreaks (on by default), the vertices nearest each break
  of the profile slide onto it first wherever the stroke shapes them, so its ledges and cliff edges are clean lines; then the shaped cells are triangulated along the
  contours as followContours does, and snapped vertices that would leave a cell no diagonal facing up are put back (keptOffContours).
  With shaping passes, the change goes into the active pass. Around a cave (cutCave) its lining stays where it is (caveLiningLeft), the
  vertices where it meets the ground follow that ground, and none of its vertices slides sideways onto a break."""
  return await callBridge(context, "sculptOutline", {"objectName": objectName, "mode": mode, "outline": outline, "base": base, "profile": profile, "strength": strength, "conformBreaks": conformBreaks})


@guardedTool()
async def addShapingPass(context: Context, objectName: str, name: str):
  """Add a named shaping pass to a mesh and make it active: vertex moves and sculpting go into the active pass, which can later be
  turned up or down, muted, removed, or collapsed, so a shaping step is revised without redoing the others. Tools that add or remove
  vertices otherwise (delete, extrude, inset, bevel, subdivide, booleanCut, decimate, join) refuse while a mesh has passes; collapse
  them first. Cuts along a line (cutContours, carveWaterBed's cut along the waterline) and turned diagonals keep them, each new vertex
  placed alike in every pass. A cave (cutCave) keeps them too; while a mesh holds caves, the tools that change faces refuse it."""
  return await callBridge(context, "addShapingPass", {"objectName": objectName, "name": name})


@guardedTool()
async def setShapingPass(context: Context, objectName: str, name: str, strength: float | None = None, muted: bool | None = None, makeActive: bool = False):
  """Change a shaping pass: its strength (-1 to 2: 0.5 halves what it holds, -1 inverts it), muted, or makeActive so shaping goes into it."""
  return await callBridge(context, "setShapingPass", {"objectName": objectName, "name": name, "strength": strength, "muted": muted, "makeActive": makeActive})


@guardedTool()
async def removeShapingPass(context: Context, objectName: str, name: str):
  """Remove a shaping pass and what it holds; the other passes keep theirs. A graded route's pass takes its definition with it; a
  facade's pass (dressFacade) takes the dressing back, cuts its cave again to fit the ground as it was (refit), and box-maps the faces it
  had moved again from where they now stand (remappedFaces), so the facade can be dressed again."""
  return await callBridge(context, "removeShapingPass", {"objectName": objectName, "name": name})


@guardedTool()
async def collapseShapingPasses(context: Context, objectName: str):
  """Make a mesh's shape as it is seen (its passes combined) its new base and drop the passes, so its faces can change again."""
  return await callBridge(context, "collapseShapingPasses", {"objectName": objectName})


@guardedTool()
async def gradeRoute(
  context: Context, objectName: str, name: str, points: list[list[float]], width: float | None = None, widths: list[float] | None = None,
  maximumGradeDegrees: float = 26.0, cutBatterDegrees: float = 70.0, fillBatterDegrees: float | None = 45.0, landingLength: float | None = None,
):
  """Grade a route into the ground (objectName) as a builder benches a path into a hillside: a bench `width` wide (or `widths`, one per
  point, changing evenly between them), level across, along `points` [[x, y] or [x, y, z], ...]. A point's z fixes its height; the
  points between fixed heights take an even grade by plan length, and an end given without z takes the ground there. A stretch steeper
  than maximumGradeDegrees is refused, naming it and the run it needs: the tool never adds a switchback, so place the hairpins' points.
  A bend turns on an arc the route's width in radius, so the bench stays level across it; a turn over 90 degrees (a hairpin) turns on
  an arc of half the width and is a flat landing, at least `landingLength` long (by default the width) and at least the arc, centered
  on the turn. Ground above the bench is cut down to it, meeting the ground at cutBatterDegrees; ground below is filled up to it,
  meeting the ground at fillBatterDegrees (null makes a ledge whose outside drops away, refused where its bench would stand more than 2
  over the ground, naming each span). The whole route is graded as one: inside its bench the bench's height wins, elsewhere the lowest
  cut and the highest fill, and where those disagree (between two legs) the cut, so a lower leg is never buried; the ground between two
  legs close together is dressed into one straight bank from one's edge to the other's, and between legs further apart nothing is left
  standing above the higher of them, so no ridge or berm stands between a switchback's legs. Refused where one part's
  batter would reach another part's bench (legs too close for their difference in height), naming the spot and the separation needed;
  where any of the bench lies under rock (a covered way is a cave); and where its batters would reach more than 600 out. The vertices
  nearest the bench's edges slide onto them, so the path's edges run as clean lines. The route goes into its own pass, "route <name>",
  which keeps its definition and takes no other shaping: grading it again under its name replaces it, rebuilt from the ground as it
  stands without that pass; removeShapingPass takes it back, definition and all. It is graded on the ground without any defined pass
  made after it (a later route, or plot grading first made later), and every later one whose ground it changes is graded again after
  it (replayed, with what each moved), so where two meet the later one wins; regradeTerrain puts them all back on target after other
  shaping. Returns each segment's grade, the landings, the deepest cut and highest fill, how far the batters reach, the centerline at
  its graded heights (for painting the path or walking it), and a walkRoute along that centerline. Look at it at eye height up and down
  each leg, from across the valley, and in sections across the legs; then paint the bench and its batters."""
  return await callBridge(context, "gradeRoute", {
    "objectName": objectName, "name": name, "points": points, "width": width, "widths": widths, "maximumGradeDegrees": maximumGradeDegrees,
    "cutBatterDegrees": cutBatterDegrees, "fillBatterDegrees": fillBatterDegrees, "landingLength": landingLength,
  })


@guardedTool()
async def regradeTerrain(context: Context, objectName: str):
  """Put every defined pass on a mesh back on target after other shaping changed the ground under them (a roughen, a sculpt, a pass
  turned up, down, or off): each graded route (gradeRoute), dressed facade (dressFacade), and the plots graded on it (gradePlot, all
  together) are taken back and graded again in the order they were first made, each on the ground as it then stands, so where two meet the later one still wins. A
  muted or turned defined pass comes back unmuted at full strength (restoredFrom says which). A defined pass graded on the ground as the
  hand passes leave it takes back their work where it grades the ground (a facade's apron levels a mound sculpted on it): each replay
  names the hand passes it overrode there (overrodeHandWork: pass, vertices, largest offset). Then every cave (cutCave) whose ground
  moved since it was cut is cut again to fit it (refittedCaves, with how far its ground had moved), and the faces a facade moved are
  box-mapped again. Whole or not at all: a refused refit leaves the mesh as it was. Reports what each one moved, in order, and
  staleStructures: every structure (buildBridge, buildWall, ...) whose ground or kit changed since it was laid, with why; editStructure
  lays each again."""
  return await callBridge(context, "regradeTerrain", {"objectName": objectName})


@guardedTool()
async def cutCave(
  context: Context, objectName: str, name: str, path: list[list[float]], widths: list[float], heights: list[float], wallMaterial: str,
  floorMaterial: str, worldUnitsPerRepeat: float, edgeLength: float = 16.0, wallShare: float = 0.35, breakup: dict | None = None,
  mouthFade: float | None = None, maximumFloorDegrees: float = 30.0, trimBands: list[dict] | None = None,
):
  """Cut a cave into a terrain mesh (objectName) as the client's own caves are built, one terrain holding the hill and the room under
  it: a closed tube swept along the floor `path` [[x, y, z], ...] with one width and height per point, its floor flat across, its walls
  straight up to `wallShare` of the height and a vault above. `wallShare` 1 cuts a hall, as Crescent's guild halls and market are carved
  into its cliffs: walls straight up the full height under a flat ceiling, its corners square, its ends never rounded: a blind end closes
  as a flat wall, and an open end stands wholly in the open in front of the cliff (dressFacade dresses the cliff into a carved face
  there). `trimBands` [{fromFloor, height, material, worldUnitsPerRepeat}] run along the walls at those heights over the floor in every
  row (a dado, a frieze: Crescent's cr_tile_trim_marble_dark), the walls cut exactly along their edges, in their own createMaterial
  material mapped along the band, a strip texture running once up it (v from the band's bottom at its repeat); set again on every cut,
  before the strokes kept with the lining. Breakup stays the artist's choice (none for a dressed hall). Widths and heights ease from point to point and the floor grades evenly
  between their heights; a room is a wide stretch of the path. A bend turns on an arc the width in radius (less where the points are
  close). `breakup` {featureSize, amplitude, seed} moves the walls and vault along their outward directions by noise, the floor kept
  flat, fading out within `mouthFade` (default twice edgeLength) of wherever the tube lies in the open, so the lip stays a clean arch.
  Each end is open, some of its floor within a step of walkable ground (a mouth, its section in the open but for a sill a step deep;
  two make a through tunnel; or a gallery's end on the ground beside a cliff, part in the rock), a ledge (part in the rock, its floor
  running out over a drop beside it: a gallery's dead end up a cliff), or blind (wholly inside the rock); an end with rock in its
  section closes as a dome on its floor over half its width beyond its last point. A floor whose middle hangs in the air (no rock under
  it within a step) is refused, naming the stretch. traceLedge traces a gallery's path along a cliff.
  The ground within reach of the tube (half its width, three breakup amplitudes, and two edges) is closed into a solid, the tube taken
  out of it with the exact boolean, and the result spliced into the same mesh keeping every shaping pass: the ground faces the cut
  changed (the plug) are recorded and deleted as faces only, their vertices staying in every pass, so removeCave puts the ground back
  exactly; the new vertices where the tube meets the ground (the ring) follow the plug's triangles in every pass, so the mouth stays
  sealed however the ground is shaped; the tube's own surface (the lining) holds no offset in any pass. Short edges at the mouth are
  welded down to a third of `edgeLength`. Lining faces facing up get `floorMaterial`, the rest `wallMaterial`, with every surfacing
  layer uncovered on them, box-mapped at `worldUnitsPerRepeat` and smooth shaded; ground faces the cut split keep their materials,
  paint, and mapping.
  Refused, changing nothing: a stretch of floor steeper than `maximumFloorDegrees` (naming it and the run it needs), a bend tighter than
  half the width, an end part in the rock with its floor buried more than a step under the ground (for a hall, any end part in the rock
  and part in the open, or a ledge), a floor hanging in the air, both
  ends wholly inside the rock, the tube reaching the terrain's border or another cave's reach, a mesh with modifiers or shared with another object, and caves that fail their integrity checks;
  `wallShare` outside (0, 1]; a band reaching above the walls' straight part (wallShare of the height) at any path point (naming it),
  bands overlapping, a band below the floor or not tall, a band material createMaterial did not make, a repeat not positive.
  Returns the faces and vertices it made, the shortest edges of the lining, the pieces of ground at the mouth, and the seam, each end's
  kind, each trim band's faces, and the floor's level stretches (start, end, length, height, narrowest width: a stretch about 220 wide and long holds a stock
  player plot). The cave is kept with its definition: the ground within its reach is fingerprinted, getObjectDetail and exports flag it
  stale once that ground moves, and editCave or regradeTerrain cuts it again to fit. Around a cave, shaping leaves its lining where it
  is (results count caveLiningLeft) and keeps its ring on the ground; strokes never slide its vertices sideways; contour cuts, turned
  diagonals, and face edits refuse or keep clear of it (removeCave, change, cutCave with the definition it returned); surface tools
  leave its lining out unless their selector names it ({"cave": name}), and paint that reaches its lining is kept with it and painted
  again on every cut. Look at it from outside the mouth, close on the throat, from inside looking out, in the room with the figure,
  from the hill above, and in sections across and along."""
  return await callBridge(context, "cutCave", {
    "objectName": objectName, "name": name, "path": path, "widths": widths, "heights": heights, "wallMaterial": wallMaterial,
    "floorMaterial": floorMaterial, "worldUnitsPerRepeat": worldUnitsPerRepeat, "edgeLength": edgeLength, "wallShare": wallShare,
    "breakup": breakup, "mouthFade": mouthFade, "maximumFloorDegrees": maximumFloorDegrees, "trimBands": trimBands,
  })


@guardedTool()
async def editCave(context: Context, objectName: str, name: str, changes: dict | None = None):
  """Change a cave cut into a terrain (cutCave) and cut it again in one step: `changes` holds any of its definition's path, widths,
  heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare (1 for a hall), breakup, mouthFade,
  maximumFloorDegrees, trimBands; the cave is taken back and cut again from its definition with them merged in, against the ground as it
  now stands, its trim bands set again and the strokes kept with its lining painted again. With no changes it refits the cave to the ground (after a sculpt at its mouth or a pass turned up). A
  facade dressed at its mouth (dressFacade) follows it: made again from what it was given (faceAt, width, height, apron, blend,
  turnDegrees) on the changed cave, dressed again where its face moved (refitFacades), and refused when it would no longer frame the
  cave (narrower than the cave plus 2, lower than it plus 1), naming the facade to dress again or take back. If the new cut or a facade
  is refused, the cave and the ground stay exactly as they were. Returns what taking it back restored and what the new cut made."""
  return await callBridge(context, "editCave", {"objectName": objectName, "name": name, "changes": changes})


@guardedTool()
async def removeCave(context: Context, objectName: str, name: str):
  """Take a cave (cutCave) back out of a terrain exactly: its lining and the pieces of ground at its mouth go, and the ground's own faces
  return on their vertices with every shaping pass made since, each carrying the surfacing and mapping its largest piece of ground held
  (painted or mapped since the cut). Returns the cave's definition, to cut it again with cutCave, and the strokes kept with its lining."""
  return await callBridge(context, "removeCave", {"objectName": objectName, "name": name})


@guardedTool()
async def dressFacade(
  context: Context, objectName: str, cave: str, end: str, faceAt: float, width: float, height: float, apron: float, blend: float = 16.0,
  turnDegrees: float = 0.0,
):
  """Dress the cliff at a cave's mouth into a carved face, so a hall (cutCave with wallShare 1) reads as cut into the rock rather than as
  a cave mouth, as Crescent's halls open on dressed faces. The face stands on a vertical plane crossing the cave's path `faceAt` along
  it in from its `end` ("start" or "end"), square to the path, or turned `turnDegrees` (clockwise from above, at most 45 either way) to
  stand along the cliff's line where the hall enters it obliquely; `width` wide centered on the path, from the floor's height there (F)
  up to F + `height`. On the terrain objectName: each ground edge crossing the face's line within its width has its front vertex slid
  square onto the line at F and its back vertex onto the line at F + height, the vertices nearest the face's two sides slid along the
  line onto them, so the face is a flat vertical rectangle with straight edges standing in a recess in the cliff (the cliff beside and
  above it is not moved); the ground in front of it within `apron` and the width is set level at F, cut or filled, and the ground in
  front of its line past the apron's edges eases back to the ground as it was over `blend` (smoothstep). The faces it moved are
  box-mapped again from where they stand, each material at the repeat the faces it left alone show (remappedFaces), so the face and its
  returns carry their texture unstretched. The ground read is the ground as it stands with the caves taken back (each cave's recorded
  faces in place of its cut), so it dresses the same with the cave cut or not; a cave's lining never moves and the vertices where it
  meets the ground follow that ground. It is a defined pass, "facade <cave> <end>", keeping the face itself (faceAt, turnDegrees,
  center, facingDegrees, width, height, apron, blend), so regradeTerrain replays it in order with the routes and plots and then refits
  stale caves, and editCave dresses it again on the changed cave; dressing again under that cave and end replaces it; removeShapingPass
  takes it back and refits the cave. The cave is refit in the same call (refit, as editCave reports it), so it opens on the face;
  staleCaves names others whose ground moved. foldedFaces counts faces left facing down, not the face's vertical returns. Then paint
  the face and apron (paintSurface with a box around them), place a portal piece at the returned `frame` (its front facing out), and
  look from the apron at eye height, from across the valley, from inside looking out, and in a section along the path.
  Refused, changing nothing: no such cave or end; an end that is not open (blind or a ledge); faceAt not positive or past the end's first
  bend; turnDegrees past 45; a width less than the cave's width where it crosses the face plus 2, or a height less than its height
  there plus 1; the ground just behind the line lower than F + height anywhere across the width (nothing to dress: move faceAt into the
  cliff, naming where); ground in front of the face (on the apron or easing back) higher than F + height (the face stands behind the
  cliff's face there and would cut a notch through the rock: move faceAt out, raise the height, or turn the face, naming where); the
  apron or blend reaching the ground within another cave's reach or the terrain's border; a mesh with modifiers. Returns the face's four
  corners (foot left, foot right, top right, top left, looking at it), `frame` {center (the foot's middle), facingDegrees (outward),
  width, height}, the face's vertices and all vertices moved, the apron's cut and fill, and the cave's refit."""
  return await callBridge(context, "dressFacade", {
    "objectName": objectName, "cave": cave, "end": end, "faceAt": faceAt, "width": width, "height": height, "apron": apron, "blend": blend,
    "turnDegrees": turnDegrees,
  })


@guardedTool()
async def traceLedge(
  context: Context, objectName: str, start: list[float], end: list[float], floorFrom: float, floorTo: float, width: float, height: float,
  side: str, insideShare: float = 0.75, step: float = 20.0,
):
  """Trace a starting path for a covered gallery or a rock shelter along a cliff of a terrain (objectName), for cutCave; changes
  nothing. Points every `step` or less from `start` to `end` ([x, y] in plan, along the cliff) each look toward `side` (left or right
  of travel from start to end: the side the rock stands on), a step over the floor, for where the rock begins, and are set so
  `insideShare` of the `width` lies inside the rock there; more than half keeps the floor's middle on rock and leaves its outer side
  open under the rock above. A point with no rock beside it within three widths (past the cliff's top) keeps the offset from the line
  of the nearest point that found the cliff, so the path runs on in line. The floor rises evenly from `floorFrom` to `floorTo` along the traced path. Start with the floor on the ground at the
  cliff's foot (grade it first, gradeRoute) and end on open ground at the top or on another floor: cutCave takes an end beside the
  cliff, part in the rock, when its floor is on the ground where it is open, and rounds it off. Returns `path`, `widths`, and
  `heights` ready for cutCave; for each point the share of its width measured inside the rock and whether it found the cliff
  (traced); for each segment its grade in degrees and whether it is covered (the rock over its floor's middle reaches above its
  vault). Refused when the traced path bends tighter than cutCave takes at that width (a rough face traced at a short step): trace it
  with a longer step. Between points the path runs straight while the cliff's face wanders, so a recess in the face takes a bite out
  of the floor's outer edge; a shorter step follows the face closer. Cut it with wall and floor materials close in value to the
  cliff's own rock, or the gallery reads as a stripe or a ribbon up the cliff; look at it from across the valley, at an angle, standing on it looking up and down, from above, and in sections across it."""
  return await callBridge(context, "traceLedge", {
    "objectName": objectName, "start": start, "end": end, "floorFrom": floorFrom, "floorTo": floorTo, "width": width, "height": height,
    "side": side, "insideShare": insideShare, "step": step,
  })


@guardedTool(description=(
  "Roughen the selected vertices of a mesh with fractal noise: bumps about `featureSize` units across, moving vertices `amplitude` units"
  " as a typical (root mean square) move, the largest about three times that, along each vertex's normal (`direction` normal: sideways on a wall, so cliffs break up too) or straight up. `octaves` (1-8) add finer"
  " noise, each twice as fine and `roughness` times as strong; the result warns when the finest octave is finer than the mesh's edges,"
  " which cannot hold it (it reads as a grain along the triangles). The same `seed` gives the same noise. `fadeDistance` ramps the effect"
  " in from the selection's edge so a mask leaves no step. Use it in its own shaping pass, confined to a region at that region's own scale"
  " rather than over the whole zone, coarse first (large featureSize, few octaves), then finer, turning each pass up or down after looking."
  + caveShapingHelp + selectorHelp))
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
  " sideways, moving every height at a spot alike so walls bend without shearing; surface moves along the surface; full moves in every direction. The same `seed` gives the same warp; `fadeDistance` ramps"
  " it in from the selection's edge. Use it in its own shaping pass, confined to a region." + caveShapingHelp + selectorHelp))
async def warp(
  context: Context, objectName: str, featureSize: float, amplitude: float, seed: int = 0, plane: str = "horizontal",
  selector: dict = allSelector, fadeDistance: float = 0.0,
):
  return await callBridge(context, "warp", {
    "objectName": objectName, "featureSize": featureSize, "amplitude": amplitude, "seed": seed, "plane": plane, "selector": selector,
    "fadeDistance": fadeDistance,
  })


@guardedTool(description=(
  "Flatten the selected part of a mesh into planar facets about `cellSize` across: each patch of vertices is pressed onto the plane"
  " that best fits it (by `strength`, 1 fully flat), so rock reads as broad faces meeting at sharp edges, as the client's natural"
  " terrain is built, and the texture carries the fine detail, where warp and roughen would leave smooth or bumpy noise. The same"
  " `seed` gives the same facets; `fadeDistance` ramps it in from the selection's edge. Use it in its own shaping pass, confined to the"
  " rock it should shape." + caveShapingHelp + selectorHelp))
async def facet(context: Context, objectName: str, cellSize: float, strength: float = 1.0, seed: int = 0, selector: dict = allSelector, fadeDistance: float = 0.0):
  return await callBridge(context, "facet", {"objectName": objectName, "selector": selector, "cellSize": cellSize, "strength": strength, "seed": seed, "fadeDistance": fadeDistance})


@guardedTool(description="Delete the selected faces of a mesh, with edges and vertices left unused; for example the terrain inside a rock that should form its own cave floor ({\"insideObject\": \"rockName\"})." + caveFacesHelp + selectorHelp)
async def deleteFaces(context: Context, objectName: str, selector: dict):
  return await callBridge(context, "deleteFaces", {"objectName": objectName, "selector": selector})


@guardedTool(description=(
  "Extrude the selected faces of a mesh by `distance` units along their average normal, or along `direction`. The new side faces take"
  " the material of the faces beside them and are box-mapped at the density box projection gives that material on the mesh (mappedFaces;"
  " on a surfaced mesh, as the mapping beneath its layers' transitions, so it stays as the layers compose again), so a box-projected mesh"
  " carries on its texture without a seam; faces left unmapped, where the mesh has no UV layer or the material no UV area, are listed"
  " with why (unmappedFaces)." + caveFacesHelp + selectorHelp))
async def extrudeFaces(context: Context, objectName: str, selector: dict, distance: float, direction: list[float] | None = None):
  return await callBridge(context, "extrudeFaces", {"objectName": objectName, "selector": selector, "distance": distance, "direction": direction})


@guardedTool(description=(
  "Inset the selected faces of a mesh as one region by `thickness`, pushed in or out by `depth`. The inset faces keep their texture as"
  " it lay; the new rim faces are box-mapped at the density box projection gives their material on the mesh (mappedFaces; on a surfaced"
  " mesh, as the mapping beneath its layers' transitions), or listed with why they could not be (unmappedFaces)." + caveFacesHelp + selectorHelp))
async def insetFaces(context: Context, objectName: str, selector: dict, thickness: float, depth: float = 0.0):
  return await callBridge(context, "insetFaces", {"objectName": objectName, "selector": selector, "thickness": thickness, "depth": depth})


@guardedTool(description="Bevel the edges whose two vertices are both selected, by `width` units in `segments` steps; `minimumAngleDegrees` limits it to edges at least that sharp." + caveFacesHelp + selectorHelp)
async def bevelEdges(context: Context, objectName: str, selector: dict, width: float, segments: int = 1, minimumAngleDegrees: float | None = None):
  return await callBridge(context, "bevelEdges", {"objectName": objectName, "selector": selector, "width": width, "segments": segments, "minimumAngleDegrees": minimumAngleDegrees})


@guardedTool(description="Subdivide the selected faces of a mesh with `cuts` cuts per edge, adding detail where it is needed." + caveFacesHelp + selectorHelp)
async def subdivide(context: Context, objectName: str, cuts: int, selector: dict = allSelector):
  return await callBridge(context, "subdivide", {"objectName": objectName, "selector": selector, "cuts": cuts})


@guardedTool()
async def booleanCut(context: Context, objectName: str, cutterName: str, operation: str = "DIFFERENCE", keepCutter: bool = False):
  """Apply a boolean (DIFFERENCE, UNION, INTERSECT) of a cutter mesh to a mesh, for cave mouths and openings; the cutter is deleted
  unless keepCutter. The faces the cut makes (madeFaces) take the material and surfacing of the nearest face the cutter crosses (a cave
  cut into a cliff is lined with the cliff's material) and are box-mapped at that material's density on the mesh (mappedFaces, or
  unmappedFaces with why; on a surfaced mesh, as the mapping beneath its layers' transitions); no material slot is added. A cutter that crosses none of the mesh's faces is refused. A DIFFERENCE that keeps
  none of the cutter's faces left an opening with nothing lining it, through an open surface that encloses nothing (a terrain sheet, a
  plane), and its result carries a warning saying so. Refused on a mesh holding caves (cutCave): take each back (removeCave), cut, and
  cut it again."""
  return await callBridge(context, "booleanCut", {"objectName": objectName, "cutterName": cutterName, "operation": operation, "keepCutter": keepCutter})


@guardedTool()
async def decimate(context: Context, objectName: str, ratio: float):
  """Reduce a mesh to roughly `ratio` (0 to 1) of its triangles; refused on a mesh holding caves (cutCave): take each back (removeCave),
  decimate, and cut it again."""
  return await callBridge(context, "decimate", {"objectName": objectName, "ratio": ratio})


@guardedTool()
async def cleanupMesh(context: Context, objectName: str, mergeDistance: float = 0.01, recalculateNormals: bool = True):
  """Merge vertices closer than mergeDistance, dissolve degenerate geometry, and make face normals consistent; refused on a mesh holding
  caves (cutCave): take each back (removeCave), clean up, and cut it again."""
  return await callBridge(context, "cleanupMesh", {"objectName": objectName, "mergeDistance": mergeDistance, "recalculateNormals": recalculateNormals})


@guardedTool(description=(
  "Triangulate the selected part of a terrain along its contours: split its quads into triangles and turn each cell's diagonal to the"
  " one with the smaller height step, never leaving a sliver, so ledges and cliff edges that cross the grid run as clean lines instead"
  " of notching where they step to the next row. carve and fill do this where they shape; use it after warp, roughen, or changing"
  " passes. Vertices never move, so shaping passes keep what they hold; cells whose two triangles differ in material or surfacing keep"
  " their diagonal, and cells at a cave (cutCave) stay as its cut left them." + selectorHelp))
async def followContours(context: Context, objectName: str, selector: dict = allSelector):
  return await callBridge(context, "followContours", {"objectName": objectName, "selector": selector})


@guardedTool()
async def placeLights(context: Context, lights: list[dict], collection: str | None = "lights"):
  """Place zone lights: point lights [{name, position [x,y,z], color [r,g,b] 0-1 as the client stores it, radius (reach in units)}] in
  `collection`. exportZone writes them into the zone's .zon, and client-shaded views draw them as the client does
  (docs/clientRendering.md, Point lights): each adds its color, falling off as 1 - (distance / radius)^2 and by how squarely a surface
  faces it, to surfaces within its radius. Terrain takes only lights whose name's third letter is B (LIB_), the client's mark for
  lights on geometry with baked light; placed objects and characters take every light. Each draw takes the three that score highest. The
  asset catalog's light styles (findAssets kind light) give the colors and radii client zones use for torches, braziers, fill light, and
  the rest."""
  return await callBridge(context, "placeLights", {"lights": lights, "collection": collection, "clientContent": None})


@guardedTool()
async def placeEmitters(context: Context, emitters: list[dict], collection: str | None = "emitters"):
  """Place particle emitters: [{name, position [x,y,z], definition (the client emitter definition index), lifespan (the list's lifespan
  field; 4000000 on most of the client's emitters)}] as empties in `collection`. exportZone writes them to <zone>_EnvironmentEmitters.txt
  beside the archive, and client-shaded views draw their particles as they stand at a moment of the client's steady state, from its
  EnvironmentEmittersNew.edd (docs/clientRendering.md, Particle emitters); the client makes no emitter whose lifespan is 0. The asset
  catalog (findAssets kind emitter) says what each definition shows and under which names client zones place it."""
  return await callBridge(context, "placeEmitters", {"emitters": [emitter | {"alwaysVisible": None} for emitter in emitters], "collection": collection, "clientContent": None})


@guardedTool()
async def createRegion(context: Context, name: str, outline: list[list[float]], bottom: float, top: float, intent: str, access: str):
  """Mark an area of the zone for what it is to become: a vertical prism over `outline` ([[x, y], ...], in order) from `bottom` to
  `top`, kept in the regions collection (seen in Blender, never rendered or exported) with its `intent` ("north guild terrace: packed
  earth, dwellings carved into the back wall") and its `access`, whether players reach it: "play" (they walk or swim there; walking
  against swimming is the swim volumes' decision), "view" (seen but never entered: a backdrop slope, a far shore), or "none" (never
  reached: behind the rim, rock interiors, under the world). A zone is planned as regions first and each is shaped, surfaced, and
  dressed for its own intent; the {"region": name} selector confines any tool to one."""
  return await callBridge(context, "createRegion", {"name": name, "outline": outline, "bottom": bottom, "top": top, "intent": intent, "access": access})


@guardedTool()
async def editRegion(
  context: Context, name: str, outline: list[list[float]] | None = None, bottom: float | None = None, top: float | None = None, intent: str | None = None,
  access: str | None = None,
):
  """Change a region's outline, bottom, top, intent, or access (play, view, or none) as the plan changes."""
  return await callBridge(context, "editRegion", {"name": name, "outline": outline, "bottom": bottom, "top": top, "intent": intent, "access": access})


sketchShapeHelp = (
  " A shape is {name, kind, ...}: an area or a footprint takes an outline [[x, y], ...] or a rectangle {center: [x, y], size: [across,"
  " along], headingDegrees} (its facing; along runs that way), and may take floor (the height it is graded to) and facingDegrees;"
  " a footprint may take height, and then stands in views (with guides on) as a plain block that height above its floor (or the"
  " ground), on a plinth down to the lowest ground under it; a path takes points [[x, y] or [x, y, z], ...] and may take width; a point takes at [x, y] and may take facingDegrees; a note"
  " takes at and a label, its text. Any shape may take a label and a note."
)


@guardedTool(description=(
  "Sketch on a named sheet to think a layout through before or while building it: where buildings stand and face, the yards and"
  " plazas between them, streets and stairs, landmarks, the arrival point, rooms of a dungeon, a village's lanes. Shapes are added,"
  " or redrawn when a name is reused; each comes back measured against the ground under it: an area or footprint's size, the ground's"
  " lowest, mean, and highest height and steepest slope under it, cut and fill to its floor, how much of it lies under water, which"
  " shapes on the sheet it overlaps, and the nearest one and the gap to it; a path's length, its ground heights, and its steepest"
  " grade, and the shapes it meets; a point's ground height. Sketches stay in the file on their sheets and can be revised, but they are"
  " aids, not a plan: nothing holds the zone to them, export leaves them out, and players do not stand on them. Look at them with"
  " renderSketch." + sketchShapeHelp
))
async def sketch(context: Context, sheet: str, shapes: list[dict]):
  return await callBridge(context, "sketchShapes", {"sheet": sheet, "shapes": shapes})


@guardedTool()
async def eraseSketch(context: Context, sheet: str, names: list[str] | None = None, wholeSheet: bool = False):
  """Erase sketch shapes by name from a sheet, or the whole sheet with wholeSheet true."""
  return await callBridge(context, "eraseSketch", {"sheet": sheet, "names": names, "wholeSheet": wholeSheet})


@guardedTool()
async def getSketch(context: Context, sheet: str | None = None):
  """A sketch sheet's shapes (or every sheet's) as drawn, each measured against the ground under it now, as sketch reports them."""
  return await callBridge(context, "getSketch", {"sheet": sheet})


@guardedTool()
async def renderSketch(
  context: Context, center: list[float], width: float, sheets: list[str] | None = None,
  layers: list[str] = ["regions", "plots", "water", "boundaries", "zoneLines", "entries"], bandHeight: float = 25.0, spotHeights: bool = True,
):
  """Draw a plan: the zone from straight above in quiet grey relief (lighter higher, a step every bandHeight units, slopes shaded
  from the game's northwest), `width` units across (along y) about `center`, the game's north (+X) up and east (-Y) right as the
  in-game map draws, with a coordinate grid (the ground's height written at each
  crossing unless spotHeights is false, giving way wherever a label must stand on it), a scale bar, the sketch sheets (all, or
  those named) in their own colors (areas dashed and faintly filled, footprints filled with their facing arrows and heights, paths at
  their widths, points, notes; every shape shows, inside an area or under another sheet's), and the plan's own layers: regions
  (dashed, named, filled by access: view hatched amber, none hatched red, undecided hatched grey, play outlined alone), entries (where
  players arrive, getEntries: dark arrows from the point toward the heading, hollow when isolated, a ring where a teleport keeps the
  player's heading, labelled S for the safe point, zoneIn with the zone it comes from, landing with its name, T<number> for a teleport's
  landing, and entrance with its plot), plots (outlined, by address, with a mark pointing out of the entrance side), water (as players see it, not where a
  surface runs on tucked under its banks: blue, lava orange), swim (the swim volumes, dashed, cyan water and magenta lava, each
  named inside itself where its name fits clear of the other labels, else by its number; `unnamedSwimVolumes` lists those in the
  drawing left unnamed, to see closer), boundaries (red, named: walls as lines along their foot, lids and floors faintly filled), and
  zoneLines (their boxes faintly green, named). Labels are placed clear of one another, and beside the spot heights rather than over
  them where they can be; an area is named only where it reaches into the drawing."""
  if len(center) != 2 or width <= 0:
    raise ToolError(f"center is [x, y] and width positive, got {center} and {width}")
  zone = await callBridge(context, "getZoneProperties", {})
  basePath = newRenderPath()
  base = await callBridge(context, "renderView", {
    "view": {"map": {"center": center, "width": width}}, "outputPath": str(basePath), "figureModel": None, "shading": "relief",
    "bandHeight": bandHeight, "guides": False, "sky": await zoneSky(zone), "swimVolumes": False, "labels": None, "emitters": None,
  })
  spots = planDrawing.gridCrossings(center, width, base["height"] / base["width"]) if spotHeights else []
  overlays = await callBridge(context, "planOverlays", {"sheets": sheets, "layers": layers, "spots": spots})
  outputPath = newRenderPath()
  drawn = await anyio.to_thread.run_sync(planDrawing.drawPlan, basePath, outputPath, center, width, overlays)
  basePath.unlink()
  return [Image(data=outputPath.read_bytes(), format="png"), {
    "outputPath": str(outputPath), "center": center, "width": width, "unitsPerPixel": round(width / drawn["size"][0], 4), "heightRange": base["heightRange"],
    "bandHeight": bandHeight, "gridStep": drawn["gridStep"], "sheetColors": drawn["sheetColors"],
    "shapes": {sheet["sheet"]: len(sheet["shapes"]) for sheet in overlays["sheets"]}, "unnamedSwimVolumes": drawn["unnamedSwimVolumes"],
  }]


@guardedTool()
def compareRenders(before: str, after: str, beforeLabel: str = "before", afterLabel: str = "after"):
  """Two renders of the same view (renderView's outputPath before and after a change) side by side, with a change map: the after view
  dimmed and every changed pixel in red; and how much of the view changed and where (pixel bounds [left, top, right, bottom]).
  Identical input renders byte-identical, so red marks real change, as long as both used one camera: render the second with the
  first's eye and target (a frame view re-frames on objects that changed size, and a standAt view stands on ground that moved)."""
  outputPath = newRenderPath().with_suffix(".jpg")
  try:
    compared = viewSheets.compareSheet(before, after, outputPath, [beforeLabel, afterLabel])
  except ValueError as error:
    raise ToolError(str(error)) from error
  return [Image(data=outputPath.read_bytes(), format="jpeg"), {"outputPath": str(outputPath)} | compared]


@guardedTool()
async def renderOrbit(
  context: Context, objects: list[str], pitchDegrees: float = -25.0, views: int = 8, shading: str = "client", guides: bool = True,
):
  """Look all the way round named objects: `views` frame views (2 to 12) at headings evenly round them, the first looking along +Y (the game's west),
  from pitchDegrees (negative looks down), each framed so the objects fit, on one sheet labeled by heading. For judging a form from
  every side, and which sides need work."""
  if not 2 <= views <= 12:
    raise ToolError(f"views is 2 to 12, got {views}")
  zone = await callBridge(context, "getZoneProperties", {})
  sky = await zoneSky(zone)
  emitters = await previewEmitterAssets()
  cells = []
  for index in range(views):
    heading = 360 * index / views
    outputPath = newRenderPath()
    await callBridge(context, "renderView", {
      "view": {"frame": {"objects": objects, "headingDegrees": heading, "pitchDegrees": pitchDegrees}}, "outputPath": str(outputPath),
      "figureModel": None, "shading": shading, "bandHeight": 50.0, "guides": guides, "sky": sky, "swimVolumes": False, "labels": None,
      "emitters": emitters,
    })
    cells.append((viewSheets.openRender(outputPath), f"looking {heading:g} degrees"))
  sheetPath = newRenderPath().with_suffix(".jpg")
  size = viewSheets.writeGrid(cells, 4 if views > 4 else views, sheetPath)
  return [Image(data=sheetPath.read_bytes(), format="jpeg"), {"outputPath": str(sheetPath), "views": views} | size]


# A sheet holds at most this many views, so it stays well inside what a tool result can carry.
sheetViews = 16
sheetColumns = 4


async def renderSheet(context, zone, views, labels, shading, figureModel):
  """Render each view (with progress), lay them out on one sheet labeled under each, and return the sheet and the renders' paths."""
  sky = await zoneSky(zone)
  emitters = await previewEmitterAssets()
  cells, paths = [], []
  for index, (view, label) in enumerate(zip(views, labels)):
    await context.report_progress(index, len(views), f"rendering {index + 1} of {len(views)}")
    outputPath = newRenderPath()
    await callBridge(context, "renderView", {
      "view": view, "outputPath": str(outputPath), "figureModel": figureModel, "shading": shading, "bandHeight": 50.0, "guides": True, "sky": sky,
      "swimVolumes": False, "labels": None, "emitters": emitters,
    })
    cells.append((viewSheets.openRender(outputPath), label))
    paths.append(str(outputPath))
  await context.report_progress(len(views), len(views), "laying out the sheet")
  sheetPath = newRenderPath().with_suffix(".jpg")
  size = viewSheets.writeGrid(cells, min(sheetColumns, len(cells)), sheetPath)
  return Image(data=sheetPath.read_bytes(), format="jpeg"), {"outputPath": str(sheetPath)} | size, paths


@guardedTool()
async def saveReviewCamera(context: Context, name: str, view: dict, note: str):
  """Keep a named review camera to look at the zone the same way again: the pose of a renderView view, {"eye", "target"},
  {"standAt", "headingDegrees", "pitchDegrees"} (figureAt optional; where the scale figure stood is kept with it), or {"frame"}, with
  `note`, what to judge from there. It is a camera guide in the reviewCameras collection, kept in the .blend, never drawn in views and
  never exported; saved again under its name it moves to the new view. renderView {"camera": name} and renderReviewSet render it."""
  zone = await callBridge(context, "getZoneProperties", {})
  return await callBridge(context, "saveReviewCamera", {"name": name, "view": view, "note": note, "sky": await zoneSky(zone), "figureModel": await scaleFigureModel(zone, view)})


@guardedTool()
async def getReviewCameras(context: Context):
  """The review cameras by name: each one's note, the view it was saved from, its eye, heading, and pitch, where its scale figure stands,
  and the concept art it was matched to (compareToConcept saveAs: the art's path, the frame size, and the vertical field of view)."""
  return await callBridge(context, "getReviewCameras", {})


@guardedTool()
async def deleteReviewCameras(context: Context, names: list[str]):
  """Delete the named review cameras; refuses, deleting none, when a name is not a review camera."""
  return await callBridge(context, "deleteReviewCameras", {"names": names})


@guardedTool()
async def renderReviewSet(context: Context, names: list[str] | None = None, shading: str = "client"):
  """Render review cameras (all of them by name, or those named, in that order; up to 16) as renderView {"camera": name} does, in any
  renderView shading, and lay them out on one sheet, each labeled with its name and note; the result gives each render's path."""
  cameras = {camera["name"]: camera for camera in (await callBridge(context, "getReviewCameras", {}))["cameras"]}
  chosen = list(cameras) if names is None else names
  unknown = sorted(set(chosen) - set(cameras))
  if unknown or not chosen or len(chosen) > sheetViews:
    raise ToolError(f"Name up to {sheetViews} review cameras to render, each one saved (saved: {sorted(cameras)}); got {chosen}")
  zone = await callBridge(context, "getZoneProperties", {})
  sheet, size, paths = await renderSheet(
    context, zone, [{"camera": name} for name in chosen], [f"{name}: {cameras[name]['note']}" for name in chosen], shading, await zoneFigureModel(zone),
  )
  return [sheet, size | {"cameras": [{"name": name, "note": cameras[name]["note"], "outputPath": path} for name, path in zip(chosen, paths)]}]


@guardedTool()
async def compareToConcept(
  context: Context, camera: str | None = None, concept: str | None = None, view: dict | None = None,
  verticalFieldOfViewDegrees: float = 46.5, saveAs: str | None = None, note: str | None = None, guides: bool = True,
):
  """Compare concept art with the zone seen as the art sees it, as an artist checks work against the art: the zone rendered at the art's
  aspect with a vertical field of view of verticalFieldOfViewDegrees (the client's is 46.5), beside the art; the art at half over the
  render with the render's eye level drawn, for matching the camera to the art's horizon and framing; both squinted into their dark,
  middle, and light thirds, with where the render's masses are lighter or darker; their strongest edges (the big forms' outlines) over each
  other; and their palettes. The numbers: the share of the picture in the same third and the masses' correlation, the share of each one's
  edges the other meets, each palette's color difference (CIE76 dE) from the other's, and each one's lightness, its spread, chroma, and
  warmth (red-green, yellow-blue). Try a camera with concept (the art's path) and view (an {eye, target}, {standAt, headingDegrees,
  pitchDegrees}, or {frame} view as renderView takes them; no scale figure is drawn) and move it until the art and the render line up;
  keep it with saveAs (a name) and note (what to judge from it): a review camera that holds the art's path (relative to the .blend once it
  is saved), its frame, and its field of view, which renderView {"camera": name}, renderReviewSet, and compareToConcept camera render as
  matched. compareToConcept camera compares a kept one with its art again."""
  if camera is not None:
    if concept is not None or view is not None or saveAs is not None or note is not None:
      raise ToolError("compareToConcept takes a kept camera alone, or concept and view (with saveAs and note to keep the camera)")
    cameras = {entry["name"]: entry for entry in (await callBridge(context, "getReviewCameras", {}))["cameras"]}
    if camera not in cameras or cameras[camera]["concept"] is None:
      matched = sorted(name for name, entry in cameras.items() if entry["concept"] is not None)
      raise ToolError(f"'{camera}' is not a review camera matched to concept art; those are {matched}")
    kept = cameras[camera]["concept"]
    conceptPath, fieldOfView, renderedView, frame = kept["path"], kept["verticalFieldOfViewDegrees"], {"camera": camera}, None
  else:
    if concept is None or view is None:
      raise ToolError("compareToConcept needs a kept camera, or concept (the art's path) and view")
    if not isinstance(view, dict) or {"camera", "map"} & set(view):
      raise ToolError(f"Try a camera on the art with an {{eye, target}}, {{standAt, headingDegrees, pitchDegrees}}, or {{frame}} view, got {view!r}")
    if (saveAs is None) != (note is None):
      raise ToolError("Keep a matched camera with both saveAs (its name) and note (what to judge from it)")
    conceptPath = str(Path(concept).resolve())
    try:
      size = conceptComparison.renderSize(conceptPath)
    except (ValueError, OSError) as error:
      raise ToolError(f"The concept art cannot be read: {error}") from error
    fieldOfView, renderedView = verticalFieldOfViewDegrees, view
    frame = {"size": size, "verticalFieldOfViewDegrees": fieldOfView}
  zone = await callBridge(context, "getZoneProperties", {})
  sky = await zoneSky(zone)
  renderPath = newRenderPath()
  described = await callBridge(context, "renderView", {
    "view": renderedView, "outputPath": str(renderPath), "figureModel": None, "shading": "client", "bandHeight": 50.0, "guides": guides,
    "sky": sky, "swimVolumes": False, "labels": None, "emitters": await previewEmitterAssets(), "frame": frame,
  })
  sheetPath = newRenderPath().with_suffix(".jpg")
  try:
    compared = await anyio.to_thread.run_sync(conceptComparison.compare, conceptPath, str(renderPath), str(sheetPath), described["forward"], fieldOfView)
  except (ValueError, OSError) as error:
    raise ToolError(f"The concept art cannot be read: {error}") from error
  keptCamera = None
  if saveAs is not None:
    keptCamera = await callBridge(context, "saveReviewCamera", {
      "name": saveAs, "view": view, "note": note, "sky": sky, "figureModel": None, "concept": {"path": conceptPath} | frame,
    })
  forward, width, height = described["forward"], described["width"], described["height"]
  return [Image(data=sheetPath.read_bytes(), format="jpeg"), {
    "outputPath": str(sheetPath), "renderPath": str(renderPath), "concept": conceptPath, "eye": described["eye"], "forward": forward,
    "headingDegrees": round(math.degrees(math.atan2(forward[0], forward[1])) % 360, 2),
    "pitchDegrees": round(math.degrees(math.asin(max(-1.0, min(1.0, forward[2])))), 2),
    "frameSize": [width, height], "verticalFieldOfViewDegrees": fieldOfView,
    "horizontalFieldOfViewDegrees": round(math.degrees(2 * math.atan(math.tan(math.radians(fieldOfView) / 2) * width / height)), 2),
    "keptCamera": keptCamera,
  } | compared]


@guardedTool()
async def saveReviewRoute(context: Context, name: str, path: list[list[float]]):
  """Keep a named review route: a polyline [[x, y, z], ...] (heights within a step of the ground, as walkRoute takes them) in the
  reviewRoutes collection, kept in the .blend, never drawn in views and never exported; saved again under its name it takes the new
  path. walkRoute and renderRouteStrip take its name as `route`."""
  return await callBridge(context, "saveReviewRoute", {"name": name, "path": path})


@guardedTool()
async def getReviewRoutes(context: Context):
  """The review routes by name, each with its points and its length in plan."""
  return await callBridge(context, "getReviewRoutes", {})


@guardedTool()
async def deleteReviewRoutes(context: Context, names: list[str]):
  """Delete the named review routes; refuses, deleting none, when a name is not a review route."""
  return await callBridge(context, "deleteReviewRoutes", {"names": names})


def problemText(problem):
  """A walk's problem in a few words, for a frame's label."""
  kind = problem["kind"]
  if kind == "drop":
    text = "drop: no footing within 60 below"
  elif kind == "rise":
    text = "rise: over 60 high" if problem["height"] is None else f"rise {problem['height']:g} high"
  elif kind == "blocked":
    text = "blocked by a boundary"
  elif kind == "steep":
    text = f"steep climb, {problem['steepestDegrees']:g} degrees"
  else:
    text = f"headroom {problem['lowest']:g}"
  if "resumesAt" in problem:
    text += "; the walk never finds footing again" if problem["resumesAt"] is None else f"; walking on from {problem['resumesAtDistance']:g}"
  return text


@guardedTool()
async def renderRouteStrip(context: Context, spacing: float, route: str | None = None, path: list[list[float]] | None = None):
  """walkRoute as a strip of eye-level frames: walks a saved review route or a bridge's, flight's, or walkway's walk line (route) or a
  path [[x, y, z], ...] as walkRoute does, and
  renders a frame every `spacing` along it in plan (from its start) and one where each problem the walk meets starts, each standing
  where the walk stands there (eye 5.5 over the footing; no scale figure), heading along the route and pitched toward where the walk
  stands 30 further on, or toward the brink where it stops if sooner (a problem's frame looks past the problem: down past the brink of a
  drop), laid out on one sheet in order along the route, each labeled with its distance and any problem (up to 16 frames). Places the
  walk cannot reach, between a stop and where it walks on, get no frame (stationsNotStoodOn); the problem's frame shows why. The result
  gives the walk (length in plan, walkable, problems, oneWay) and each frame's distance, view (renderView renders it, adding the scale
  figure), problem, and render path."""
  planned = await callBridge(context, "planRouteStrip", {"path": path, "route": route, "spacing": spacing})
  frames, problems, length = planned["frames"], len(planned["problems"]), planned["length"]
  if len(frames) > sheetViews:
    room = sheetViews - problems
    advice = f"use a spacing of at least {math.ceil(length / (room - 1))}" if room > 1 else "strip a shorter part of the route"
    raise ToolError(f"{len(frames)} frames ({len(frames) - problems} every {spacing:g} along {length:g} and {problems} at problems) are more than {sheetViews} on a sheet; {advice}")
  views = [{"standAt": frame["standAt"], "headingDegrees": frame["headingDegrees"], "pitchDegrees": frame["pitchDegrees"]} for frame in frames]
  labels = [f"{frame['distance']:g} of {length:g}" + ("" if frame["problem"] is None else f": {problemText(frame['problem'])}") for frame in frames]
  sheet, size, paths = await renderSheet(context, await callBridge(context, "getZoneProperties", {}), views, labels, "client", None)
  return [sheet, size | {key: planned[key] for key in ("route", "length", "spacing", "walkable", "problems", "oneWay", "stationsNotStoodOn")} | {
    "frames": [{"distance": frame["distance"], "view": view, "problem": frame["problem"], "outputPath": path} for frame, view, path in zip(frames, views, paths)],
  }]


@guardedTool()
async def renderSection(
  context: Context, start: list[float], end: list[float], bottom: float, top: float,
  layers: list[str] = ["ground", "water", "swim", "massing", "sketch", "plots", "boundaries", "zoneLines"],
):
  """Draw a section: where the vertical plane through the line from start [x, y] to end [x, y] cuts the zone, seen from the line's right
  so start is on the left, from bottom to top at one scale across and up, clipped to that frame: the ground players stand on (brown;
  caves, overhangs, and arches show as the shapes they are), water surfaces where players see them (blue; not where they run on
  tucked under the banks), swim volumes (dashed boxes, cyan water and magenta lava), sketch massing (grey), sketch areas' floors
  (dashed) and paths in their sheets' colors (a path's rise and fall where it runs along the line, level across its width where it
  crosses it), plot pads (orange, level at the plot's height across its footprint, with a mark on its entrance side pointing out),
  boundaries (red: a wall from under the ground to its top, a lid or a floor at its height), and zone lines (green boxes), each named
  just above, with a height grid and the distance along the line. For judging what plans cannot show: swim volumes against the
  surface and the bed, a cave's headroom, a plot's pad against the slope, stacked floors and the stairs between them, an arch's span,
  a wall's height over the ground and a lid's over a path. The result also gives the cuts as numbers (s along the line, z height)."""
  if len(start) != 2 or len(end) != 2:
    raise ToolError(f"start and end are [x, y], got {start} and {end}")
  cuts = await callBridge(context, "sectionCuts", {"start": start, "end": end, "bottom": bottom, "top": top, "layers": layers})
  outputPath = newRenderPath()
  drawn = await anyio.to_thread.run_sync(planDrawing.drawSection, outputPath, cuts, start, end, bottom, top)
  summary = {
    "outputPath": str(outputPath), "length": cuts["length"], "gridStep": drawn["gridStep"], "unitsPerPixel": drawn["unitsPerPixel"],
    "groundSegments": len(cuts["ground"]),
    "water": [{"name": entry["name"], "levels": [min(min(z0, z1) for _, z0, _, z1 in entry["segments"]), max(max(z0, z1) for _, z0, _, z1 in entry["segments"])]} for entry in cuts["water"]],
    "swim": cuts["swim"],
    "massing": [{"name": entry["name"], "label": entry["label"]} for entry in cuts["massing"]],
    "sketch": [{"sheet": entry["sheet"], "shape": entry["shape"], "pieces": entry["pieces"]} for entry in cuts["sketch"]],
    "plots": [{key: entry[key] for key in ("name", "s", "z", "entrance")} for entry in cuts["plots"]],
    "boundaries": [{"name": entry["name"], "kind": entry["kind"], "segments": entry["segments"]} for entry in cuts["boundaries"]],
    "zoneLines": cuts["zoneLines"],
  }
  return [Image(data=outputPath.read_bytes(), format="png"), summary]


@guardedTool()
async def buildSwimVolumes(context: Context, body: str, area: dict | None = None, replaceEdited: bool = False):
  """Starting swim volumes for a pool or river: boxes over its grid where its surface covers water over the bed, grouped while their
  tops stay within a unit of the surface, bottoms 4 under the deepest bed below them (stopping a unit over any open space under the
  bed, a cave below the water). They are named for the .zon regions they become (AWT_ water, ALV_ lava) and kept in "swimVolumes" as
  boxes to look at and adjust by hand (transformObjects, duplicateObjects, deleteObjects; placeSwimVolume adds one). area ({"circle":
  ...} or {"polygon": ...}) builds only inside it. A rebuild replaces the body's generated boxes there and refuses over boxes edited or
  placed by hand unless replaceEdited. The result gives the body's state and findings (surface left uncovered, a top away from the
  surface, ground rising to the top inside a box, a box mostly without the body's water over it): things to look at, never errors,
  since a box need not match a surface."""
  return await callBridge(context, "buildSwimVolumes", {"body": body, "area": area, "replaceEdited": replaceEdited})


@guardedTool()
async def placeSwimVolume(context: Context, name: str, liquid: str, minimum: list[float], maximum: list[float], body: str | None = None):
  """Place a swim volume by hand from its corners [x, y, z], square to the axes: water or lava, named <prefix><name> (AWT_ or ALV_
  added), tied to a pool or river (body; not one marked not swimmable) or standing alone: a floating pool, a cove, a pool without a
  surface. Adjust it like any box."""
  return await callBridge(context, "placeSwimVolume", {"name": name, "liquid": liquid, "minimum": minimum, "maximum": maximum, "body": body})


@guardedTool()
async def acceptSwimVolumes(context: Context, body: str):
  """Mark a body's swim volumes as made for the body as it now is: after its water or its bed changed and the boxes were looked at again
  (renderView with swimVolumes, renderSketch's swim layer). Hand edits stay as they are."""
  return await callBridge(context, "acceptSwimVolumes", {"body": body})


@guardedTool()
async def getSwimVolumes(context: Context, name: str | None = None):
  """The swim volumes (or one by name), each with its liquid, body, corners, and whether it was edited or placed by hand; every pool and
  river's swim state (boxed; changed since its boxes were accepted; notSwimmable, as editWater's swimmable false marks a fountain or a
  trickle; undecided) with its findings (as buildSwimVolumes gives them: a box left over a drained or moved basin is mostly without
  water over it); and any errors a zone file cannot hold (a box turned or without size, names that clash once lowercased, a prefix
  against its liquid, a body gone, a box of a body marked not swimmable), which export refuses."""
  return await callBridge(context, "getSwimVolumes", {"name": name})


@guardedTool()
async def placeBoundaryWall(context: Context, name: str, path: list[list[float]], height: float):
  """Place an invisible wall players cannot pass, as the client's own walls run: a ribbon along `path` [[x, y], ...] from 5 under the
  ground players stand on to `height` above it, following the ground every 4 units; a path ending where it began closes into a ring.
  It faces to the left of the path's direction (inward for a ring drawn counterclockwise), and export writes it facing both ways, so it
  blocks from either side whichever way it faces. Boundaries are never drawn in the client's view, block walkRoute, tint views with
  guides red in every shading and draw red in plans and sections, and export as triangles without a material in the terrain, which the
  client collides with but never draws. Place it again by name to redraw it against the ground as it is now; deleteObjects
  removes it. Walls go at the foot of a rim players should not climb, along an open edge, across a gap beside a zone line."""
  return await callBridge(context, "placeBoundaryWall", {"name": name, "path": path, "height": height})


@guardedTool()
async def placeBoundaryPlane(context: Context, name: str, kind: str, outline: list[list[float]], height: float):
  """Place a flat invisible boundary over a closed `outline` [[x, y], ...] at `height`: kind "lid" (facing down: over a canyon path or a
  gap, so players cannot climb or float out over the rim) or "floor" (facing up: under a drop players must not fall out of the world
  through). Like a wall it is never drawn, blocks walkRoute, shows red with guides and in plans and sections, and exports into the
  terrain without a material, facing as it faces in the scene.
  Place it again by name to redraw it."""
  return await callBridge(context, "placeBoundaryPlane", {"name": name, "kind": kind, "outline": outline, "height": height})


@guardedTool()
async def markPassable(context: Context, objects: list[str], passable: bool = True):
  """Mark meshes or collection instances players pass through (art that carries its own collision shell, hanging moss, a bead curtain),
  or take the mark off with passable false. Export flags every triangle of a marked object 0x1, which the client lets players through,
  as it flags liquid surfaces (water, waterfall, lava) and cutout (alpha-tested) cards on its own; walkRoute, standAt, spot heights,
  and every other lookup for the ground players stand on pass through them all."""
  return await callBridge(context, "markPassable", {"objects": objects, "passable": passable})


@guardedTool()
async def getBoundaries(context: Context):
  """Every boundary (walls with their path, height, and length; lids and floors with their outline, height, and area; those an imported
  zone archive brought), each with its triangles and world bounds; every object marked passable with its triangles; and what export
  cannot merge into the terrain."""
  return await callBridge(context, "getBoundaries", {})


@guardedTool()
async def placeZoneLine(context: Context, number: int, label: str, minimum: list[float], maximum: list[float], target: dict):
  """Place a zone line: an axis-aligned box from corner `minimum` to `maximum` [x, y, z] named ATP_<number>_<label>, the .zon region the
  client zones players through when they enter it (it reads the number right after ATP_; the server's zone_points row for it is
  number x 10). target is where it leads: {zone, x, y, z, headingDegrees}, zone a short name (this zone's own for a same-zone teleport
  or a fall catcher), x, y, z in the zone file's axes (as /loc prints them, y, x, z of the server's), headingDegrees 0 = +Y,
  clockwise; each coordinate and the heading may be "keep" (the player's own). Zone lines sit in gaps of the boundary walls, tint
  views with guides green in every shading, draw green in plans and sections, and export as ATP_ regions; the server's rows are not written yet. A line placed
  with a number already in use replaces that line; adjust a box with transformObjects, remove it with deleteObjects."""
  if not isinstance(target, dict) or not isinstance(target.get("zone"), str) or not eqgExport.zoneNamePattern.match(target["zone"]):
    raise ToolError(f"target zone is a zone short name, lowercase letters and digits, got {target.get('zone') if isinstance(target, dict) else target!r}")
  return await callBridge(context, "placeZoneLine", {"number": number, "label": label, "minimum": minimum, "maximum": maximum, "target": target})


@guardedTool()
async def getZoneLines(context: Context):
  """Every zone line (its name, number, label, corners, and target; those an imported archive brought carry no target), what a zone
  file cannot hold among them, and what a game export still needs (a target for each, each number used once)."""
  return await callBridge(context, "getZoneLines", {})


@guardedTool()
async def placeEntry(
  context: Context, name: str, at: list[float], headingDegrees: float, kind: str, fromZone: str | None = None, fromNumber: int | None = None,
  isolated: bool = False,
):
  """Place an entry, where players arrive, as an arrow at its footing facing headingDegrees (0 = +Y, clockwise): kind "zoneIn", where a
  neighbour's zone line lands players (fromZone, the neighbour's short name; fromNumber, that line's zone_points number, when known),
  or "landing", where a port inside the world lands them (a teleport door, an NPC port). isolated marks an area reached only by its own
  port. at [x, y] takes the highest footing there, [x, y, z] the footing from 3 above z down to 50 below it, as standAt finds it, on
  what the zone ships and the client collides with: reference zones, placed client objects, guides, and what players pass through are
  no ground. Refused, changing nothing: no footing, footing steeper than players walk or with less headroom than a player's height
  (playerScale), footing inside a zone line or a swim volume (players would arrive swimming), or under the surface of a pool or river
  whose swimming is undecided (a floating pool over the point, with its basin between, is not over it). Placing an entry's name again
  replaces it; transformObjects moves it and deleteObjects removes it, and getEntries says when one no longer stands on its footing,
  which a game export refuses. Entries are never exported as geometry. Returns the entry and its arrival view: standing there facing
  its heading, in client shading, with the scale figure ahead."""
  placed = await callBridge(context, "placeEntry", {
    "name": name, "at": at, "headingDegrees": headingDegrees, "kind": kind, "fromZone": fromZone, "fromNumber": fromNumber, "isolated": isolated,
  })
  view = {"standAt": placed["at"], "headingDegrees": placed["headingDegrees"], "pitchDegrees": 0.0}
  outputPath = newRenderPath()
  zone = await callBridge(context, "getZoneProperties", {})
  arrival = await callBridge(context, "renderView", {
    "view": view, "outputPath": str(outputPath), "figureModel": await scaleFigureModel(zone, view), "shading": "client", "bandHeight": 50.0,
    "guides": True, "sky": await zoneSky(zone), "swimVolumes": False, "labels": None, "emitters": await previewEmitterAssets(),
  })
  return [Image(data=outputPath.read_bytes(), format="png"), placed | {"arrivalView": {"outputPath": str(outputPath), "view": view, "eye": arrival["eye"], "figure": arrival["figure"]}}]


@guardedTool()
async def getEntries(context: Context):
  """Every place players arrive, stored and derived, each with its point, heading, source, and state (onFooting, or offFooting with
  the footing found there and how far off): the entries placeEntry placed (zoneIn with fromZone and fromNumber, landing, isolated);
  the safe point; the landing of each zone line whose target is this zone's shortName at a whole point (a teleport, named T<number>);
  and each plot's entrance, facing into the plot. Footing is read on what the zone ships, as placeEntry reads it. notFollowed lists
  the zone lines whose landing cannot be placed, with why (a kept coordinate, or no shortName to tell which lead back here)."""
  return await callBridge(context, "getEntries", {})


@guardedTool()
async def getRegions(context: Context):
  """Every region with its intent, access, outline, height span, and area: the zone's plan; and under `undecided` the regions made
  before access was decided, which a game export refuses until editRegion gives each its access."""
  return await callBridge(context, "getRegions", {})


@guardedTool()
async def addSurfaceLayer(context: Context, objectName: str, name: str):
  """Add a named surfacing layer on top of a mesh's others. Each face shows the topmost unmuted layer that covers it, else the material it
  had before the first layer, so surfacing is built up and revised layer by layer (a region's ground, a stratum, a path, accents) and a
  decision is taken back by erasing, muting, or removing its layer. A layered mesh takes materials only through its layers."""
  return await callBridge(context, "addSurfaceLayer", {"objectName": objectName, "name": name})


@guardedTool()
async def setSurfaceLayer(context: Context, objectName: str, name: str, muted: bool | None = None, position: int | None = None):
  """Mute or unmute a surfacing layer, or move it to `position` (0 is the bottom)."""
  return await callBridge(context, "setSurfaceLayer", {"objectName": objectName, "name": name, "muted": muted, "position": position})


@guardedTool()
async def removeSurfaceLayer(context: Context, objectName: str, name: str):
  """Remove a surfacing layer and what it painted."""
  return await callBridge(context, "removeSurfaceLayer", {"objectName": objectName, "name": name})


@guardedTool(description=(
  "Paint a material into a surfacing layer where the selector says, as an artist paints by intent: a region, a stroke along a path"
  " (nearPath with a radius), around a point (sphere), or masks combined with them. A slope or height mask inside a region is a recipe"
  " as the client's terrain ecosystems use them (rock on that region's steep ground, a band of ground at chosen heights); one hard rule"
  " across the whole zone is not. edgeNoise {featureSize, amplitude, seed} moves the painted edge along the ground by smooth noise, up"
  " to about `amplitude` units (a face further where faces are larger), so it wanders as a painted edge does instead of tracing a"
  " circle, a line, or the grid. It never breaks a selected piece apart: where the noise would pinch a narrow stroke through, the faces"
  " it took there go back, and faces it would leave touching the piece only at a corner are left out. The noise is laid out in plan:"
  " the same edgeNoise on the same selector picks the same faces again on ground left as it was (eraseSurface, clearRegion); reshaped"
  " since, its edge can land a face to either side." + caveSurfaceHelp + selectorHelp))
async def paintSurface(context: Context, objectName: str, layer: str, material: str, selector: dict, edgeNoise: dict | None = None):
  return await callBridge(context, "paintSurface", {"objectName": objectName, "layer": layer, "material": material, "selector": selector, "edgeNoise": edgeNoise})


@guardedTool(description="Erase a surfacing layer where the selector says, with edgeNoise as paintSurface takes it, so the layers beneath show again, each with its own mapping (a transition erased leaves the mapping beneath it as it was)." + caveSurfaceHelp + selectorHelp)
async def eraseSurface(context: Context, objectName: str, layer: str, selector: dict, edgeNoise: dict | None = None):
  return await callBridge(context, "eraseSurface", {"objectName": objectName, "layer": layer, "selector": selector, "edgeNoise": edgeNoise})


@guardedTool(description=(
  "Edit a surfacing layer's painted area at its edges, within the selector, `steps` faces at a time: grow it outward, shrink it inward,"
  " or smooth it (each face takes the value that holds most of the surface around its corners, itself included, which absorbs islands"
  " and rounds off ragged notches and teeth a face or two across; a straight edge stays, and the grid's own stair-steps along a smooth"
  " edge are conformSurfaceEdges' work); or clean it, as an artist picks off the stray specks they see: every speck of a material and"
  " every hole in one smaller than `minimumArea` square units, as the surface shows it with every layer, is taken over by the material it"
  " borders most. Clean only lifts this layer's own paint off (where what lies beneath shows that material, or the speck is this"
  " layer's) or fills a hole in it with its own paint; it never copies another layer's material into it, so a speck that a lower layer"
  " paints amid another lower layer's paint is left for cleaning that layer." + caveSurfaceHelp + selectorHelp))
async def editSurface(
  context: Context, objectName: str, layer: str, operation: str, steps: int = 1, selector: dict = allSelector, minimumArea: float | None = None,
):
  return await callBridge(context, "editSurface", {"objectName": objectName, "layer": layer, "operation": operation, "steps": steps, "selector": selector, "minimumArea": minimumArea})


@guardedTool(description=(
  "Bring a surfacing layer's edges onto the mesh's own edges along a smooth line, as the client's zones run material borders along"
  " edges their artists modeled: each border is evened out along its length over about `smoothing` world units (a cell or two of the"
  " mesh takes out saw teeth; more rounds bends further, and a stroke narrower than about twice the smoothing has its ends rounded"
  " back), keeping the area a closed border encloses; where two pieces of a stroke touch only at a corner, its border runs on through"
  " the corner the way it bends least, so the stroke is evened as one. The vertex nearest where each mesh edge crosses the evened line"
  " slides along that edge onto it (the farther one where the nearer serves another crossing), in every shaping pass alike and"
  " carrying its UVs, and each face near the line takes the side of it that holds most of the face. Each border edge it evens keeps"
  " the smoothing it was evened at: run again at that smoothing or less, those borders stay as they are (alreadyEvened); a larger"
  " smoothing evens them further; painting, erasing, or editing beside one makes it new to even. Borders on creases (a cliff's foot or"
  " lip) already run on modeled edges and stay; a closed piece whose outline is shorter than about three times `smoothing` is left as"
  " it is (editSurface clean takes specks off). Within the selector only; crossings no vertex could slide onto without turning a face"
  " over are left (keptInPlace). A cave's lining (cutCave) is left out unless the selector names the cave, and the vertices of a cave"
  " never slide (caveVerticesLeft)." + selectorHelp))
async def conformSurfaceEdges(context: Context, objectName: str, layer: str, smoothing: float, selector: dict = allSelector):
  return await callBridge(context, "conformSurfaceEdges", {"objectName": objectName, "layer": layer, "smoothing": smoothing, "selector": selector})


@guardedTool(description=(
  "Cut the selected faces along level lines, as an artist adds an edge loop where a material, a ledge, or a band should begin: lines of"
  " equal height at each of `levels`; with distanceFrom (a selector), lines at each distance in `levels` from the border of the"
  " faces it picks, and with onlyAbove as well, only from the stretches of that border where the faces beyond the picked ones mostly"
  " rise above them (a wall's foot, notches of ground poking up it and all, not its lip), as paintTransition's onlyAbove measures its"
  " strip; or with waterline (a pool or river), lines at each distance in `levels` in plan out of the water from where the mesh meets"
  " its surface, 0 being that waterline itself, so a bed and a wet bank band (underWater, nearWater) end on clean lines, for a sloping"
  " river too. distanceFrom and waterline exclude each other, and onlyAbove needs distanceFrom. Each crossed edge splits where the line"
  " crosses it, the new vertex placed alike in every shaping pass with UVs and surfacing paint carried over, and each crossed face"
  " splits along the line, so a height band, a stratum, or a transition strip ends on a modeled edge instead of zigzagging across the"
  " triangles. An edge a distance line crosses twice (up a wall one cell wide, both ends on the border; past a bend in a bank) is"
  " first split where it lies farthest past the level, so both crossings are cut (doubleCrossings). Refused, changing nothing, where it"
  " would cut faces at a cave (cutCave), naming it: keep the selector clear of the cave." + selectorHelp))
async def cutContours(
  context: Context, objectName: str, levels: list[float], distanceFrom: dict | None = None, waterline: str | None = None, selector: dict = allSelector,
  onlyAbove: bool = False,
):
  return await callBridge(context, "cutContours", {
    "objectName": objectName, "levels": levels, "distanceFrom": distanceFrom, "waterline": waterline, "selector": selector, "onlyAbove": onlyAbove,
  })


@guardedTool(description=(
  "Paint a transition texture where two grounds meet, as the client's zones blend one into the next: a strip `width` units wide"
  " along the border between the faces `selector` picks and those `toward` picks, on the selector's side, painted with `material`"
  " into `layer` and mapped so the texture's bottom edge lies on the border and its top `width` away, repeating along the border every"
  " worldUnitsPerRepeat units (around a closed border, the nearest whole number of repeats, so the strip has no seam); how far along the"
  " border a corner lies is spanned smoothly out from the border, so the faces over a corner it turns around (a notch of ground poking"
  " up a wall) share out the turn. For a texture that tiles across but not down, such as sand blending up into rock at a wall's foot."
  " The strip keeps its own mapping in its layer:"
  " erasing, muting, or removing it shows the mapping beneath again. Only faces lying wholly within `width`, over which the distance runs"
  " evenly and along one stretch of border, are painted; the faces the strip wants but cannot paint are counted"
  " (straddlingFaces): cut a contour at `width` first (cutContours with distanceFrom the toward faces and the same onlyAbove) so the strip"
  " ends on a modeled edge. With onlyAbove, the strip runs only from the stretches of border where the selector's faces mostly rise"
  " above the toward faces, and up from them: the foot of a wall, where rock rises from the ground, notches and all, and not the lip"
  " of a ledge, where ground ends above rock falling away." + caveSurfaceHelp + selectorHelp))
async def paintTransition(
  context: Context, objectName: str, layer: str, material: str, selector: dict, toward: dict, width: float, worldUnitsPerRepeat: float,
  onlyAbove: bool = False,
):
  return await callBridge(context, "paintTransition", {
    "objectName": objectName, "layer": layer, "material": material, "selector": selector, "toward": toward, "width": width,
    "worldUnitsPerRepeat": worldUnitsPerRepeat, "onlyAbove": onlyAbove,
  })


@guardedTool(description=(
  "Take shaping back inside the selector (usually a region): each named shaping pass, or every pass, loses what it moved there, faded"
  " out over fadeDistance from the edge so the area rejoins its surroundings. Removing is a way of adding: take an area back to its"
  " earlier form, then shape it again. Passes hold moves, not shapes: a pass kept that also moved the area (stillShapedBy) keeps its"
  " moves, so a level it raised the ground to (a fill's plateau on a hill) now stands off it by what the passes taken back lifted under"
  " it, a dished plateau where a hill is taken back; level it again (rebuildRegion height) or take that pass back too." + selectorHelp))
async def resetRegion(context: Context, objectName: str, selector: dict, passes: list[str] | None = None, fadeDistance: float = 0.0):
  return await callBridge(context, "resetRegion", {"objectName": objectName, "selector": selector, "passes": passes, "fadeDistance": fadeDistance})


@guardedTool(description=(
  "Give the selector's area (usually a region) a fresh start: mode surroundings spans its heights smoothly from the ground around it,"
  " a blank slate already joined to its surroundings; mode height levels it to `height`. Faded in over fadeDistance from the edge."
  " With shaping passes, its vertices also go back to where the base lays them out in plan, so sideways moves of earlier passes (a"
  " roughened or faceted wall, contours a fill snapped onto) leave no creases, and the change goes into the active pass; without"
  " passes nothing records where they lay, and they keep their places in plan. Its quads are split into triangles whose diagonals turn"
  " to follow the new ground (turnedDiagonals), as diagonals turned for the old shape would crease the new one, so its faces change."
  + selectorHelp))
async def rebuildRegion(context: Context, objectName: str, selector: dict, mode: str, height: float | None = None, fadeDistance: float = 0.0):
  return await callBridge(context, "rebuildRegion", {"objectName": objectName, "selector": selector, "mode": mode, "height": height, "fadeDistance": fadeDistance})


@guardedTool()
async def clearRegion(
  context: Context, region: str, terrainObject: str, shaping: str = "keep", surfacing: bool = False, objects: bool = False, fadeDistance: float = 0.0,
  edgeNoise: dict | None = None,
):
  """Take a region back to start it again, in one stroke: its surfacing erased from every layer of the terrain over the region's faces,
  or with edgeNoise {featureSize, amplitude, seed} over them as paintSurface's edgeNoise moves their edge, so passing the edgeNoise the
  region was painted with also takes back the paint that spilled past its edge, and any paint lying wholly within the noise's reach
  of the region, cut off from paint beyond it, so the spill goes even where the ground was reshaped since (without edgeNoise, the
  spill stays); its shaping kept, reset (passes taken back), or rebuilt (spanned from the ground around it), faded over
  fadeDistance; and the objects placed in it (models, lights, emitters) deleted. A cave's lining keeps its surfacing (its definition and
  the strokes kept with it paint it)."""
  return await callBridge(context, "clearRegion", {
    "region": region, "terrainObject": terrainObject, "shaping": shaping, "surfacing": surfacing, "objects": objects, "fadeDistance": fadeDistance,
    "edgeNoise": edgeNoise,
  })


def catalogTexturePath(texture):
  """A texture given as an absolute path, or as a catalog texture id (texture/<name>@<hash>), which uses the catalog's extracted file."""
  if texture is None or not texture.startswith("texture/"):
    return texture
  asset = callReportingFailures(catalog.requireAsset, texture)
  if "file" not in asset["measured"]:
    raise ToolError(f"{texture} has no readable file: {asset['measured'].get('problem')}")
  return asset["measured"]["file"]


@guardedTool()
async def createMaterial(context: Context, name: str, diffuseTexture: str, normalTexture: str | None = None, cutout: bool = False, alphaThreshold: float = 0.5, blockout: bool = False):
  """A Phase 1 material: diffuse texture, optional normal map, no shine; cutout makes the diffuse alpha a hard alpha test for foliage
  cards. blockout marks a layout stand-in (a grey to block out with): a game export refuses it on exported faces, a test export lists
  it, and coverage views draw it brown. A texture is an absolute path or a catalog texture id (texture/<name>@<hash>), which uses the
  catalog's extracted file. The material is kept in the file whether or not anything uses it yet."""
  return await callBridge(context, "createMaterial", {
    "name": name, "diffuseTexture": catalogTexturePath(diffuseTexture), "normalTexture": catalogTexturePath(normalTexture), "cutout": cutout,
    "alphaThreshold": alphaThreshold, "blockout": blockout,
  })


@guardedTool(description="Assign a material to the selected faces of a mesh without surfacing layers, adding a material slot if needed: for objects and blockout. A zone's terrain is surfaced by painting into layers (addSurfaceLayer, paintSurface)." + selectorHelp)
async def assignMaterial(context: Context, objectName: str, materialName: str, selector: dict = allSelector):
  return await callBridge(context, "assignMaterial", {"objectName": objectName, "materialName": materialName, "selector": selector})


@guardedTool(description="Project UVs onto the selected faces from world positions so one texture repeat spans `worldUnitsPerRepeat` units: `planar` along `direction`, or `box`, which projects each face along its dominant axis so steep faces do not stretch. On terrain whose layers hold transitions (paintTransition), this sets the mapping beneath them; the transitions keep theirs." + selectorHelp)
async def projectUVs(context: Context, objectName: str, method: str, worldUnitsPerRepeat: float, selector: dict = allSelector, direction: list[float] | None = None):
  return await callBridge(context, "projectUVs", {"objectName": objectName, "method": method, "worldUnitsPerRepeat": worldUnitsPerRepeat, "selector": selector, "direction": direction})


@guardedTool()
async def placeCopies(
  context: Context, source: str, copies: list[dict], collection: str | None = None, settle: bool = False, depth: float = 0.0, tiltShare: float = 0.0,
):
  """Place many linked copies of an object (sharing its mesh, or its collection for a kit instance) in one call, each with its own
  location [x, y, z], rotationDegrees [x, y, z] (Blender's XYZ order, z turning counterclockwise seen from above, x and y tilting; EQ
  models face +X), uniform scale, and optional name: exactly what an EQ placement holds. With settle, each copy is then settled onto the
  ground by its footprint as settleObjects does (its z is ignored; [x, y] will do). A copy of a placed kit piece stands upright at scale
  1 (rotationDegrees [0, 0, turn]); tilted or scaled ones are refused. Returns each copy as placed (with settleObjects' report when settled), to edit copy by copy."""
  return await callBridge(context, "placeCopies", {"source": source, "copies": copies, "collection": collection, "settle": settle, "depth": depth, "tiltShare": tiltShare})


@guardedTool()
async def generateCopies(
  context: Context, source: str, pattern: dict, jitter: dict | None = None, seed: int = 0, collection: str | None = None,
  settle: bool = True, depth: float = 0.0, tiltShare: float = 0.0,
):
  """A set of linked copies laid out by a pattern, as a starting point to edit copy by copy: {"row": {from: [x, y], to: [x, y], count
  or spacing, facing: "along"}}, {"grid": {center, size [across, along], spacing [across, along], turnDegrees}}, {"ring": {center,
  radius, count, startDegrees, facing: "out", "in", or "along"}}, or {"route": {path: [[x, y], ...], spacing, offset (to the right),
  facing: "along"}}; facing turns each copy's +X (the way EQ models face) out, in, or along. jitter varies each copy, seeded:
  turnDegrees [low, high], tiltDegrees [low, high] (each horizontal axis, either way), scale [low, high], position (a random nudge up to
  that far). Copies settle onto the ground by footprint unless settle is false; tiltShare leans them
  toward the slope instead of tilting them at random. Returns the copies as placeCopies does."""
  return await callBridge(context, "generateCopies", {
    "source": source, "pattern": pattern, "jitter": jitter, "seed": seed, "collection": collection, "settle": settle, "depth": depth, "tiltShare": tiltShare,
  })


@guardedTool()
async def settleObjects(context: Context, names: list[str], depth: float = 0.0, tiltShare: float = 0.0, onto: str | None = None):
  """Drop objects onto what lies below them by their footprint rather than their origin, from above the whole scene, or where rock lies
  over ground at their middle (a cave's ceiling, an overhang; looked for from halfway up each) from their own tops, or from just under
  the rock where a top reaches it, so one in a cave lands on its floor, not on the hill over it, even a column up to a hall's ceiling
  (one sunk into a solid with no ground under it, such as a ramp raised through it, rises onto its top): onto the ground (what
  players stand on, apart from the objects being settled), sunk to the lowest ground under the footprint so no edge floats; or onto a
  named object, resting on it with no vertex below its surface (a crate on a table, or tilted on a ramp); then `depth` lower. tiltShare
  (0 to 1) turns each that share of the way toward the slope of the ground under it, keeping its heading (and replacing any tilt it
  had); refused for a placed kit piece, which stands upright. Settling again after the ground changes puts everything back on it. Each
  result gives the ground's lowest and highest under the footprint and the object's own bottom and top."""
  return await callBridge(context, "settleObjects", {"names": names, "depth": depth, "tiltShare": tiltShare, "onto": onto})


@guardedTool()
async def placeOnSurface(context: Context, objectNames: list[str], at: list[list[float]] | None = None, alignToNormal: bool = False, surfaceObjects: list[str] | None = None, offset: float = 0.0):
  """Drop objects, with what is parented to them, onto the surface below `at` points, or below their own origins cast from just above
  their tops, so one sunk into the ground, under an overhang, or in a cave lands on the ground beneath it. They land on what players
  stand on (not water, faces players pass through, guides, regions, spawns, or doors), or only on surfaceObjects, every face of them, never on themselves, what they carry, or each
  other; optionally tilted to the surface normal keeping their heading, then lifted `offset`. To set props by their footprint,
  settleObjects drops them from above the whole scene, or from their own tops where rock lies over them."""
  return await callBridge(context, "placeOnSurface", {"objectNames": objectNames, "at": at, "alignToNormal": alignToNormal, "surfaceObjects": surfaceObjects, "offset": offset})


@guardedTool()
async def scatterInRegion(
  context: Context, sourceObject: str, region: dict, density: float, minimumSpacing: float, collection: str,
  yawRangeDegrees: list[float] = [0, 360], scaleRange: list[float] = [1, 1], alignToNormal: bool = False,
  maximumSlopeDegrees: float = 90, surfaceObjects: list[str] | None = None, castFromHeight: float | None = None, seed: int = 0,
  avoidObjects: list[str] | None = None, avoidClearance: float = 0.0,
):
  """Scatter linked copies of an object over a region ({"circle": {center, radius}} or {"polygon": [[x,y], ...]}): `density` per 10,000
  square units, at least `minimumSpacing` apart, random yaw within yawRangeDegrees, each copy's scale the source's times a factor from
  scaleRange, dropped from `castFromHeight` (default just above the scene) onto what players stand on (not water, faces players pass
  through, guides, regions, spawns, or doors; never the source) or only onto surfaceObjects, every face of them, skipped where steeper than maximumSlopeDegrees or inside or within
  avoidClearance of any avoidObjects. Without castFromHeight, refused where rock lies over ground at any of the region's points (a
  cave under a hill, an overhang), naming one: give castFromHeight, just under the rock's underside for the ground under it, or above
  the top. Deterministic for a seed."""
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
  """Link a collection marked as an asset in a kit .blend (absolute path) and place an instance of it; the result gives its size. Its
  materials draw in the preview lit as the open file's own are."""
  return await callBridge(context, "linkKitAsset", {"kitPath": kitPath, "assetName": assetName, "instanceName": instanceName, "location": location, "rotationDegrees": rotationDegrees, "scale": scale, "collection": collection})


# Kits

kitPieceHelp = (
  " A kit is a .blend of pieces (eqzones library\\kits\\<kitName>.blend, one per family): each piece a collection marked as an asset whose"
  " name starts with the kit file's stem (hpTimberPlank in hpTimber.blend), holding its meshes and its record (kind, sockets, passable,"
  " openings); its origin is its base center (the collection's instance_offset), its front faces +Y, its length runs along X and its"
  " height up Z. A socket {name, at, direction} is a point of the piece where another meets it: two placed sockets within 0.01 facing"
  " each other are joined. Client sizes: a 25 module (12.5 and 50 too), 30 a storey, about 10 thick, doors about 10 x 16; walls repeat"
  " every 11 to 12.5, floorboards 10 to 15, beams and trims 2.5 to 5."
)
facingHelp = (
  " facingDegrees is the way a placed piece's front (+Y) faces: 0 = +Y, clockwise; placements turn only about the vertical and keep"
  " scale 1, as the client's structure placements do."
)


def requireKitPath(kitPath):
  if kitPath is not None and (not os.path.isabs(kitPath) or not kitPath.lower().endswith(".blend") or not os.path.isfile(kitPath)):
    raise ToolError(f"kitPath must be an absolute path to an existing .blend (or null for the open file's own pieces), got '{kitPath}'")


@guardedTool(description=(
  "Model one kit module to exact size, as the client's kits are made: a collection `name` holding one mesh object and mesh `name`, its"
  " base center at `location` [x, y, z] in this file (so pieces lie side by side to look at) and that point its origin, marked as an"
  " asset, its record written. `kind` is wall (a box without a bottom; roles face for front and back, edge for its ends and top;"
  " sockets start and end at its base ends facing out along X, top up), floor (a slab; top, edge, under; start, end, front, back at the"
  " base), post (an upright without a bottom; side, top; base down, top up), beam (a bar; side, end; start, end), plank (a board; top for"
  " its broad faces, edge; start, end), rail (a bar; side, end; start, end; passable), ropeRail (a ribbon of two faces back to back;"
  " rope, a cutout material; start, end; passable), or cap (a block without a bottom; side, top; base down). `size` is [length, depth,"
  " height] along X, Y, Z (a ropeRail's depth exactly 0); `materials` and `worldUnitsPerRepeat` give exactly the kind's roles a material"
  " createMaterial made and a repeat, each role box-mapped in the piece's frame from its start corner (-length/2, -depth/2, 0), so a 25"
  " module at 12.5 a repeat tiles without a seam; a beam, plank, or rail runs its texture's v (the client's wood grain) along its length."
  " `passable` overrides the kind's (rails and ropes pass players, as the client's do)."
  " Refine with the mesh tools and cutOpening; markKitPiece if its sockets change. Returns the record, its size, module, triangles,"
  " each material's texture and measured repeat, each role's measured repeat, and seams: materials whose repeat does not fit the"
  " length (or height) the piece joins along a whole number of times, so a texture seam shows where two meet (reported, not refused)."
  + kitPieceHelp
))
async def createKitPiece(
  context: Context, name: str, kind: str, size: list[float], materials: dict[str, str], worldUnitsPerRepeat: dict[str, float],
  location: list[float], passable: bool | None = None,
):
  return await callBridge(context, "createKitPiece", {
    "name": name, "kind": kind, "size": size, "materials": materials, "worldUnitsPerRepeat": worldUnitsPerRepeat, "location": location, "passable": passable,
  })


@guardedTool(description=(
  "Make a collection of this file a kit piece, or change a piece's kind, sockets, origin, or passability: its origin (instance_offset)"
  " set to `origin` (file coordinates) or the base center of its meshes (the middle of their plan bounds at their lowest point),"
  " marked as an asset, its record written. `kind` is any createKitPiece kind, roof, or custom (a corner block, a portal frame, a"
  " bench; placed only); `sockets` [{name, at [x, y, z] in the piece frame, direction}] (each direction a unit vector, level or straight"
  " up or down; each within 1 unit of the piece's bounds), or the kind's defaults from its bounds (custom has none); a wall, beam, plank,"
  " rail, or ropeRail needs start and end facing apart; a corner is custom with turned sockets (a 10 x 10 block: start at (-5, 0, 0)"
  " facing -X, end at (0, 5, 0) facing +Y), and so is a curved wall section (its module null: turned sockets span no module)."
  " `passable` true marks its meshes passable, false takes the mark off, null keeps the piece's"
  " (or takes the kind's for a new piece). Every face needs a material createMaterial made and texture coordinates. Placed instances"
  " keep their transforms, so a moved origin or socket shows in them: look after. Returns the record, what is derived from it (as"
  " createKitPiece), and unmappable: faces whose texture coordinates are not affine in position within 1 percent, which map oddly where"
  " a span stretches or bends the piece." + kitPieceHelp
))
async def markKitPiece(
  context: Context, collectionName: str, kind: str, sockets: list[dict] | None = None, origin: list[float] | None = None, passable: bool | None = None,
):
  return await callBridge(context, "markKitPiece", {"collectionName": collectionName, "kind": kind, "sockets": sockets, "origin": origin, "passable": passable})


@guardedTool(description=(
  "Cut a door or window through a wall piece of this file, or a custom one (a curved wall section, modeled with the middle of its wall"
  " facing +Y), front to back along Y: centered `along` from its middle along X, its bottom `sill`"
  " over the base (a door 0, a window above 0), `width` wide with jambs `height` tall; with `archRise` its head a round arch rising"
  " that far over the jambs in `archSegments` straight pieces (a semicircle at half the width). The cut is an exact boolean of a prism"
  " through the piece; its reveal takes the material and texture density of the faces it cuts through, or with `frame` {width, depth,"
  " material, worldUnitsPerRepeat} the reveal and a band `width` wide round the opening on both faces (jambs, head, and a window's sill),"
  " standing `depth` proud of the faces the opening goes through (however many framed openings the piece already has), take the frame's"
  " material box-mapped at its repeat, as the client trims its openings. The opening and its"
  " frame leave at least 1 unit of wall at each end and above (and below a window) and overlap no opening cut before; it is recorded in"
  " the piece's openings. Zones that link the kit show it when next opened. Returns the opening's corners [x, z] in the piece frame,"
  " its clear width and height as cut, the faces made, and the triangles before and after."
))
async def cutOpening(
  context: Context, piece: str, kind: str, along: float, width: float, height: float, sill: float = 0.0, archRise: float | None = None,
  archSegments: int = 8, frame: dict | None = None,
):
  return await callBridge(context, "cutOpening", {
    "piece": piece, "kind": kind, "along": along, "width": width, "height": height, "sill": sill, "archRise": archRise, "archSegments": archSegments, "frame": frame,
  })


@guardedTool(description=(
  "Model a roof as a kit piece (kind roof) at `location` (its wall plate's middle, its origin) for a rectangular `footprint` [length X,"
  " depth Y], or for what the meshes or placed pieces of this file named in `over` stand on: measured square to them (they stand"
  " turned alike, or a quarter turn apart; others are refused, naming the turn), a placed wall by its body between its faces, not the"
  " frames standing proud of them; give exactly one. Each slope's"
  " underside passes through the footprint's edges at the plate (z 0), so it sits on walls of that footprint; slopes at `pitchDegrees`"
  " (5 to 60), running out `overhang` past every side (the eaves drop overhang x tan(pitch) under the plate), `thickness` thick square to"
  " the slope. `kind` gable: two slopes meeting at a ridge along `ridgeAlong` (\"x\" or \"y\"), gable triangles closing its ends down to"
  " the plate; hip: four slopes, the ridge along `ridgeAlong` (the longer side) shortened by hips at 45 degrees in plan, a point over a"
  " square; shed: one slope rising toward the back (-Y), its ends closed by gable triangles. Each gable or shed end is two faces on"
  " corners of their own, one looking out and one in, so a walk-in building's ends are seen from inside. Roles: roof (slope tops, mapped in their"
  " plane, u level along the eaves and v up the slope, so shingles run level), under (undersides, the same), gable (gable and shed ends,"
  " box-mapped; not hip), edge (fascia and verges, box-mapped). Socket plate (0, 0, 0) facing down. Returns the record, its ridge height"
  " (the top), its eave height (the eaves' underside), size, triangles, and with `over`, plateOver and facingOver: where and how to"
  " place it (placeKitPiece) to sit on those objects." + kitPieceHelp
))
async def addRoof(
  context: Context, name: str, kind: str, pitchDegrees: float, overhang: float, thickness: float, materials: dict[str, str],
  worldUnitsPerRepeat: dict[str, float], location: list[float], footprint: list[float] | None = None, over: list[str] | None = None,
  ridgeAlong: str = "x",
):
  return await callBridge(context, "addRoof", {
    "name": name, "kind": kind, "pitchDegrees": pitchDegrees, "overhang": overhang, "thickness": thickness, "materials": materials,
    "worldUnitsPerRepeat": worldUnitsPerRepeat, "location": location, "footprint": footprint, "over": over, "ridgeAlong": ridgeAlong,
  })


@guardedTool(description=(
  "Place one kit piece as the client places its kits: an empty instancing the piece's collection, linked from the kit at `kitPath`"
  " (absolute) or the open file's own with null (as in a kit file), sharing the piece's one model with every other placement. Either at"
  " `location` [x, y, z] (its base center) facing `facingDegrees`; or at [x, y] facing `facingDegrees`, settled by its footprint as"
  " settleObjects settles (onto the lowest ground under it), then `depth` lower (refused where rock lies over ground: give z); or"
  " snapped: `snapTo` {object, socket, pieceSocket} puts the piece where its pieceSocket meets the placed piece object's socket face to"
  " face (Freeport's wall runs stand exactly 25.0 apart): a level pair turns the piece to meet it (no facingDegrees), a vertical pair"
  " (a post on a wall's top) keeps facingDegrees, by default the object's. A socket already joined is refused, naming its partner, and"
  " so is a snap to a piece tilted or scaled. A placed piece is an ordinary placement (transformObjects, deleteObjects, settleObjects"
  " work on it, keeping it upright at scale 1), never in the terrain collection. Where rock lies over ground the refusal names the"
  " object over it. Returns where it stands and faces, the piece {kit, piece, kind, size, module, passable, fingerprint}, its sockets in"
  " the world each joined to another's or free, and settleObjects' report when settled, else footing {base, under: the lowest and"
  " highest surface players stand on under its footprint, floats: how far its base stands over the lowest where more than a step,"
  " else null} (a run snapped out over a drop says so)." + facingHelp + kitPieceHelp
))
async def placeKitPiece(
  context: Context, name: str, kitPath: str | None, piece: str, location: list[float] | None = None, facingDegrees: float | None = None,
  snapTo: dict | None = None, depth: float = 0.0, collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "placeKitPiece", {
    "name": name, "kitPath": kitPath, "piece": piece, "location": location, "facingDegrees": facingDegrees, "snapTo": snapTo, "depth": depth, "collection": collection,
  })


@guardedTool()
async def swapKitPiece(context: Context, names: list[str], piece: str):
  """Put another piece of the same kit in place of placed ones, each keeping where it stands and how it faces: a window section for a
  plain one, without breaking the run. The new piece's sockets must be the old one's (names, places within 0.01, directions), so every
  joint holds; otherwise none is swapped and each difference is named. In a kit, a prefab whose part it swaps has its footprint
  measured again about its origin (its entrances kept: name a new doorway with assemblePrefab), and its placements go stale (kit) to be
  laid again on it. Returns each placement with its new piece and its sockets, joined or free, and each prefab changed with its
  footprint before and after and its entrances."""
  return await callBridge(context, "swapKitPiece", {"names": names, "piece": piece})


# Structures

structureHelp = (
  " A structure is one artist action kept with its definition: a collection named for it under structures (or terrain, for a span"
  " exported as ground), holding its parts and its record (kind, order, the definition as the build tool took it, the fingerprints of"
  " the kit pieces or prefab it was laid from) and every ground lookup the lay made (probes). Its geometry is always laid again from the"
  " definition, the kit, and the ground: editStructure changes it (with no changes, lays it again after its ground or kit changed),"
  " removeStructure takes it back, getStructures lists them with whether each is stale; parts refuse hand edits (transformObjects,"
  " deleteObjects, mesh, material, and UV tools), naming those tools. A lay is whole or nothing: a refusal leaves the file as it was. It"
  " casts against what players collide with but the boundaries, leaving out its own parts and every structure laid after it. kitPath is"
  " the kit every piece or prefab comes from (absolute; null for the open file's own), kept relative to the saved .blend; a piece of the"
  " wrong kind is refused naming the kind it needs. sink is how far posts, legs, anchors, and wall feet go under the ground. Every result"
  " gives structure {name, kind, order}, its parts with their triangles and the export models they become, standsOn (the objects its"
  " probes found), views (renderView views to judge it from, ready for saveReviewCamera; orbit: its parts for renderOrbit), and for a"
  " deck or flight walk: walkRoute along its walk line (walkRoute and renderRouteStrip take the structure's name as route)."
)
railsHelp = (
  " rails {piece, height, sides (\"both\", \"left\", \"right\" of travel; both by default)}: a rail piece as bars from post to post, their"
  " tops `height` over the deck, each stretched to fit, or a ropeRail swept along the posts at that height following the deck, its"
  " top at the height, hanging plumb however the deck slopes; rails and ropes pass players through (the kit's passable), so open sides"
  " can be fallen from, as the client's can. Rails need posts on their sides and run the whole stretch: a railed post stands however"
  " low the walk runs, and where what it stands on reaches the rail's height (a walkway's post at a flight's head) the rail meets that."
)


@guardedTool(description=(
  "In a kit file, gather placed instances of the file's own pieces (placeKitPiece with kitPath null; a roof from addRoof placed over"
  " the walls) into a building other files place whole, as the client splits its buildings into models placed together (Freeport's"
  " inn: shell, interior, roof, rigging, entrance; Highpass's houses: exterior and interior): a collection `name` (starting with the"
  " kit file's stem) holding a child collection per part, `<name><Part>`, with the named instances moved into them. `parts` maps each"
  " part's camelCase name (exterior, interior, roof, or any) to its instances; each part becomes one model in an export, shared by every"
  " placement of the building. Its floor is the exterior's lowest base (every part's without an exterior); its footprint is the convex"
  " hull in plan of every part where it stands on the floor (geometry within a step of the floor: walls' feet, corners, floors, not a"
  " roof's eaves), what seating and plinths are laid from; its origin (the prefab's and every part's instance_offset) is the middle of"
  " the footprint's plan bounds at the floor. `entrances` [{name, at [x, y, z] in this file, facingDegrees}]: a doorway's threshold"
  " middle and the way out of it, within a step of the footprint; a building with an interior part is walked into and names at least"
  " one (NPC service buildings), a closed shell needs none. Assembling again under its name replaces its parts and entrances, and"
  " instances no longer named go back to the scene collection, so a building is reworked by placing, deleting, and assembling again."
  " Marked as an asset. Refuses an object that is not an instance of this file's pieces, one in two parts or in another prefab, an"
  " empty part, a part name not camelCase, an interior without an entrance, an entrance more than a step outside the footprint, an"
  " entrance facing into the building (the footprint running on further its way than behind it), and a"
  " name taken by anything but this prefab. Returns its parts (instances, pieces, triangles each), footprint and its size, origin,"
  " entrances in the prefab frame, triangles, and fingerprint." + kitPieceHelp
))
async def assemblePrefab(context: Context, name: str, parts: dict[str, list[str]], entrances: list[dict] | None = None):
  return await callBridge(context, "assemblePrefab", {"name": name, "parts": parts, "entrances": entrances})


@guardedTool(description=(
  "Place one building as a structure `name`: one instance per part of the prefab `prefab` from the kit at `kitPath` (absolute; null for"
  " the open file's own) (`<name><Part>`, each part one model shared by every placement), its front facing `facingDegrees`."
  " `location` [x, y, z] sets its floor; [x, y] seats the floor at the highest ground under its footprint, found from above (refused"
  " where rock lies over ground: give z), so no ground stands inside it. `plinth` {material (createMaterial), worldUnitsPerRepeat, sink"
  " (2), margin (0)}: a skirt down the footprint's hull, offset out by margin, from the floor to sink under the lowest ground under it,"
  " mapped along its sides so its courses run level, no top or bottom faces (Highpass's houses stand on stone bases), one mesh"
  " `<name>Plinth` and one model of its own. Refuses ground inside the footprint more than a step above the floor (it would come up"
  " through the floor: grade the site or raise the floor), and the floor more than a step above the ground under the footprint without"
  " a plinth (it would float), each naming where and the floor that would fit (a plinth or grading alone where the ground under it runs"
  " more than two steps); a prefab the kit does not hold; a taken name; the terrain collection; rock or anything else over the ground"
  " under it, naming the object. Returns the floor, floorOn {object, at, ground} (what set a seated floor, with a warning when it is not"
  " the ground: a loose piece clipping the footprint), the ground's lowest and highest under the footprint and where, the plinth's top, bottom, and"
  " triangles, the parts with their triangles and the export models they become, each entrance (where it is and faces, the ground a"
  " step outside it, the step up to its threshold, and for a building with an interior part a walk from 10 outside to 10 inside), the"
  " objects its lookups stood on, and views (entrance<Name>: standing 25 out looking at it; orbit: its parts for renderOrbit)."
  " editStructure moves, turns, or re-plinths it; it goes stale when the ground under its footprint moves or its prefab's footprint,"
  " entrances, or parts (a piece swapped, moved, added, or taken out) change in the kit, and follows its pieces' own changes without"
  " going stale." + structureHelp + facingHelp
))
async def placePrefab(
  context: Context, name: str, kitPath: str | None, prefab: str, location: list[float], facingDegrees: float, plinth: dict | None = None,
  collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "placePrefab", {
    "name": name, "kitPath": kitPath, "prefab": prefab, "location": location, "facingDegrees": facingDegrees, "plinth": plinth, "collection": collection,
  })


@guardedTool(description=(
  "Lay one bridge between two anchors the artist picked, as one model, as the client's whole-span bridges are. `start` and `end` [x, y,"
  " z] are the deck's top at its centerline at each end, each within a step over footing with no rock over it within a player's height"
  " (grade the abutments first: gradeRoute). The deck runs straight between them, or by `profile` {\"sag\": d} hangs d under the chord at"
  " mid-span (a parabola; the client's rope bridges sag about 9 percent of the span, Xorbb's 24 over 279, about 19 degrees at the ends) or"
  " {\"arch\": r} rises r over it; level across its `width`. `deck`: a plank piece (planks across edge to edge, round(deck length / plank"
  " depth) of them, each fitted to the width and to the deck length over their count, each turned to the deck's slope where it lies) or"
  " a floor piece (swept along the deck, fitted to the width); the deck and stringers are cut square (vertical) at the anchors, so the deck's end meets the ground it starts from without a sloped sliver to step onto. `stringers`: a beam piece swept under both deck edges. `posts` {piece,"
  " spacing, above (0), sides (the rails' sides; both without rails)}: post pieces just outside those edges (inner faces at the edges,"
  " so the walk keeps the deck's width), at both ends"
  " and evenly at most `spacing` apart, from the deck's underside up to the rails' height plus `above`; the end pairs reach down to"
  " `sink` under the ground as anchors." + railsHelp + " `bents` {stations, post, beam}: trestle frames at those plan distances from the"
  " start: the post piece stretched under each edge from the deck's underside (or the stringers', or under the beam) down to `sink`"
  " under the ground below, and the beam piece (optional) across under the deck. Refused: ends under two plank depths apart in plan;"
  " width over the chord; an end without footing or with rock over it; the deck steeper than `maximumDeckDegrees` anywhere (naming"
  " where, and for a sag or arch the largest that fits); the deck's own underside (stringers may rest in the ground) meeting the ground"
  " beyond a plank depth from the ends (naming where and how deep: it would run through the hill); rails without posts, or on a side"
  " posts do not stand on; a bent station within a width of an end or past"
  " it; an anchor or bent leg with no ground within 300. Returns the plan span, chord, deck length, each end's slope, the steepest, the"
  " lowest deck point, the planks (count and run), posts and anchors with their lengths, rails, bents with each leg's length, the least"
  " clearance under the deck and where, triangles." + structureHelp
))
async def buildBridge(
  context: Context, name: str, kitPath: str | None, start: list[float], end: list[float], width: float, deck: str, profile: dict | None = None,
  posts: dict | None = None, rails: dict | None = None, stringers: str | None = None, bents: dict | None = None, sink: float = 2.0,
  maximumDeckDegrees: float = 30.0, collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "buildBridge", {
    "name": name, "kitPath": kitPath, "start": start, "end": end, "width": width, "deck": deck, "profile": profile, "posts": posts, "rails": rails,
    "stringers": stringers, "bents": bents, "sink": sink, "maximumDeckDegrees": maximumDeckDegrees, "collection": collection,
  })


@guardedTool(description=(
  "Lay one straight flight between a foot and a head the artist picked, as one model: `bottom` [x, y, z] within a step over footing"
  " (ground, a floor, a deck laid earlier) and `top` [x, y, z] the floor it climbs to, within a step over footing. Its risers are equal,"
  " as many as keep each at most `riser` (the client's timber flights rise 1.0 a step at about 21 degrees); each tread the plank piece"
  " `tread` across, fitted to the width and to one step's run, so treads meet without a gap, the last at the head's height. `stringers`:"
  " a beam piece swept along both sides under the treads' ends. `posts` {piece, spacing, sides (both by default)}: legs just outside the"
  " edges, evenly at most `spacing` apart, from `sink` under the ground up to the rails' height where that side has rails (else to the"
  " flight's underside), where the flight stands more than a step over the ground or that side has rails." + railsHelp + " A walkway's"
  " stair legs are laid by the same code. Refused: steeper than 45 degrees (naming the run it needs); riser not above 0 or over 2 (a"
  " step); a top not a riser higher than the bottom; a foot or head without footing within a step, or lying under what it stands on"
  " (naming its top: the treads would lie in it); a head set back on the surface it climbs to, so the top tread would lie inside it"
  " (set the head at its edge); the treads' underside meeting the ground beyond a step from its ends (naming where); a leg with no"
  " ground within 300; rails without posts. Returns the risers (count and height), run per step, pitch, plan length, the legs and their"
  " lengths, the least clearance, triangles; views fromFoot, fromHead (just behind the head, looking down it), and side (from the side"
  " standing in the open, not inside a hill beside it)." + structureHelp
))
async def buildStairs(
  context: Context, name: str, kitPath: str | None, bottom: list[float], top: list[float], width: float, tread: str, riser: float = 1.0,
  stringers: str | None = None, posts: dict | None = None, rails: dict | None = None, sink: float = 2.0, collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "buildStairs", {
    "name": name, "kitPath": kitPath, "bottom": bottom, "top": top, "width": width, "tread": tread, "riser": riser, "stringers": stringers,
    "posts": posts, "rails": rails, "sink": sink, "collection": collection,
  })


@guardedTool(description=(
  "Lay one walkway (a deck on stilts or bracketed along a cliff, a dock, landings and their flights) along the artist's `points` [[x,"
  " y, z], ...], each the deck's top at its centerline, as one model (or, with collection \"terrain\", as ground, as Crescent's walkways"
  " are). At each inner point a level landing at its height spans both legs' end edges (the legs stop half a width short of the point:"
  " a width-square landing at a right-angle turn); between landings each leg grades evenly; leg i runs from point i to point i + 1. A"
  " leg steeper than `maximumGradeDegrees` is refused unless listed in `stairLegs`, where a flight is laid instead (buildStairs' code:"
  " `treads`, `riser`); `deck` (plank or floor, as buildBridge's) covers the other legs and the landings (a landing's laid across the"
  " incoming leg's direction and cut to the landing). `posts` {piece, spacing, sides (\"both\", \"left\", \"right\"; both by default)}:"
  " legs just outside the given edges at every landing corner (on the miter where the edge turns, so a corner post stands out from both"
  " edges) and evenly at most `spacing` apart along each leg, from `sink` under the"
  " ground up to the rails' height (or the deck's underside where that side has no rail), as Crescent's stilts rise into its rail posts."
  " `brackets` {piece, side, reach, legs}: on the listed legs and the landing edges carrying on from them, at the post stations where the deck stands higher than the beam over the ground, a beam piece level under the deck from its far edge"
  " across into the rock beside its `side` edge (found within `reach` of that edge), stretched to sink `sink` into it, as walkways bolted along a cliff"
  " are; there the brackets take that side and posts stand only on the other." + railsHelp + " Rails break at each flight's foot and"
  " head. `stringers` as buildBridge's. The ends need not stand on ground (a dock ends over water, a lookout over air); each end's"
  " footing gap is reported (null for none). Refused: fewer than two points or two at one place in plan; a turn over 150 degrees, or a"
  " leg no longer than its landings take (naming it); a leg over maximumGradeDegrees not in stairLegs (naming its grade and the run it"
  " needs); a stair leg steeper than 45 degrees; a deck leg or landing without `deck`, a stair leg without `treads`; the deck's or"
  " treads' own underside meeting the ground anywhere off the ends' first step (naming where); the open ends are cut square; a post with no ground within 300; a bracket station with no"
  " rock within reach (naming it); a stretch standing more than a step over the ground with an edge held by neither posts nor brackets"
  " (naming it and the edge). Returns the legs (from, to, plan and laid length, grade, deck planks or flight risers), landings, posts with"
  " the longest and shortest, brackets with their reach, each end's footing gap, triangles." + structureHelp
))
async def buildWalkway(
  context: Context, name: str, kitPath: str | None, points: list[list[float]], width: float, deck: str | None = None, treads: str | None = None,
  stairLegs: list[int] | None = None, riser: float = 1.0, posts: dict | None = None, brackets: dict | None = None, rails: dict | None = None,
  stringers: str | None = None, sink: float = 2.0, maximumGradeDegrees: float = 15.0, collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "buildWalkway", {
    "name": name, "kitPath": kitPath, "points": points, "width": width, "deck": deck, "treads": treads, "stairLegs": stairLegs, "riser": riser,
    "posts": posts, "brackets": brackets, "rails": rails, "stringers": stringers, "sink": sink, "maximumGradeDegrees": maximumGradeDegrees,
    "collection": collection,
  })


@guardedTool(description=(
  "Lay one wall or fence of kit sections end to end along the artist's `path` [[x, y] or [x, y, z], ...] (closed when its last point is"
  " its first), as the client lays its wall runs; every path point is a joint. `frontSide` (\"left\" or \"right\" of travel) is the side"
  " the sections' fronts face. `sections` names straight wall pieces of one depth and height, each its own module; each leg is filled"
  " with the fewest sections, longest first, exactly (within 0.01): a leg no set of modules fills is refused, naming its length, the"
  " nearest lengths that fill, and how far to move its end. `variants` {\"<section index>\": piece} puts another wall piece of the same"
  " module and size in that place (a window, a door), indices counted along the whole path from 0. A turn (over 0.5 degrees) needs a"
  " post. `posts` {piece, at (\"joints\": where every two sections meet and the ends; \"turns\": the turns and the ends)}: the post piece"
  " there, its base `sink` under the lowest ground at its foot, as Freeport's pillars stand at its joints; wider and deeper than the wall,"
  " else the sections' faces flicker through it. `follow` \"shear\" (the client's way): each section's foot and top slant together to the"
  " ground's fall, verticals vertical: joint heights start at the ground under each joint less sink + shearStep / 2, are lowered until no"
  " section's straight foot stands above the ground less that anywhere along it (sampled every 1 unit along both faces; a joint two"
  " sections share lowered once, by the more either needs, so a wall on level ground stays level), then rounded down"
  " by whole shear steps counted from the first joint, so every foot stays at least sink under; a section with rise 0 is an instance of"
  " its piece, any other a mesh object sharing `<piece>Up<r>` or `<piece>Down<r>` (r the rise along the piece's +X in hundredths: the"
  " piece sheared about its middle), so sections of one rise share one model across every wall in the file. `follow` \"step\": each"
  " section level, its foot sink under the lowest ground under it. The ground is found from a step above each point's z when given (a"
  " wall in a cave, on a deck), from above the scene without it (refused where rock lies over ground: give z). Refused: fewer than two"
  " points or a leg shorter than the shortest section; sections of different depth or height (between their faces: a variant's frames"
  " may stand proud); a leg the modules cannot fill; a variant of another module or past the last section; a turn without a post"
  " (naming it and its angle); a post no wider or deeper than the wall, or one whose top stands under the wall's top beside it; shearStep"
  " not positive; a section sheared steeper than 30 degrees (step the wall, or run it across the slope); a section buried more than"
  " `maximumBurial` anywhere along it (the ground bulges over its line, or rises along a stepped one), naming it and how deep; the"
  " terrain collection. Returns the legs (length, sections), joints (ground, and height when sheared or the bases of the sections"
  " meeting there when stepped), sections (piece, base at each end, rise, model, deepest and shallowest burial), the shear models made"
  " or reused, posts, the placements and triangles per model, and views front<leg> (standing on the ground in front of each leg, short of"
  " any other leg in the way)." + structureHelp
))
async def buildWall(
  context: Context, name: str, kitPath: str | None, path: list[list[float]], frontSide: str, sections: list[str], follow: str = "shear",
  shearStep: float = 1.0, sink: float = 1.0, maximumBurial: float = 10.0, posts: dict | None = None, variants: dict[str, str] | None = None,
  collection: str = "structures",
):
  requireKitPath(kitPath)
  return await callBridge(context, "buildWall", {
    "name": name, "kitPath": kitPath, "path": path, "frontSide": frontSide, "sections": sections, "follow": follow, "shearStep": shearStep,
    "sink": sink, "maximumBurial": maximumBurial, "posts": posts, "variants": variants, "collection": collection,
  })


@guardedTool()
async def editStructure(context: Context, name: str, changes: dict | None = None):
  """Change a structure and lay it again: `changes` (any keys of its definition, as its build tool takes them, such as a placed prefab's
  location, facingDegrees, or plinth; kitPath absolute) merged into its definition, laid against the ground and kit as they now are,
  whole or not at all; with no changes, lay it again as defined (after the ground under it or its kit changed: getStructures and
  regradeTerrain name the stale ones). Lay structures again in the order getStructures lists them, so a flight landing on a walkway
  follows the walkway. Returns what its build tool returns, with the changes and how many of its probes had moved since it was last laid
  (and the largest). Refused: no such structure (listing them); keys not in its kind's definition (listing them); whatever its build
  refuses, the structure staying exactly as it was."""
  if changes is not None and "kitPath" in changes:
    requireKitPath(changes["kitPath"])
  return await callBridge(context, "editStructure", {"name": name, "changes": changes})


@guardedTool()
async def removeStructure(context: Context, name: str):
  """Take a structure back: its collection and parts (and a part's mesh no other object uses), and any shared shear mesh no other wall
  uses. Returns its kind and definition (kitPath absolute), to build it again with its build tool."""
  return await callBridge(context, "removeStructure", {"name": name})


@guardedTool()
async def getStructures(context: Context, names: list[str] | None = None):
  """Every structure (or those named) in the order they were first laid: kind, order, definition (kitPath absolute), parts with their
  triangles and the export models they become, standsOn (the objects its probes find now), and whether it is stale and why: ground
  (its probes looked up again: how many moved more than 0.01, the largest change and where, and where a look down from over the scene
  now meets something standing over the ground, overGround: that object, its top and underside, and the ground under it), kit (the
  pieces or prefabs whose fingerprint changed since it was laid: a span bakes its pieces, so it follows the kit only when laid again; a
  placed prefab follows its pieces at once and goes stale when its prefab's footprint, entrances, or parts change), or missing (its kit file, a piece, or a prefab
  cannot be found); its walk line for a bridge, flight, or walkway; its views (orbit: its parts for renderOrbit). Also the loose kit
  pieces placed by hand, counted by kit and piece. Each structure is looked up as its lay looked: leaving out its own parts and every
  structure laid after it. Changes nothing."""
  return await callBridge(context, "getStructures", {"names": names})


# Water

liquidDefaults = {
  # The client's own new-water settings (Resources/WaterSwap/WaterSwap.ini, [NewWater]) and the slides most of its water materials use,
  # whose two layers move against each other: still water.
  "water": {"fresnelBias": 0.25, "fresnelPower": 8.0, "reflectionAmount": 0.7, "reflectionColor": [1.0, 1.0, 1.0], "waterColor1": [0.0, 0.04, 0.11], "waterColor2": [0.0, 0.23, 0.17], "slides": [0.02, 0.02, 0.03, 0.03]},
  # The slides of wtr_waterfall_tile, the fall the most client zones use (Highpass Hold, Steppes, Silyssar, Illsalin, Underquarry, ...):
  # both layers down a fall whose v runs down, as the client's and pourWaterfall's falls run it.
  "waterfall": {"slides": [-0.12, -0.32, 0.0, -0.5]},
  # The slides of Brell's Rest's lava_lake02_c, which more of the client's lava-textured materials carry than any other (7 in 6 archives:
  # Brell's Rest, Shining City, Underquarry, Convorteum, Breeding Grounds, Brell's Temple): two layers creeping apart.
  "lava": {"slides": [-0.01, -0.01, 0.01, -0.01]},
}
slideNames = ("first x", "first y", "second x", "second y")
# The client's effects take their clock modulo this many seconds (RegionWater.fxo, RegionWaterFall.fxo, RegionLava.fxo preshaders).
effectWrapSeconds = 100


def flowSlides(liquid, flow):
  """The slides that move a liquid's two layers flow[0] and flow[1] texture repeats per second along its body's v, downstream or
  down: water's first layer moves by its first slide and its second, sampled at twice the coordinates, by minus half its second; a
  waterfall's and lava's layers by minus their slides."""
  if len(flow) != 2:
    raise ToolError(f"flow is [first layer, second layer] in texture repeats a second along the body, got {flow!r}")
  first, second = flow
  return [0.0, first, 0.0, -2 * second] if liquid == "water" else [0.0, -first, 0.0, -second]


def wrapJumps(liquid, slides, flow):
  """Where a layer's pattern jumps when the effect clock wraps: each slide whose wrap time is not a whole number of repeats, with how far
  it jumps (in repeats, at most half) and the nearest values either side that do not, given as the slide or, when `flow` set the slides,
  as the flow layer and its value."""
  jumps = []
  for index, slide in enumerate(slides):
    travelled = effectWrapSeconds * slide
    if abs(travelled - round(travelled)) <= 1e-9:
      continue
    seamless = [math.floor(travelled) / effectWrapSeconds, math.ceil(travelled) / effectWrapSeconds]
    jump = {"jumpRepeats": round(abs(travelled - round(travelled)), 4)}
    if flow is None:
      jumps.append({"slide": slideNames[index], "value": slide} | jump | {"seamless": [round(value, 4) for value in seamless]})
    else:
      factor = {"water": (1.0, -2.0)}.get(liquid, (-1.0, -1.0))[index // 2]
      jumps.append({"flow": ("first", "second")[index // 2], "value": flow[index // 2]} | jump | {"seamless": sorted(round(value / factor, 4) for value in seamless)})
  return jumps


def environmentLookupPath(environmentTexture):
  """The equirectangular image the preview looks a water's environment cube map up in (eqCubeMaps), written once under the tooling root."""
  path = Path(environmentTexture)
  if not path.is_absolute() or not path.is_file():
    raise ToolError(f"environmentTexture '{environmentTexture}' is not an existing absolute path")
  data = path.read_bytes()
  if not eqTextures.isCubeMap(data):
    raise ToolError(f"environmentTexture {path.name} is not a DDS cube map: the client loads a water's environment only as a cube map and reflects nothing from any other texture")
  lookup = toolingRoot / "environmentLookups" / f"{path.stem}@{hashlib.sha256(data).hexdigest()[:12]}.png"
  if not lookup.is_file():
    image = callReportingFailures(eqCubeMaps.environmentLookupPNG, data, path.name)
    lookup.parent.mkdir(parents=True, exist_ok=True)
    lookup.write_bytes(image)
  return str(lookup)


@guardedTool()
async def createLiquidMaterial(
  context: Context, name: str, liquid: str, diffuseTexture: str, normalTexture: str | None = None, environmentTexture: str | None = None,
  secondDiffuseTexture: str | None = None, fresnelBias: float | None = None, fresnelPower: float | None = None, reflectionAmount: float | None = None,
  reflectionColor: list[float] | None = None, waterColor1: list[float] | None = None, waterColor2: list[float] | None = None, slides: list[float] | None = None,
  flow: list[float] | None = None,
):
  """A liquid material, the one exception to Phase 1's diffuse-and-normal rule: `liquid` "water" (the client's Opaque_MaxWater.fx: a
  diffuse, normalTexture, environmentTexture, fresnelBias and fresnelPower, reflectionAmount and reflectionColor, waterColor1 and
  waterColor2), "waterfall" (Opaque_MaxWaterFall.fx: a diffuse), or "lava" (Opaque_MaxLava.fx: a diffuse, secondDiffuseTexture, and
  normalTexture); each takes `slides` [first x, first y, second x, second y], how fast its two texture layers scroll, or instead `flow`
  [first, second]: how many texture repeats a second each layer moves along the body that takes it (a river downstream, a fall down,
  as runWater and pourWaterfall run their v), from which the slides are set as the client's effects take them (water: [0, first, 0,
  -2 * second]; waterfall and lava: [0, -first, 0, -second]). Water's own two layers moving against each other is still water, as most
  of the client's is; flow moves both one way, a river. The client's effects take time modulo 100 seconds, so a layer whose slide
  times 100 is not a whole number of repeats jumps then, as the client's slowest lava creeps do (Ashengate's 0.005); the result's
  jumpsAtWrap lists each such slide (or flow layer) with how far it jumps and the nearest values either side that do not. A material's
  slides are shared by every body that takes it; getWater says which way and how fast each body flows. Values left out take the
  client's own (water: its WaterSwap.ini new water and still slides; waterfall: the slides of the client's most used fall texture,
  flowing down; lava: Brell's Rest's lava_lake02_c, the slides most of its lava-textured materials carry). Colors are three numbers
  from 0 to 1; fresnelBias is 0 to 1, fresnelPower above 0, and reflectionAmount 0 or more (the client's own run 0 to 1, 1 to 10, and
  0 to 2). A texture is an absolute path or a catalog texture id (texture/<name>@<hash>); a water's environmentTexture is a DDS cube
  map, as the client loads only those (a 2D texture there would reflect nothing in game); the preview mirrors it from a lookup image
  kept under the tooling root, made again from the cube map whenever a file opens without it (another machine, a cleared cache). The
  preview draws it as the client's effects draw it on a placed object (which a water body exports as), at effect time 0 unless
  renderView sets liquidTime: water takes no color from its diffuse (the client's older effects do) but runs from waterColor1 seen from
  above to waterColor2 at grazing angles, rippled by its normal map at the texture coordinates and twice them (so the normal map
  repeats as often as the surface's texture coordinates do), lit by the surface's own normal as the client's vertex light is, and
  mirrors its environment cube map by fresnel, looked up along the view reflected about the ripples; a waterfall is its diffuse's
  color, lit, blended over what lies behind by its alpha, each layer scrolled by its own slide; lava's diffuse alpha is how much crust
  shows (1 crust, 0 glow): the crust is the diffuse lit, and the rest glows with secondDiffuseTexture's color, unlit, so it glows in
  the dark and at night (paint the alpha to place the glow). Pools, rivers, and falls (floodWater, runWater, pourWaterfall) take these
  materials."""
  if liquid not in liquidDefaults:
    raise ToolError(f"liquid is one of {list(liquidDefaults)}, got '{liquid}'")
  if flow is not None and slides is not None:
    raise ToolError("Give slides or flow, not both: flow sets the slides")
  if flow is not None:
    slides = flowSlides(liquid, flow)
  given = {
    "fresnelBias": fresnelBias, "fresnelPower": fresnelPower, "reflectionAmount": reflectionAmount, "reflectionColor": reflectionColor,
    "waterColor1": waterColor1, "waterColor2": waterColor2, "slides": slides,
  }
  stray = sorted(key for key, value in given.items() if value is not None and key not in liquidDefaults[liquid])
  if stray:
    raise ToolError(f"A {liquid} material does not take {stray}; it takes {list(liquidDefaults[liquid])}")
  values = liquidDefaults[liquid] | {key: value for key, value in given.items() if value is not None}
  environmentPath = catalogTexturePath(environmentTexture)
  made = await callBridge(context, "createLiquidMaterial", {
    "name": name, "liquid": liquid, "diffuseTexture": catalogTexturePath(diffuseTexture), "normalTexture": catalogTexturePath(normalTexture),
    "environmentTexture": environmentPath, "environmentLookup": environmentLookupPath(environmentPath) if environmentPath is not None else None,
    "secondDiffuseTexture": catalogTexturePath(secondDiffuseTexture), "values": values,
  })
  return made | {"jumpsAtWrap": wrapJumps(liquid, values["slides"], flow)}


waterBodyHelp = (
  " A water body is one named object in the water collection, rebuilt from what it was made from whenever editWater or"
  " shapeWaterExtent changes it, against the ground as it then is; look at it after every change (renderView close at the shore and"
  " from above), then adjust. Its surface reaches a little under its banks so no seam shows, and where its bounds cross open water"
  " (a river's reach, a within outline, a stroke) it is cut cleanly along them rather than stepping cell by cell. Where players swim is designed"
  " apart from the surface, as swim volumes (buildSwimVolumes starts them, placeSwimVolume adds one), once the water and bed settle;"
  " editWater's swimmable false marks a body no one swims in. The ground is what players stand on: rendered meshes and collection"
  " instances that are not water, guides, regions, spawns, or doors, without the faces players pass through. The result's `built` reports what the build found: a pool or river's deepest point and where it"
  " runs off the end of the ground (a zone edge used as a source or an end, or a leak to bound)."
)


@guardedTool(description=(
  "Flood a pool: from `seed` [x, y] (where the water must stand over ground) out over every grid point with ground below `level`,"
  " point to neighbouring point, within the `within` outline [[x, y], ...] when given. A flood is not stopped by its size: one that"
  " spreads over open ground to the end of the ground (a level over a plateau, a gap in a rim) is built, and its built.reachesGroundEnd"
  " says where it ran off (groundEndPoints how often); bound it with `within`, a lower level, or shapeWaterExtent unless it is meant to"
  " reach the zone's edge (a sea). Only a flood over 250,000 grid points is refused. Meshed every `spacing` units, its flat cells"
  " merged, the material repeating every `worldUnitsPerRepeat` units. A starting point: shape it with editWater (level, seed, within)"
  " and shapeWaterExtent, and its bed with carveWaterBed." + waterBodyHelp
))
async def floodWater(
  context: Context, name: str, seed: list[float], level: float, material: str, within: list[list[float]] | None = None,
  spacing: float = 8.0, worldUnitsPerRepeat: float = 64.0, collection: str | None = None,
):
  return await callBridge(context, "floodWater", {
    "name": name, "seed": seed, "level": level, "within": within, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat,
    "material": material, "collection": collection,
  })


@guardedTool(description=(
  "Run a river along `path` [[x, y, level], ...], downstream in order, its surface at each point's level and falling evenly between"
  " them (a drop is a fall: end one river at the lip, pourWaterfall, start the next below); a path whose level rises along it is"
  " refused (one given from the mouth runs backwards: reverse it). It spreads over the ground below its level within `reach` of the"
  " path. Mapped along the path as the client's rivers are (Brell's Rest, Beasts' Domain): u across, rightward looking downstream, and"
  " the client's v downstream, a repeat every `worldUnitsPerRepeat` both ways, so a material's flow (createLiquidMaterial) runs it"
  " downstream; getWater says which way it flows. The mapping follows the path with each bend rounded to three times the reach, so the"
  " texture turns with the water instead of folding; where the legs either side of a bend leave less room than the reach (a hairpin),"
  " built.tightBends names the bend, whose inside still squeezes: add path points to open the bend, or narrow the reach. Its material"
  " is water (a surface whose two layers move one way downstream, as Beasts' Domain's), waterfall (a see-through scrolling ribbon laid"
  " over a rock bed, as Crescent Reach's and Brell's Rest's are; swum as water), or lava. A path may start or end at the edge of the"
  " ground, as a river entering or leaving the zone. Carve its channel first (sculptAlongPath) where the ground has none. A starting"
  " point: adjust with editWater (path, reach) and shapeWaterExtent; sprayWater adds white water at points on its rapids and where it"
  " joins a pool." + waterBodyHelp
))
async def runWater(
  context: Context, name: str, path: list[list[float]], reach: float, material: str, spacing: float = 8.0, worldUnitsPerRepeat: float = 64.0,
  collection: str | None = None,
):
  return await callBridge(context, "runWater", {
    "name": name, "path": path, "reach": reach, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat, "material": material,
    "collection": collection,
  })


@guardedTool(description=(
  "Pour a waterfall from `lip` [[x, y, z], ...], the edge the water goes over, running from the fall's left edge to its right as seen"
  " from in front of it: a sheet that starts a little back from the lip, turns over it, and drops to `bottom`, carried out from the face"
  " by `throw` at the bottom (out as the square root of the drop, as falling water goes) and `spread` times as wide there; rows every"
  " `spacing` units. Mapped as the client maps its falls: the texture spans the lip `acrossRepeats` times (once by default, so a fall"
  " texture's faded side edges frame the fall; more for a fall made of side-by-side strands) and repeats every `worldUnitsPerRepeat`"
  " down the drop, the client's v running down, so a waterfall material's slides as the client's falls carry them flow down. The"
  " client's falls end at their pool's surface or a little under it. The result's built.insideRock lists rows that pass inside the"
  " rock (raise throw or move the lip) and closestToRock how near the sheet comes. Its waterfall material scrolls in the client; give"
  " the pool below its own body, and its white water with sprayWater (the client's falls have no foam surface: spray and mist where"
  " they strike, ripple rings on the pool, and wet rocks placed at the foot to hide where the sheet meets the water)." + waterBodyHelp
))
async def pourWaterfall(
  context: Context, name: str, lip: list[list[float]], bottom: float, material: str, throw: float = 6.0, spread: float = 1.0,
  spacing: float = 8.0, worldUnitsPerRepeat: float = 64.0, acrossRepeats: int = 1, collection: str | None = None,
):
  return await callBridge(context, "pourWaterfall", {
    "name": name, "lip": lip, "bottom": bottom, "throw": throw, "spread": spread, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat,
    "acrossRepeats": acrossRepeats, "material": material, "collection": collection,
  })


emitterDefinitionsCache = {}


def clientEmitterDefinitions():
  """The client's environment emitter definitions, read once per file version."""
  path = eqEmitterDefinitions.environmentDefinitionsPath(zoneSources.resolveClientRoot())
  stamp = path.stat().st_mtime_ns
  if emitterDefinitionsCache.get("stamp") != stamp:
    emitterDefinitionsCache.update(stamp=stamp, definitions=eqEmitterDefinitions.parseDefinitions(path.read_bytes(), path.name))
  return emitterDefinitionsCache["definitions"]


def resolvedSpray(spray):
  """A spray as a body keeps it, its emitter definitions checked against the client's, and a row with neither spacing nor count set
  one emitter per disc width: twice the radius its definition starts particles within."""
  spray = {key: value for key, value in spray.items() if key != "emitters"}
  stray = sorted(set(spray) - {"at", "definition", "rings", "spacing", "count", "above"})
  if stray or "at" not in spray or "definition" not in spray:
    raise ToolError(f"A spray is {{at, definition, rings, spacing, count, above}}, at and definition required; got {sorted(spray)}")
  try:
    definitions = clientEmitterDefinitions()
  except (OSError, ValueError) as error:
    raise ToolError(f"Sprays use the client's emitter definitions: {error}") from error
  for key in ("definition", "rings"):
    index = spray.get(key)
    if index is not None and (not isinstance(index, int) or not 1 <= index < len(definitions)):
      raise ToolError(f"A spray's {key} is a client environment emitter definition, 1 to {len(definitions) - 1}, got {index!r}")
  spray = {"rings": None, "spacing": None, "count": None, "above": 1.0} | spray
  if spray["at"] in ("foot", "lip") and spray["spacing"] is None and spray["count"] is None:
    definition = definitions[spray["definition"]]
    if definition["emitterScaled"] or definition["shapeRadius"] <= 0:
      raise ToolError(f"Definition {definition['index']} '{definition['name']}' starts its particles at its emitter, so no width sets a row's spacing; give spacing or count")
    spray["spacing"] = 2 * definition["shapeRadius"]
  return spray


@guardedTool(description=(
  "White water on one body, as the client's zones make it: no foam surface (no client EQG zone has one) but particle emitters where"
  " the water strikes, kept with the body and placed again whenever it is rebuilt. `at` is \"foot\" (a fall's foot: where its sheet"
  " strikes the water below, a pool's or a client zone's, or else the ground, along the whole width), \"lip\" (along a fall's lip,"
  " where the water goes over), or {\"points\": [[x, y], ...]} (on a pool's or river's own surface, one emitter at each point, laid"
  " by hand: rocks in a river, steps, a river's rapids, where a river joins a pool). `definition` is the client environment emitter"
  " definition (findAssets kind emitter, and the catalog's measured use, say which zones use each where); `rings` an optional second"
  " one laid at the same places 0.5 over the water, as Crescent Reach lays its ripple rings, refused at a lip and where any of the"
  " row's emitters strikes dry ground. A row at a foot or lip has `count` emitters, or one per `spacing` of its width (at least one),"
  " each in the middle of its share; without either, one per disc width of the definition, twice the radius it starts particles"
  " within. Each emitter stands `above` over what it strikes (the client's stand 0 to 5 over their water). From the client's zones: a"
  " fall's foot 133 (crwaterfallbottom, Crescent Reach's within a unit of their pools) or 99 (geyserbottom, Highpass Hold) with rings"
  " 142 (waterwheelsplashbig, Crescent Reach); along a lip where a river pours away, 133 in a"
  " row (Beasts' Domain lays six 56 to 88 apart where its river leaves the zone over the edge); 101 (waterfall_top) only high on a tall"
  " fall (Beasts' Domain's two on its 1000-unit fall, at 969 and 679), as its disc (radius 50, thrown 30 to 70 up) whites out the lip"
  " of a short one; a river's rapids small splashes such as 277 at points scattered across and along the white water, never an even"
  " row (Brell's Rest's 22 lie 18 to 59 apart and zigzag across its river); where a river meets a pool 141 (waterwheelsplash). The"
  " emitters export as any emitter does, into <zone>_EnvironmentEmitters.txt; editWater's sprays changes or removes them ([] takes them"
  " all off), and deleting the body takes them with it. Rebuilding or deleting the pool or river a fall lands in places that fall's"
  " foot sprays again (fallFeetSprayedAgain), refusing the edit where they could not stand; other falls are left be. An emitter whose"
  " strike did not move is kept as it is, so a hand nudge (transformObjects) or a hidden render lasts until the water under it moves."
  " Wet rocks at a fall's foot, which the client's falls use to hide where the sheet meets the water, are placed by hand (placeObject)."
))
async def sprayWater(
  context: Context, name: str, at: str | dict, definition: int, rings: int | None = None, spacing: float | None = None, count: int | None = None,
  above: float = 1.0,
):
  spray = resolvedSpray({"at": at, "definition": definition, "rings": rings, "spacing": spacing, "count": count, "above": above})
  return await callBridge(context, "sprayWater", {"name": name, "spray": spray})


@guardedTool(description=(
  "Change what a water body is made from and build it again against the ground as it is now: a pool's level, seed, within (an empty"
  " list removes it), spacing, worldUnitsPerRepeat, or strokes (an empty list clears them); a river's path, reach, spacing,"
  " worldUnitsPerRepeat, or strokes; a pool or river's swimmable (false: no one swims in it, a fountain or a trickle; true: it is"
  " swum, its swim volumes to be built; marking false is refused while the body has swim volumes, which go first, with deleteObjects);"
  " a fall's lip, bottom, throw, spread, spacing, worldUnitsPerRepeat, or acrossRepeats; any body's material; any body's sprays (the"
  " whole list as sprayWater keeps them, each {at, definition, rings, spacing, count, above}; getWater's may be passed back as they are,"
  " their emitters placed anew; an empty list takes them all off)."
  " With nothing to change it only rebuilds, for after the ground under it has changed, and places its sprays again." + waterBodyHelp
))
async def editWater(
  context: Context, name: str, level: float | None = None, seed: list[float] | None = None, within: list[list[float]] | None = None,
  path: list[list[float]] | None = None, reach: float | None = None, lip: list[list[float]] | None = None, bottom: float | None = None,
  throw: float | None = None, spread: float | None = None, spacing: float | None = None, worldUnitsPerRepeat: float | None = None,
  acrossRepeats: int | None = None, strokes: list[dict] | None = None, swimmable: bool | None = None, sprays: list[dict] | None = None,
  material: str | None = None,
):
  changes = {
    "level": level, "seed": seed, "within": within, "path": path, "reach": reach, "lip": lip, "bottom": bottom, "throw": throw,
    "spread": spread, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat, "acrossRepeats": acrossRepeats, "strokes": strokes,
    "swimmable": swimmable, "sprays": None if sprays is None else [resolvedSpray(spray) for spray in sprays],
  }
  return await callBridge(context, "editWater", {"name": name, "changes": {key: value for key, value in changes.items() if value is not None}, "material": material})


@guardedTool(description=(
  "Stroke where a pool or river may spread, as an artist paints a mask: mode \"add\" lets it flood an `area` its bounds left out (a"
  " cove past its within outline, a backwater beyond a river's reach), \"remove\" stops it where it leaks (a stroke across a gap in"
  " the bank); `area` is {\"circle\": {\"center\": [x, y], \"radius\": r}} or {\"polygon\": [[x, y], ...]}. Strokes apply in order"
  " and are kept, so later edits keep them; editWater with strokes [] clears them. A stroke may be laid ahead of an edit (a removed"
  " stroke across a side channel the water reaches only once its level is raised): one that changes nothing yet is kept all the same,"
  " and the result says so (strokeChanged false) and why (whyUnchanged): no water inside a removed area yet, or for an added one,"
  " ground there at or above the level, low ground the water already covers, or low ground that higher ground or the body's own bounds"
  " part from the water (open a way, stroke the gap in too, or flood it as its own body)." + waterBodyHelp
))
async def shapeWaterExtent(context: Context, name: str, mode: str, area: dict):
  return await callBridge(context, "shapeWaterExtent", {"name": name, "mode": mode, "area": area})


@guardedTool()
async def carveWaterBed(context: Context, name: str, objectName: str, depth: float, shoreWidth: float):
  """Lower the ground (objectName) under a pool or river: `depth` under its surface from `shoreWidth` out from its waterline (where
  the ground meets the surface), rising smoothly to the surface at the waterline. It first cuts the ground along the waterline (an edge
  loop, as cutContours with waterline makes; `waterlineCut` counts the splits), so the waterline stays exactly where it is: nothing at
  or above the surface moves, and ground already deeper stays. With shaping passes the cut keeps them and the lowering goes into the
  active pass. Paint the bed and wet banks with paintSurface's underWater and nearWater selectors; along the cut, the bed meets the
  waterline exactly, and a bank band ends cleanly once cut at its width too (cutContours with waterline)."""
  return await callBridge(context, "carveWaterBed", {"name": name, "objectName": objectName, "depth": depth, "shoreWidth": shoreWidth})


@guardedTool()
async def getWater(context: Context):
  """Every water body: its kind, what it is made from, its material, its levels, its visibleExtent (the plan bounds of the water
  players see: a pool or river's surface where it is not tucked under its banks, a fall's sheet outside the rock; null when none
  shows), its mesh counts, its sprays with the emitters each placed (name, definition, position, and whether it stands on water, ground,
  or the lip), and its flow, derived from its material's slides and its own mapping: each texture layer's speed along the body (a river
  downstream, a fall down; negative is upstream or up) and across it in world units per second of the client's effect clock (a pool's
  as a velocity [x, y]), and the body's flow: downstream or down, upstream or up (the material is wrong for it), still (the two layers
  move against each other: the client's still water), drifting (a pool's layers one way), across, or none."""
  return await callBridge(context, "getWater", {})


# Housing

def borderModelFolder(kind):
  """The plot border model as the client loads it in the Neighborhood, the zone that links its archive (stonesquare.eqg); exportZone
  lists that archive in the zone's own <zone>_assets.txt."""
  folder, _ = eqModel("neighborhood", {"player": "OBP_LOTSQUARE", "guild": "OBP_GUILDSQUARE"}[kind])
  return str(folder)


plotHelp = (
  " A plot is a guide on the ground (its outline, and a chevron on the middle of its entrance side pointing out) with the client's own border model (a player's"
  " OBP_LOTSQUARE, a guild's OBP_GUILDSQUARE, sized as a door's size scales it, in whole percents) at its center, as players will see"
  " it; guides draw in renderView (guides false hides them) and never export. `facingDegrees` is the way its entrance faces, toward its"
  " street (0 = +Y, clockwise). Sizes are [across, along] (along runs from the entrance to the back); the default is the stock plot:"
  " player 169.1 x 170.1, guild 351.2 x 699.8, as Sunrise Hills' are; any size can be given. items and pets default to the zone's"
  " pricing (player 105 and 5, guild 210 and 12, Live's limits; Live's other player tiers are 90 and 4, 120 and 6). Look at every plot"
  " from the street at eye height and from"
  " above, and grade it (gradePlot) so it sits level."
)


@guardedTool()
async def setZoneHousing(
  context: Context, role: str | None = None, intent: str | None = None, placement: str | None = None, plotBudget: dict | None = None,
  pricing: dict | None = None, routes: list[list[list[float]]] | None = None,
):
  """The zone's housing decision, made before any plot: `role` none (no housing), incidental (a few plots in a zone for something
  else, such as along its main road), featured (a housing area is one of the zone's parts), or primary (the zone is for housing);
  `intent`, what housing is for here; `placement`, where Peridot hosts its plots: "world" (in the public zone itself, its world plots)
  or "neighborhood" (instanced neighborhoods made from this zone); `plotBudget` {"player": n, "guild": n}, how many plots it means to
  have; `pricing`, changes to its price rules: basePlatinum and defaultItems and defaultPets per kind (player 84pp for 105 items and 5
  pets, guild 21pp for 210 and 12, Live's), platinumPerItem and platinumPerPet for allowances above or below the defaults (2.1 and 10.5,
  which price Live's other tiers at Live's prices), featureMultipliers {feature: multiplier} (prominent 1.5, secluded 1.5,
  view 1.25, waterfront 1.25, sheltered 1.25, remote 0.75, swamp 0.5 to start; add any), upkeepShare (a tenth: upkeep a day is a tenth
  of the price, as on Live); `routes` [[[x, y], ...], ...], the zone's main routes, from which assessPlot judges how prominent a plot is.
  Calls change what they name and keep the rest. Returns the decision, its plots, and any overlaps."""
  return await callBridge(context, "setZoneHousing", {"role": role, "intent": intent, "placement": placement, "plotBudget": plotBudget, "pricing": pricing, "routes": routes})


@guardedTool()
async def getHousing(context: Context):
  """The zone's housing decision and every plot: address, kind, center, facing, size, allowances, features, price with each step that
  led to it, upkeep, border size, and grading (the ground it is graded on, its margin and batter, or null; the ground is null when it
  was deleted); plot counts against its budget; and overlapping plots, which export refuses."""
  return await callBridge(context, "getHousing", {})


@guardedTool(description=(
  "Place one plot at `center` [x, y], its address its name (\"101 Canyon Way\"); its height is the ground's under its center unless"
  " `height` is given (the ground as it lies, without any plot's grading). Where rock lies over ground there (a cave under a hill, an"
  " overhang) either could be meant, so without `height` it is refused, naming the top, the rock's underside, and the ground under it:"
  " give the height of the one meant (a cavern's level stretch from cutCave gives its floor). `features` (from the zone's featureMultipliers) and `pricePlatinum` (an override of the derived price) set its"
  " price. The result gives its price and any plots it overlaps." + plotHelp
))
async def placePlot(
  context: Context, address: str, center: list[float], facingDegrees: float, kind: str = "player", size: list[float] | None = None,
  height: float | None = None, items: int | None = None, pets: int | None = None, features: list[str] | None = None,
  pricePlatinum: int | None = None, collection: str | None = None,
):
  folder = await anyio.to_thread.run_sync(borderModelFolder, kind) if kind in ("player", "guild") else None
  return await callBridge(context, "placePlot", {
    "address": address, "kind": kind, "center": center, "facingDegrees": facingDegrees, "size": size, "height": height, "items": items,
    "pets": pets, "tags": features, "pricePlatinum": pricePlatinum, "borderFolder": folder, "collection": collection,
  })


@guardedTool(description=(
  "Change one plot: newAddress, kind, center (its height follows the ground as it lies, without any plot's grading, unless `height` is"
  " given; refused where rock lies over ground, as placePlot is), facingDegrees, size, height, items, pets, features (replacing its list), pricePlatinum (an override; 0 returns it to its"
  " derived price). A plot whose kind or size changes gets a new border. A graded plot keeps its grading: renamed, its pass is renamed;"
  " moved, turned, resized, or raised, its ground is graded again where it now lies, and the result's grading says as gradePlot does"
  " what that changed. It stays graded on the ground it is graded on; `objectName` grades it on another (moved onto another terrain"
  " mesh, say), taking its grading back from the first, as formerGround reports. Nothing changes when the edit is refused." + plotHelp
))
async def editPlot(
  context: Context, address: str, newAddress: str | None = None, kind: str | None = None, center: list[float] | None = None,
  facingDegrees: float | None = None, size: list[float] | None = None, height: float | None = None, items: int | None = None,
  pets: int | None = None, features: list[str] | None = None, pricePlatinum: int | None = None, objectName: str | None = None,
):
  folder = None
  if kind is not None or size is not None:
    current = await callBridge(context, "getHousing", {})
    plot = next((entry for entry in current["plots"] if entry["address"] == address), None)
    if plot is None:
      raise ToolError(f"No plot '{address}'")
    newKind = kind or plot["kind"]
    if newKind not in ("player", "guild"):
      raise ToolError(f"kind is player or guild, got '{newKind}'")
    folder = await anyio.to_thread.run_sync(borderModelFolder, newKind)
  return await callBridge(context, "editPlot", {
    "address": address, "newAddress": newAddress, "kind": kind, "center": center, "facingDegrees": facingDegrees, "size": size,
    "height": height, "items": items, "pets": pets, "tags": features, "pricePlatinum": pricePlatinum, "objectName": objectName,
    "borderFolder": folder,
  })


@guardedTool()
async def removePlot(context: Context, address: str, keepGrading: bool = False):
  """Remove a plot and its border. A graded plot's grading goes with it: its pass is removed and the other plots graded on that ground
  are graded again without it, their pads untouched. With keepGrading the ground stays as it is, the plot's pass kept as ordinary
  shaping named "kept grade <address>"; a plot whose pass was collapsed, or whose ground was deleted, is removed only so. Nothing is
  removed when the removal is refused."""
  return await callBridge(context, "removePlot", {"address": address, "keepGrading": keepGrading})


@guardedTool()
async def gradePlot(context: Context, address: str, objectName: str, margin: float = 10.0, batterDegrees: float = 35.0):
  """Grade a plot on the ground (objectName): level its footprint and `margin` around it at the plot's height, cutting into ground
  above it and filling ground below it, each meeting the ground around at `batterDegrees`, as a builder's cut and fill slopes do, in
  the plot's own shaping pass ("grade <address>"). Every plot graded on that ground is graded together from the ground without their
  passes, so no plot's pad is ever disturbed by another's slopes and the result does not depend on the order plots were graded in;
  where two pads stand too close for their difference in height, the ground between them runs in one straight bank, steeper than the
  batter, listed in steepBanks (move a plot, change its height, or build a retaining wall there). touched names the other plots whose
  ground within 60 units of their edges changed: look at them too. The plots are graded on the ground without any defined pass made
  after the first of them (a route graded later, gradeRoute), and every such later pass whose ground the grading changes is graded
  again after it, so a route to a plot's entrance stays on its bench (replayed lists them, with what each moved; editPlot and removePlot
  replay alike). Grading again (with another margin or batter) replaces what the pass
  held; grading on other ground takes its grading back from the ground it was on (formerGround reports that). editPlot keeps a graded
  plot graded, and removePlot takes its grading back. The grading follows the ground when it is renamed; when that ground is deleted,
  grade the plot again where it stands. A plot standing far off the ground around it is refused: move it or change its height.
  Shaping never goes into a plot's pass, which grading rewrites whole: the pass active before grading stays active, and on ground that
  had no passes none is, so add one (addShapingPass) before shaping it again. Look at the cut and fill slopes in a render; paint them
  like the rest of the ground."""
  return await callBridge(context, "gradePlot", {"address": address, "objectName": objectName, "margin": margin, "batterDegrees": batterDegrees})


@guardedTool()
async def assessPlot(context: Context, address: str):
  """Measure a plot where it lies: the ground under it (unevenness, tilt, the cut and fill to level it), what rises and falls beyond each
  side, its entrance point (for walkRoute from the street), water beside it, how high it stands over its surroundings, how enclosed it
  is, rock or roof over it (overhead: the height of its underside over the plot's center, or null), its nearest plot and route, how
  much of the zone's main routes see it, and overlaps; and the features those suggest, for pricing (set them with editPlot features).
  The ground under it, beyond its sides, and at its entrance is looked up from the plot's own height (ground standing above it, or the
  footing under a step over it), so a plot in a cave measures the cave's floor and walls, not the hill over it. A view is never
  suggested: judge it from pictures taken at the plot's edge, looking out as its owner would. The measures check what a picture shows;
  look at the plot too."""
  return await callBridge(context, "assessPlot", {"address": address})


@guardedTool()
async def layOutPlots(
  context: Context, street: str, path: list[list[float]], side: str = "both", kind: str = "player", size: list[float] | None = None,
  firstNumber: int = 101, gap: float = 20.0, setback: float = 20.0, items: int | None = None, pets: int | None = None,
  features: list[str] | None = None, collection: str | None = None,
):
  """A starting point for a row of plots along a street: stations along `path` [[x, y], ...] a plot's width plus `gap` apart, each plot
  `setback` from the path on `side` (left, right, or both, looking along the path), facing the street, addressed "<number> <street>".
  Every place takes the next number from firstNumber in order along the street (left before right at each station) whether or not a
  plot fits there, so an address says where along the street it stands and, on both sides, each side keeps its own odd or even
  numbers; places where a plot would overlap another, find no ground, or find rock over ground (a cave under a hill: place those with
  placePlot and their height) are skipped and listed with the address they leave free.
  Refused when any of its addresses is taken. Then look at each plot and adjust it (editPlot, gradePlot, assessPlot): a street of
  identical plots is a draft, not a neighborhood."""
  if kind not in ("player", "guild"):
    raise ToolError(f"kind is player or guild, got '{kind}'")
  folder = await anyio.to_thread.run_sync(borderModelFolder, kind)
  return await callBridge(context, "layOutPlots", {
    "street": street, "path": path, "side": side, "kind": kind, "size": size, "firstNumber": firstNumber, "gap": gap, "setback": setback,
    "items": items, "pets": pets, "tags": features, "borderFolder": folder, "collection": collection,
  })



serverSourcePaths = loadedServerSources()
serverSourceFingerprint = sourceSignatures(serverSourcePaths)


def main():
  toolingLog.configureLogging(toolingRoot)
  atexit.register(bridge.stop)
  server.run()


if __name__ == "__main__":
  main()
