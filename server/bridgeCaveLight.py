"""Light in caves. The share of scene light each cave's lining takes (its runs' daylight, eased along them as their widths are) is worked
out where it is used and never stored on the mesh: a client-shaded preview draws a terrain holding daylit caves from a copy carrying it
as the share of scene light its lining takes, and export writes it as the terrain placement's baked light (alpha the share). Point
lights anchored on a cave's lining (placeLights onCave) keep their anchor and are placed again whenever their cave is cut again. Runs
under Blender's Python."""
import json

import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeCaveData
import bridgeCaveRuns
import bridgeCaves
import bridgeClientLight
import bridgeMeshAccess
import bridgePointLights
import clientPointLights

anchorProperty = "zonewrightCaveAnchor"
anchorKeys = ({"objectName", "cave", "at", "side", "out"}, {"run", "over"})
anchorSides = ("left", "right", "ceiling")
previewCopySuffix = "Daylit"
# A lining vertex takes the daylight of the run whose middle (half its height over its floor) is nearest, sampled this often along it.
sampleSpacing = 2.0


def daylitCaves(sceneObject):
  """The caves of a mesh that set daylight on any run."""
  daylit = []
  for name, record in sorted(bridgeCaveData.caves(sceneObject).items()):
    definition = record["definition"]
    if definition.get("daylight") is not None or any(branch.get("daylight") is not None for branch in definition.get("branches") or []):
      daylit.append(name)
  return daylit


def daylightShares(sceneObject):
  """Each vertex's share of scene light: its cave's run's daylight eased along the run for a lining vertex of a daylit cave (1 for a run
  that sets none), 1 for the ground; None for a mesh without daylit caves."""
  names = daylitCaves(sceneObject)
  if not names:
    return None
  mesh = sceneObject.data
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  shares = numpy.ones(len(mesh.vertices))
  known = bridgeCaveData.caves(sceneObject)
  for name in names:
    record = known[name]
    definition = bridgeCaves.caveDefinition(**record["definition"])
    lines = bridgeCaves.recordedLines(name, record)
    middles, values = [], []
    for runName, run, _ in bridgeCaves.runSpecs(definition):
      line = lines[runName]
      alongs = line.samples(sampleSpacing)
      floors, _, _, heights = line.at(alongs)
      middles.append(floors + numpy.column_stack([numpy.zeros((len(alongs), 2)), heights / 2]))
      values.append(numpy.ones(len(alongs)) if run["daylight"] is None else line.eased(run["daylight"], alongs))
    middles, values = numpy.vstack(middles), numpy.concatenate(values)
    tree = mathutils.kdtree.KDTree(len(middles))
    for index, point in enumerate(middles.tolist()):
      tree.insert(point, index)
    tree.balance()
    lining = numpy.flatnonzero(bridgeCaveData.attributeValues(mesh, bridgeCaveData.vertexTagPrefix + name) == bridgeCaveData.liningTag)
    shares[lining] = [values[tree.find(point)[1]] for point in shown[lining].tolist()]
  return shares


