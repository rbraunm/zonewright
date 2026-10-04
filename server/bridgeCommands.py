"""Blender-side command handlers, independent of the loop that delivers them. Runs under Blender's Python."""
import contextlib
import io
import json
import math
import os

import bpy

import bridgeArrangement
import bridgeAuthoring
import bridgeBoundaries
import bridgeCaves
import bridgeDressing
import bridgeEnvironment
import bridgeExportChecks
import bridgeGrading
import bridgeHousing
import bridgePasses
import bridgeReview
import bridgeReviewGuides
import bridgeModels
import bridgeObjects
import bridgeShaping
import bridgeSketch
import bridgeSwim
import bridgeSurfacing
import bridgeViews
import bridgeWater
import skyDrawing
from bridgeState import requireNoUnsavedChanges, state

zonePropertyName = "zonewrightZone"
zonePropertyKeys = (
  "ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "sunAzimuthDegrees", "sunElevationDegrees", "fogColor", "fogStart", "fogEnd",
  "fogDensity", "fogOn", "minClip", "maxClip", "newEngineZone", "sky", "safePoint", "underworld",
)
# The client raises a lower minimum clip to this (eqgame 0x4c9ee6).
clientMinimumClip = 50.0
skyKeys = {"type": str, "weather": str, "hour": int, "minute": int}
fileImageSources = ("FILE", "SEQUENCE", "TILED")


def runPython(code):
  state.namespace.pop("result", None)
  state.unsavedChanges = True
  output = io.StringIO()
  with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    exec(compile(code, "<runPython>", "exec"), state.namespace)
  result = state.namespace.get("result")
  try:
    json.dumps(result)
  except TypeError as error:
    raise TypeError(f"result is not JSON-serializable: {error}") from error
  return {"output": output.getvalue(), "result": result}


def newFile(discardUnsavedChanges):
  requireNoUnsavedChanges(discardUnsavedChanges, "start a new file")
  bpy.ops.wm.read_homefile(use_empty=True)
  state.unsavedChanges = False
  state.resetNamespace()
  return getStatus()


def openFile(path, discardUnsavedChanges):
  if not os.path.isabs(path) or not path.lower().endswith(".blend"):
    raise ValueError(f"'{path}' is not an absolute path to a .blend file")
  if not os.path.isfile(path):
    raise FileNotFoundError(f"'{path}' does not exist")
  requireNoUnsavedChanges(discardUnsavedChanges, f"open {path}")
  bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
  state.unsavedChanges = False
  state.resetNamespace()
  return getStatus()


def externalFileProblems(targetPath):
  """Every texture and library must be a separate file on the .blend's drive, so it can be stored as a relative path."""
  targetDrive = os.path.splitdrive(os.path.abspath(targetPath))[0].lower()
  problems = []
  for image in bpy.data.images:
    # Images nothing uses (such as the scale figure's textures between renders) are not written to the file.
    if image.users == 0:
      continue
    if image.packed_file is not None:
      problems.append(f"image '{image.name}' is packed into the .blend")
      continue
    if image.source == "GENERATED":
      problems.append(f"image '{image.name}' is generated in memory; save it to a file first")
      continue
    if image.source not in fileImageSources:
      continue
    if image.library is not None:
      # A linked image's path is relative to its own library and saved there, not in this file.
      linkedPath = os.path.abspath(bpy.path.abspath(image.filepath, library=image.library))
      if not os.path.isfile(linkedPath):
        problems.append(f"image '{image.name}' linked from '{image.library.name}' file '{linkedPath}' does not exist")
      continue
    imagePath = os.path.abspath(bpy.path.abspath(image.filepath))
    if not os.path.isfile(imagePath):
      problems.append(f"image '{image.name}' file '{imagePath}' does not exist")
    elif os.path.splitdrive(imagePath)[0].lower() != targetDrive:
      problems.append(f"image '{image.name}' file '{imagePath}' is on another drive and cannot be a relative path")
  for library in bpy.data.libraries:
    libraryPath = os.path.abspath(bpy.path.abspath(library.filepath))
    if library.packed_file is not None or os.path.splitdrive(libraryPath)[0].lower() != targetDrive:
      problems.append(f"library '{library.name}' must be an unpacked file on the .blend's drive")
  return problems


