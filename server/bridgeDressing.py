"""Dressing a scene: snapping to surfaces, scattering, and linking kit assets. Runs under Blender's Python."""
import math
import os

import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgeObjects

densityArea = 10000.0
scatterAttemptsPerTarget = 30
castLift = 1.0
maximumCastDistance = 100000.0


def castDown(start, surfaceObjects):
  return bridgeMeshAccess.rayCast(start, (0, 0, -1), maximumCastDistance, surfaceObjects)


def alignedRotation(normal, yawRadians):
  yaw = mathutils.Quaternion((0, 0, 1), yawRadians)
  return mathutils.Vector((0, 0, 1)).rotation_difference(mathutils.Vector(normal)) @ yaw


def slopeDegrees(normal):
  return math.degrees(math.acos(max(-1.0, min(1.0, normal[2]))))


def placeOnSurface(objectNames, at, alignToNormal, surfaceObjects, offset):
  if at is not None and len(at) != len(objectNames):
    raise ValueError(f"at has {len(at)} points for {len(objectNames)} objects")
  placements = []
  for index, name in enumerate(objectNames):
    sceneObject = bridgeMeshAccess.requireObject(name)
    start = mathutils.Vector(at[index]) if at is not None else sceneObject.matrix_world.translation.copy()
    with bridgeMeshAccess.hiddenObjects([name]):
      hit = castDown(start + mathutils.Vector((0, 0, castLift)), surfaceObjects)
    if hit is None:
      raise ValueError(f"No surface below {list(start)} for '{name}'")
    location, normal, _, hitObject = hit
    sceneObject.location = location + normal * offset if alignToNormal else location + mathutils.Vector((0, 0, offset))
    if alignToNormal:
      if sceneObject.rotation_mode not in bridgeObjects.eulerModes:
        raise ValueError(f"'{name}' rotates by {sceneObject.rotation_mode}; aligning to the surface keeps its heading and needs an Euler rotation mode")
      heading = sceneObject.rotation_euler.to_matrix().to_euler("XYZ").z
      sceneObject.rotation_mode = "XYZ"
      sceneObject.rotation_euler = alignedRotation(normal, heading).to_euler("XYZ")
    placements.append({"object": name, "location": bridgeObjects.roundVector(sceneObject.location), "surface": hitObject.name, "normal": bridgeObjects.roundVector(normal), "slopeDegrees": round(slopeDegrees(normal), 2)})
  bpy.context.view_layer.update()
  return {"placements": placements}