def applyDaylight(preview):
  """Draw each terrain holding daylit caves from a copy carrying the share of scene light (as the shade, 1 less the share, that the
  client light group reads; a mesh without it is in full scene light); the copies replace their originals in the preview scene only."""
  depsgraph = preview.depsgraph()
  copied = []
  for sceneObject in list(preview.scene.collection.objects):
    if sceneObject.type != "MESH" or not bridgeCaveData.holdsCaves(sceneObject):
      continue
    shares = daylightShares(sceneObject)
    if shares is None:
      continue
    mesh = bpy.data.meshes.new_from_object(sceneObject.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
    mesh.name = f"{sceneObject.name}{previewCopySuffix}"
    mesh.attributes.new(bridgeClientLight.caveShadeAttribute, "FLOAT", "POINT").data.foreach_set("value", (1.0 - shares).astype(numpy.float32))
    copy = bpy.data.objects.new(f"{sceneObject.name}{previewCopySuffix}", mesh)
    copy.matrix_world = sceneObject.matrix_world.copy()
    for key in sceneObject.keys():
      copy[key] = sceneObject[key]
    copy[bridgePointLights.previewTerrainProperty] = True
    preview.addObject(copy)
    preview.scene.collection.objects.unlink(sceneObject)
    copied.append(sceneObject.name)
  return copied


# Anchored lights

def anchorDefinition(anchor):
  """An anchor as a light keeps it: {objectName, cave, run, at, side, over, out}, refusing one that is not well formed."""
  required, optional = anchorKeys
  if not isinstance(anchor, dict):
    raise ValueError(f"onCave is {{objectName, cave, run, at, side, over, out}}, got {anchor!r}")
  missing, unknown = sorted(required - set(anchor)), sorted(set(anchor) - required - optional)
  if missing or unknown:
    raise ValueError(f"onCave takes {sorted(required)} and optionally {sorted(optional)}; missing {missing}, unknown {unknown}")
  if anchor["side"] not in anchorSides:
    raise ValueError(f"onCave side is {list(anchorSides)}: a wall looking along the run, or its vault, got {anchor['side']!r}")
  over = anchor.get("over")
  if anchor["side"] == "ceiling" and over is not None:
    raise ValueError("A light on a cave's ceiling hangs under its vault over the run's middle: give no `over`")
  if anchor["side"] != "ceiling" and (over is None or not over > 0):
    raise ValueError(f"A light on a cave's wall stands `over` its floor, a positive height, got {over!r}")
  if not anchor["out"] >= 0:
    raise ValueError(f"onCave out is how far the light stands out from the rock into the cave, at least 0, got {anchor['out']!r}")
  return {
    "objectName": anchor["objectName"], "cave": anchor["cave"], "run": anchor.get("run", bridgeCaveRuns.mainRun),
    "at": bridgeCaveRuns.requirePosition(anchor["at"], "onCave at"), "side": anchor["side"], "over": None if over is None else float(over),
    "out": float(anchor["out"]),
  }


def anchoredPosition(anchor):
  """Where an anchored light stands on its cave's lining as cut: from the run's middle at `at` (at `over` its floor), out toward its
  wall (or up to its vault) to the lining, and `out` from it into the cave; with the floor's point below it. Refuses an anchor whose
  point lies in rock or outside the cave, and an unknown cave or run."""
  sceneObject = bridgeMeshAccess.requireMeshObject(anchor["objectName"])
  record = bridgeCaves.requireCave(sceneObject, anchor["cave"])
  lines = bridgeCaves.recordedLines(anchor["cave"], record)
  if anchor["run"] not in lines:
    raise ValueError(f"Cave '{anchor['cave']}' of '{anchor['objectName']}' has no run '{anchor['run']}'; its runs are {list(lines)}")
  line = lines[anchor["run"]]
  along = bridgeCaveRuns.positionAlong(line, anchor["at"], "onCave at")
  if not 0 <= along <= line.length:
    raise ValueError(f"onCave at {along:.1f} is off run '{anchor['run']}', which runs from 0 to {line.length:.1f}")
  floors, directions, widths, heights = line.at(numpy.array([along]))
  floor, direction, width, height = floors[0], directions[0], float(widths[0]), float(heights[0])
  right = numpy.array([direction[1], -direction[0], 0.0])
  if anchor["side"] == "ceiling":
    origin, toward, reach = floor + [0.0, 0.0, 1.0], numpy.array([0.0, 0.0, 1.0]), 3 * height
  else:
    origin, toward, reach = floor + [0.0, 0.0, anchor["over"]], right if anchor["side"] == "right" else -right, 2 * width
  lining = bridgeCaveData.faceTags(sceneObject, anchor["cave"]) == bridgeCaveData.liningFaceTag
  depsgraph = bpy.context.evaluated_depsgraph_get()
  inverse = sceneObject.matrix_world.inverted()
  localOrigin = inverse @ mathutils.Vector(origin.tolist())
  localToward = (inverse.to_3x3() @ mathutils.Vector(toward.tolist())).normalized()
  hit, location, normal, index = sceneObject.ray_cast(localOrigin, localToward, distance=reach, depsgraph=depsgraph)
  where = f"{anchor['side']} of cave '{anchor['cave']}' run '{anchor['run']}' at {along:.1f} along it" + ("" if anchor["over"] is None else f", {anchor['over']:g} over its floor")
  if not hit or not lining[index] or normal.dot(localToward) >= 0:
    raise ValueError(f"The light's anchor on the {where} lies in rock or outside the cave (no lining faces it toward {roundedPoint(origin)}); move it along the run or lower it")
  normalWorld = (sceneObject.matrix_world.to_3x3().inverted().transposed() @ normal).normalized()
  position = sceneObject.matrix_world @ location + normalWorld * anchor["out"]
  return [float(value) for value in position], [float(value) for value in floor]


def roundedPoint(point):
  return [round(float(value), 1) for value in point]


def placeAnchoredLights(sceneObject, cave):
  """Place again every light anchored on a cave just cut, on its lining as it now stands; refused (the cut with it) where an anchor no
  longer finds its lining, naming the light."""
  placed = []
  for lightObject in [candidate for candidate in bpy.data.objects if candidate.type == "LIGHT" and anchorProperty in candidate]:
    anchor = json.loads(lightObject[anchorProperty])
    if anchor["objectName"] != sceneObject.name or anchor["cave"] != cave:
      continue
    try:
      position, _ = anchoredPosition(anchor)
    except ValueError as error:
      raise ValueError(f"The light '{lightObject.name}' anchored on cave '{cave}' would no longer stand on it: {error}. Move its anchor (placeLights) or delete it") from error
    placed.append((lightObject, position))
  for lightObject, position in placed:
    lightObject.location = position
  return [{"light": lightObject.name, "position": roundedPoint(position)} for lightObject, position in placed]


def anchoredLights(sceneObject, cave):
  return sorted(lightObject.name for lightObject in bpy.data.objects if lightObject.type == "LIGHT" and anchorProperty in lightObject
                and json.loads(lightObject[anchorProperty])["objectName"] == sceneObject.name and json.loads(lightObject[anchorProperty])["cave"] == cave)


def anchoredReport(lightObject, floor):
  return {"light": lightObject.name, "onCave": json.loads(lightObject[anchorProperty]), "position": roundedPoint(lightObject.location), "floorBelow": roundedPoint(floor),
          "lightsTerrain": clientPointLights.lightsBakedGeometry(lightObject.name)}