def saveFile(path, replaceExisting):
  targetPath = path if path is not None else bpy.data.filepath
  if not targetPath:
    raise ValueError("The open file has never been saved; pass a path")
  if not os.path.isabs(targetPath) or not targetPath.lower().endswith(".blend"):
    raise ValueError(f"'{targetPath}' is not an absolute path to a .blend file")
  openPath = bpy.data.filepath
  if os.path.exists(targetPath) and not (openPath and os.path.normcase(os.path.abspath(openPath)) == os.path.normcase(os.path.abspath(targetPath))) and not replaceExisting:
    raise ValueError(f"'{targetPath}' already holds a file other than the open one; pass replaceExisting true to write over it")
  if not os.path.isdir(os.path.dirname(targetPath)):
    raise FileNotFoundError(f"folder '{os.path.dirname(targetPath)}' does not exist")
  problems = externalFileProblems(targetPath)
  if problems:
    raise ValueError("Cannot save: " + "; ".join(problems))
  bpy.ops.wm.save_as_mainfile(filepath=targetPath, relative_remap=True)
  bpy.ops.file.make_paths_relative()
  bpy.ops.wm.save_mainfile()
  absolutePaths = [image.filepath for image in bpy.data.images if image.source in fileImageSources and image.library is None and not image.filepath.startswith("//")]
  if absolutePaths:
    raise RuntimeError(f"Saved, but these image paths are still absolute: {absolutePaths}")
  state.unsavedChanges = False
  return getStatus()


def getStatus():
  return {"filePath": bpy.data.filepath or None, "unsavedChanges": state.unsavedChanges, "scene": bpy.context.scene.name}


def roundVector(vector, digits=2):
  return [round(float(component), digits) for component in vector]


def materialTextures(material):
  if material is None or material.node_tree is None:
    return []
  return sorted({node.image.name for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image is not None})


def readZoneProperties(scene):
  stored = scene.get(zonePropertyName)
  return stored.to_dict() if stored is not None else {}


def getSceneSummary(objectLimit):
  scene = bpy.context.scene
  depsgraph = bpy.context.evaluated_depsgraph_get()
  objects = []
  totalTriangles = 0
  for sceneObject in sorted(scene.objects, key=lambda candidate: candidate.name):
    triangles = None
    if sceneObject.type == "MESH":
      evaluated = sceneObject.evaluated_get(depsgraph)
      mesh = evaluated.to_mesh()
      mesh.calc_loop_triangles()
      triangles = len(mesh.loop_triangles)
      evaluated.to_mesh_clear()
      totalTriangles += triangles
    objects.append({
      "name": sceneObject.name,
      "type": sceneObject.type,
      "collections": [collection.name for collection in sceneObject.users_collection],
      "location": roundVector(sceneObject.matrix_world.translation),
      "dimensions": bridgeObjects.objectDimensions(sceneObject),
      "triangles": triangles,
      "materials": [slot.material.name if slot.material else None for slot in sceneObject.material_slots],
      "hiddenInRender": sceneObject.hide_render,
    })
  return {
    "status": getStatus(),
    "zoneProperties": readZoneProperties(scene),
    "objectCount": len(objects),
    "triangleCount": totalTriangles,
    "objects": objects[:objectLimit],
    "objectsOmitted": max(0, len(objects) - objectLimit),
    "collections": sorted(collection.name for collection in bpy.data.collections),
    "cameras": sorted(sceneObject.name for sceneObject in scene.objects if sceneObject.type == "CAMERA"),
    "materials": [{"name": material.name, "textures": materialTextures(material)} for material in sorted(bpy.data.materials, key=lambda candidate: candidate.name)],
    "libraries": [{"name": library.name, "filePath": library.filepath} for library in sorted(bpy.data.libraries, key=lambda candidate: candidate.name)],
    "images": [{
      "name": image.name,
      "source": image.source,
      "filePath": image.filepath or None,
      "size": list(image.size),
      "packed": image.packed_file is not None,
    } for image in sorted(bpy.data.images, key=lambda candidate: candidate.name)],
  }