def regionSampler(region):
  if not isinstance(region, dict) or len(region) != 1 or next(iter(region)) not in ("circle", "polygon"):
    raise ValueError(f"region is {{\"circle\": {{center, radius}}}} or {{\"polygon\": [[x, y], ...]}}, got {region!r}")
  if "circle" in region:
    center = numpy.array(region["circle"]["center"], dtype=numpy.float64)
    radius = region["circle"]["radius"]
    return math.pi * radius * radius, center - radius, center + radius, lambda points: numpy.linalg.norm(points - center, axis=1) <= radius
  polygon = numpy.array(region["polygon"], dtype=numpy.float64)
  if len(polygon) < 3:
    raise ValueError("A polygon region needs at least three points")
  area = abs(numpy.dot(polygon[:, 0], numpy.roll(polygon[:, 1], -1)) - numpy.dot(polygon[:, 1], numpy.roll(polygon[:, 0], -1))) / 2

  def inside(points):
    result = numpy.zeros(len(points), dtype=bool)
    for start, end in zip(polygon, numpy.roll(polygon, -1, axis=0)):
      crosses = (start[1] > points[:, 1]) != (end[1] > points[:, 1])
      with numpy.errstate(divide="ignore", invalid="ignore"):
        intersectX = start[0] + (points[:, 1] - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
      result ^= crosses & (points[:, 0] < intersectX)
    return result

  return area, polygon.min(0), polygon.max(0), inside


def spacedPoints(region, targetCount, minimumSpacing, generator):
  _, minimum, maximum, inside = regionSampler(region)
  accepted = []
  cellSize = minimumSpacing / math.sqrt(2) if minimumSpacing > 0 else None
  grid = {}
  for _ in range(targetCount * scatterAttemptsPerTarget):
    if len(accepted) == targetCount:
      break
    candidate = generator.uniform(minimum, maximum)
    if not inside(candidate[None])[0]:
      continue
    if cellSize is not None:
      cell = (int(candidate[0] // cellSize), int(candidate[1] // cellSize))
      neighbours = [grid[(cell[0] + dx, cell[1] + dy)] for dx in range(-2, 3) for dy in range(-2, 3) if (cell[0] + dx, cell[1] + dy) in grid]
      if any(numpy.linalg.norm(candidate - accepted[index]) < minimumSpacing for index in neighbours):
        continue
      grid[cell] = len(accepted)
    accepted.append(candidate)
  return accepted


def scatterInRegion(sourceObject, region, density, minimumSpacing, yawRangeDegrees, scaleRange, alignToNormal, maximumSlopeDegrees, surfaceObjects, castFromHeight, seed, collection, avoidObjects, avoidClearance):
  source = bridgeMeshAccess.requireObject(sourceObject)
  if avoidClearance < 0:
    raise ValueError(f"avoidClearance must be non-negative, got {avoidClearance}")
  avoided = [bridgeMeshAccess.requireMeshObject(name) for name in avoidObjects or []]
  area = regionSampler(region)[0]
  targetCount = round(density * area / densityArea)
  if targetCount < 1:
    raise ValueError(f"density {density} per {densityArea:.0f} square units over {area:.0f} square units places no objects")
  if minimumSpacing < 0 or not 0 < scaleRange[0] <= scaleRange[1] or yawRangeDegrees[0] > yawRangeDegrees[1]:
    raise ValueError("minimumSpacing must be non-negative, and scaleRange and yawRangeDegrees must be [low, high] (scales positive)")
  generator = numpy.random.default_rng(seed)
  candidates = spacedPoints(region, targetCount, minimumSpacing, generator)
  destination = bridgeObjects.targetCollection(collection)
  castHeight = castFromHeight if castFromHeight is not None else bridgeMeshAccess.sceneTopHeight() + castLift
  rejected = {"noSurface": 0, "tooSteep": 0, "nearAvoidedObject": 0}
  landings = []
  depsgraph = bpy.context.evaluated_depsgraph_get()
  with bridgeMeshAccess.hiddenObjects([source.name]):
    for point in candidates:
      hit = castDown((point[0], point[1], castHeight), surfaceObjects)
      if hit is None:
        rejected["noSurface"] += 1
      elif slopeDegrees(hit[1]) > maximumSlopeDegrees:
        rejected["tooSteep"] += 1
      elif any(distance <= avoidClearance or inside for distance, inside in (bridgeMeshAccess.closestOnObject(container, hit[0], depsgraph) for container in avoided)):
        rejected["nearAvoidedObject"] += 1
      else:
        landings.append(hit[:2])
  placed = []
  for location, normal in landings:
    instance = source.copy()
    instance.location = location
    yaw = math.radians(generator.uniform(*yawRangeDegrees))
    instance.rotation_mode = "XYZ"
    instance.rotation_euler = (alignedRotation(normal, yaw) if alignToNormal else mathutils.Quaternion((0, 0, 1), yaw)).to_euler("XYZ")
    instance.scale = [generator.uniform(*scaleRange)] * 3
    destination.objects.link(instance)
    placed.append(instance.name)
  bpy.context.view_layer.update()
  return {
    "targetCount": targetCount,
    "spacedCandidates": len(candidates),
    "placed": len(placed),
    "rejected": rejected,
    "collection": destination.name,
    "sharedMesh": source.data.name if source.data is not None else None,
  }


def linkKitAsset(kitPath, assetName, instanceName, location, rotationDegrees, scale, collection):
  if not os.path.isabs(kitPath) or not os.path.isfile(kitPath):
    raise FileNotFoundError(f"Kit '{kitPath}' is not an existing absolute path to a .blend")
  bridgeObjects.requireNewName(instanceName)
  with bpy.data.libraries.load(kitPath, link=True, assets_only=True) as (dataFrom, dataTo):
    if assetName not in dataFrom.collections:
      raise ValueError(f"'{assetName}' is not a collection marked as an asset in {kitPath}; asset collections: {sorted(dataFrom.collections)}")
    dataTo.collections = [assetName]
  linked = dataTo.collections[0]
  instance = bpy.data.objects.new(instanceName, None)
  instance.instance_type = "COLLECTION"
  instance.instance_collection = linked
  instance.location = location
  instance.rotation_euler = [math.radians(angle) for angle in rotationDegrees]
  instance.scale = scale
  bridgeObjects.targetCollection(collection).objects.link(instance)
  return bridgeObjects.describeTransform(instance) | {"asset": assetName, "library": linked.library.filepath}


def markAsset(collectionName):
  collection = bpy.data.collections.get(collectionName)
  if collection is None:
    raise ValueError(f"No collection named '{collectionName}'")
  collection.asset_mark()
  return {"asset": collectionName, "objects": sorted(sceneObject.name for sceneObject in collection.all_objects)}


commands = {
  "markAsset": (markAsset, True),
  "placeOnSurface": (placeOnSurface, True),
  "scatterInRegion": (scatterInRegion, True),
  "linkKitAsset": (linkKitAsset, True),
}
