import atexit
import datetime
import functools
import inspect
import sys
from pathlib import Path

import anyio.from_thread
import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError

import blenderBridge
import eqModels
import eqRaces
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


def guardedTool(**options):
  """server.tool that first refuses to run outdated server code."""
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
    return server.tool(**options)(guarded)
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
  """Compare installed Blender and extensions with the pins in toolingManifest.json, and report the bridge."""
  return toolingStatus.getToolingStatus(toolingRoot) | {
    "machineProfile": machineProfile.profileStatus(toolingRoot),
    "bridge": bridge.status(),
    "runPython": toolingLog.countRunPython(toolingRoot),
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
  fogColor: list[float] | None = None,
  fogStart: float | None = None,
  fogEnd: float | None = None,
  sunAzimuthDegrees: float | None = None,
  sunElevationDegrees: float | None = None,
  sunColor: list[float] | None = None,
  sunStrength: float | None = None,
  ambientColor: list[float] | None = None,
  newEngineZone: bool | None = None,
):
  """Set the zone's EQ properties stored in the .blend: fog color and distances (fogEnd is also the far clip), sun direction (azimuth 0 = +Y, clockwise), sun color and strength, ambient color, and newEngineZone, the zone header's NewEngineZone, which sets the scale the client draws spawns at (the live dumps' zoneHeaders give it per zone; EQEmu sends false for every zone)."""
  updates = {
    "fogColor": fogColor, "fogStart": fogStart, "fogEnd": fogEnd,
    "sunAzimuthDegrees": sunAzimuthDegrees, "sunElevationDegrees": sunElevationDegrees,
    "sunColor": sunColor, "sunStrength": sunStrength, "ambientColor": ambientColor, "newEngineZone": newEngineZone,
  }
  return await callBridge(context, "setZoneProperties", {"updates": {key: value for key, value in updates.items() if value is not None}})


@guardedTool()
async def renderView(context: Context, view: dict):
  """Render the EQ preview of a view: {"camera": name}, {"eye": [x,y,z], "target": [x,y,z]}, or {"standAt": [x,y,z], "headingDegrees": h, "pitchDegrees": p} (heading 0 = +Y, clockwise; eye 5.5 above the ground; adds a dark elf female of height 5, the race default, drawn as the client draws her in the zone (newEngineZone), walked ahead along the ground and facing the camera)."""
  outputPath = newRenderPath()
  figureModel = None
  zone = await callBridge(context, "getZoneProperties", {})
  # Without newEngineZone the preview's own check fails, naming it with any other missing zone property.
  if "standAt" in view and "newEngineZone" in zone:
    figure = await anyio.to_thread.run_sync(spawnModel, None, figureModelCode, figureHeight, bool(zone["newEngineZone"]))
    figureModel = {key: figure[key] for key in ("folder", "scale", "avatarHeight")}
  description = await callBridge(context, "renderView", {"view": view, "outputPath": str(outputPath), "figureModel": figureModel})
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
  " Head looks take the dumps' terms: faceStyle swaps a Luclin model's face materials; hairStyle and hairColor, and facialHair and"
  " facialHairColor (255 for none), attach a Luclin model's hair and beard items (IT<n> from the Luclin equipment archives) tinted"
  " by the client's color table, or a Drakkin's <code>_HAIR_<nn> piece. The result's source.unattached lists any the client would"
  " not attach either. The character is posed at"
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
  facialHair: int = 255, facialHairColor: int = 0,
  animation: str | None = None, animationVariant: str | None = None, animationFrame: int = 0,
  source: str | None = None, snapToGround: bool = True, collection: str | None = None,
):
  frameLocation, rotation = placementFrame(location, headingDegrees, x, y, z, heading)
  appearance = {
    "variation": variation, "headType": headType, "textureSet": textureSet, "faceStyle": faceStyle, "hairStyle": hairStyle, "hairColor": hairColor,
    "facialHair": facialHair, "facialHairColor": facialHairColor,
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


@guardedTool()
async def pick(context: Context, view: dict, pixel: list[int]):
  """What is under a pixel ([x, y] from the top-left of the 960x540 render) of a view: object, world position, normal, material, distance."""
  return await callBridge(context, "pick", {"view": view, "pixel": pixel})


selectorHelp = (
  " A selector picks part of a mesh by world position or surface: {\"all\": true}, {\"sphere\": {\"center\": [x,y,z], \"radius\": r}},"
  " {\"box\": {\"minimum\": [x,y,z], \"maximum\": [x,y,z]}}, {\"cylinder\": {\"center\": [x,y], \"radius\": r, \"bottom\": z, \"top\": z}},"
  " {\"facing\": {\"direction\": [x,y,z], \"withinDegrees\": d}}, {\"material\": name}, {\"vertexGroup\": name}, {\"insideObject\": closedMeshName},"
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


@guardedTool(description="Move the selected vertices of a mesh by `offset` [x, y, z] world units. With `falloff` {center, radius, curve: constant|linear|smooth|sharp} the move fades with distance from the center; this is the precise, fine-detail edit." + selectorHelp)
async def moveVertices(context: Context, objectName: str, selector: dict, offset: list[float], falloff: dict | None = None):
  return await callBridge(context, "moveVertices", {"objectName": objectName, "selector": selector, "offset": offset, "falloff": falloff})


@guardedTool()
async def sculptAtPoint(
  context: Context, objectName: str, mode: str, center: list[float], radius: float, strength: float,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1,
):
  """Sculpt a mesh within `radius` of `center`: raise, lower, or crease (strength in units, along the region's average normal or `direction`); smooth or flatten (strength a fraction 0 to 1; smooth repeats `iterations` times). Falloff curve: constant, linear, smooth, sharp."""
  return await callBridge(context, "sculptAtPoint", {"objectName": objectName, "mode": mode, "center": center, "radius": radius, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations})


@guardedTool()
async def sculptAlongPath(
  context: Context, objectName: str, mode: str, path: list[list[float]], radius: float, strength: float,
  falloff: str = "smooth", direction: list[float] | None = None, iterations: int = 1, profile: list[list[float]] | None = None, conformRim: bool = True,
):
  """Sculpt along a polyline path [[x,y,z], ...] within `radius`: raise, lower, crease, smooth, flatten as in sculptAtPoint, or carve, which cuts vertically down to the path's own heights shaped by `profile` [[lateralFraction, heightAboveFloor], ...] from 0 (center) to 1 (edge); carve strength is a fraction. With conformRim (needs rising profile heights), vertices just outside the cut slide onto the rim contour so the edge follows the profile rather than the grid."""
  return await callBridge(context, "sculptAlongPath", {"objectName": objectName, "mode": mode, "path": path, "radius": radius, "strength": strength, "falloff": falloff, "direction": direction, "iterations": iterations, "profile": profile, "conformRim": conformRim})


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
async def createMaterial(context: Context, name: str, diffuseTexture: str, normalTexture: str | None = None, cutout: bool = False, alphaThreshold: float = 0.5):
  """A Phase 1 material: diffuse texture (absolute path), optional normal map, no shine; cutout makes the diffuse alpha a hard alpha test for foliage cards."""
  return await callBridge(context, "createMaterial", {"name": name, "diffuseTexture": diffuseTexture, "normalTexture": normalTexture, "cutout": cutout, "alphaThreshold": alphaThreshold})


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