def validateColor(name, color):
  if not isinstance(color, list) or len(color) != 3 or not all(isinstance(component, (int, float)) and 0 <= component <= 1 for component in color):
    raise ValueError(f"{name} must be three numbers from 0 to 1, got {color!r}")


def drawsSky(zone):
  return zone.get("sky", skyDrawing.noSky) != skyDrawing.noSky


def validateSky(sky):
  if not isinstance(sky, dict) or sorted(set(sky) - set(skyKeys)) or not {"type", "hour", "minute"} <= set(sky):
    raise ValueError(f"sky must be {{type, weather (optional), hour, minute}}, got {sky!r}")
  for key, value in sky.items():
    if not isinstance(value, skyKeys[key]) or isinstance(value, bool) or (isinstance(value, str) and not value):
      raise ValueError(f"sky {key} must be a {skyKeys[key].__name__}, got {value!r}")
  if not 0 <= sky["hour"] <= 23 or not 0 <= sky["minute"] <= 59:
    raise ValueError(f"sky hour must be 0-23 and minute 0-59, got {sky['hour']}:{sky['minute']}")


def isFiniteNumber(value):
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validatePlayerValues(zone):
  """The safe point [x, y, z, headingDegrees] where players arrive (heading 0 = +Y, clockwise) and the underworld height below it,
  under which the client puts a falling player back."""
  if "safePoint" in zone:
    point = zone["safePoint"]
    if not isinstance(point, list) or len(point) != 4 or not all(isFiniteNumber(value) for value in point):
      raise ValueError(f"safePoint is [x, y, z, headingDegrees], got {point!r}")
    if not 0 <= point[3] < 360:
      raise ValueError(f"safePoint's headingDegrees runs from 0 up to 360, got {point[3]}")
  if "underworld" in zone and not isFiniteNumber(zone["underworld"]):
    raise ValueError(f"underworld is a height, got {zone['underworld']!r}")
  if "safePoint" in zone and "underworld" in zone and zone["underworld"] >= zone["safePoint"][2]:
    raise ValueError(f"underworld {zone['underworld']} must lie below the safe point's height {zone['safePoint'][2]}")


def setZoneProperties(updates):
  """Store zone properties. A sky supplies the light and the fog color, so setting one drops those that were set by hand, and they
  cannot be set while it stays; sky "none" (skyDrawing.noSky) states the zone draws none."""
  unknownKeys = sorted(set(updates) - set(zonePropertyKeys))
  if unknownKeys:
    raise ValueError(f"Unknown zone properties {unknownKeys}; known: {list(zonePropertyKeys)}")
  zone = readZoneProperties(bpy.context.scene) | updates
  replaced = []
  if drawsSky(zone):
    validateSky(zone["sky"])
    supplied = sorted(set(updates) & set(skyDrawing.suppliedZoneKeys))
    if supplied:
      raise ValueError(f"The zone's sky supplies {supplied}; state that it draws none (sky \"none\") to set them by hand")
    replaced = sorted(set(zone) & set(skyDrawing.suppliedZoneKeys))
    for key in replaced:
      del zone[key]
  for colorKey in ("ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "fogColor"):
    if colorKey in zone:
      validateColor(colorKey, zone[colorKey])
  if "fogStart" in zone and zone["fogStart"] < 0:
    raise ValueError(f"fogStart must be at least 0, got {zone['fogStart']}")
  if "fogStart" in zone and "fogEnd" in zone and zone["fogEnd"] <= zone["fogStart"]:
    raise ValueError(f"fogEnd {zone['fogEnd']} must be greater than fogStart {zone['fogStart']}")
  if "sunElevationDegrees" in zone and not -90 <= zone["sunElevationDegrees"] <= 90:
    raise ValueError(f"sunElevationDegrees must be in [-90, 90], got {zone['sunElevationDegrees']}")
  if "fogDensity" in zone and zone["fogDensity"] < 0:
    raise ValueError(f"fogDensity must be at least 0, got {zone['fogDensity']}")
  if "fogOn" in zone and not isinstance(zone["fogOn"], bool):
    raise ValueError(f"fogOn must be true or false, got {zone['fogOn']!r}")
  if "minClip" in zone and zone["minClip"] < clientMinimumClip:
    raise ValueError(f"minClip must be at least {clientMinimumClip:g}, as the client raises any lower one to it; got {zone['minClip']}")
  if "minClip" in zone and "maxClip" in zone and zone["maxClip"] <= zone["minClip"]:
    raise ValueError(f"maxClip {zone['maxClip']} must be greater than minClip {zone['minClip']}")
  if "maxClip" in zone and "fogStart" in zone and zone["maxClip"] <= zone["fogStart"]:
    raise ValueError(f"maxClip {zone['maxClip']} must be greater than fogStart {zone['fogStart']}: nothing would be drawn far enough to fog")
  if "newEngineZone" in zone and not isinstance(zone["newEngineZone"], bool):
    raise ValueError(f"newEngineZone must be true or false, got {zone['newEngineZone']!r}")
  validatePlayerValues(zone)
  bpy.context.scene[zonePropertyName] = zone
  return {"zone": readZoneProperties(bpy.context.scene), "replacedBySky": replaced}


def getZoneProperties():
  return readZoneProperties(bpy.context.scene)


def previewZone(sky):
  """The zone's properties for a preview, with what its sky supplies: the server resolves the stored sky against the client's files
  and passes its state (eqSky.skyState)."""
  zone = readZoneProperties(bpy.context.scene)
  if drawsSky(zone) != (sky is not None):
    raise ValueError("The zone's sky and the sky state passed for it disagree")
  return zone | sky["environment"] if sky is not None else zone


def renderView(view, outputPath, figureModel, shading, bandHeight, guides, sky, swimVolumes):
  return bridgeViews.renderView(bpy.context.scene, previewZone(sky), sky, view, outputPath, figureModel, shading, bandHeight, guides, swimVolumes)


def pick(view, pixel, sky):
  return bridgeViews.pick(bpy.context.scene, previewZone(sky), view, pixel)


def renderPasses(view, outputFolder, passNames, sky):
  return bridgeViews.renderPasses(bpy.context.scene, previewZone(sky), view, outputFolder, passNames)


commands = {
  "runPython": (runPython, False),
  "newFile": (newFile, False),
  "openFile": (openFile, False),
  "saveFile": (saveFile, False),
  "getStatus": (getStatus, False),
  "getSceneSummary": (getSceneSummary, False),
  "setZoneProperties": (setZoneProperties, True),
  "getZoneProperties": (getZoneProperties, False),
  "renderView": (renderView, False),
  "pick": (pick, False),
  "renderPasses": (renderPasses, False),
  "renderModelThumbnails": (bridgeViews.renderModelThumbnails, False),
} | bridgeObjects.commands | bridgeShaping.commands | bridgeSurfacing.commands | bridgeDressing.commands | bridgeModels.commands | bridgeExportChecks.commands | bridgePasses.commands | bridgeEnvironment.commands | bridgeReview.commands | bridgeReviewGuides.commands | bridgeAuthoring.commands | bridgeWater.commands | bridgeHousing.commands | bridgeGrading.commands | bridgeSketch.commands | bridgeSwim.commands | bridgeArrangement.commands | bridgeBoundaries.commands | bridgeCaves.commands


def dispatch(command, arguments):
  """Run a command with keyword arguments; commands that change the scene mark it unsaved."""
  if command not in commands:
    raise ValueError(f"Unknown bridge command '{command}'")
  handler, changesScene = commands[command]
  result = handler(**arguments)
  if changesScene:
    state.unsavedChanges = True
  return result
