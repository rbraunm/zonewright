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
up = mathutils.Vector((0.0, 0.0, 1.0))


def alignedRotation(normal, yawRadians):
  yaw = mathutils.Quaternion((0, 0, 1), yawRadians)
  return mathutils.Vector((0, 0, 1)).rotation_difference(mathutils.Vector(normal)) @ yaw


def slopeDegrees(normal):
  return math.degrees(math.acos(max(-1.0, min(1.0, normal[2]))))


def withDescendants(sceneObject):
  return [sceneObject] + list(sceneObject.children_recursive)


def landingSurfaces(surfaceObjects, carried):
  """What dressing lands on: the named objects, or what players stand on (water, guides, regions, spawns, and doors left out), never
  the objects being placed and what they carry."""
  excluding = {sceneObject.name for sceneObject in carried}
  if surfaceObjects is None:
    return bridgeMeshAccess.PlayerSurfaces(excluding)
  return bridgeMeshAccess.PlayerSurfaces(excluding, [bridgeMeshAccess.requireObject(name) for name in surfaceObjects])


def topOf(carried):
  """The highest point of the meshes and collection instances among objects, in world space."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  heights = [
    corner.z for sceneObject in carried if sceneObject.type == "MESH" or bridgeMeshAccess.isCollectionInstance(sceneObject)
    for corner in bridgeMeshAccess.worldBoundsCorners(sceneObject, depsgraph)
  ]
  if not heights:
    raise ValueError(f"'{carried[0].name}' and its children have no meshes to cast from above; pass at")
  return max(heights)


def worldHeading(rotation):
  """The turn about the vertical left once a world rotation's lean is taken off (a swing-twist split)."""
  swing = mathutils.Vector((0.0, 0.0, 1.0)).rotation_difference(rotation @ mathutils.Vector((0.0, 0.0, 1.0)))
  return (swing.inverted() @ rotation).to_euler("XYZ").z


def placeOnSurface(objectNames, at, alignToNormal, surfaceObjects, offset):
  if at is not None and len(at) != len(objectNames):
    raise ValueError(f"at has {len(at)} points for {len(objectNames)} objects")
  placing = [bridgeMeshAccess.requireObject(name) for name in objectNames]
  surfaces = landingSurfaces(surfaceObjects, [carried for sceneObject in placing for carried in withDescendants(sceneObject)])
  placements = []
  for index, sceneObject in enumerate(placing):
    bpy.context.view_layer.update()
    origin = sceneObject.matrix_world.translation
    start = mathutils.Vector(at[index]) + up * castLift if at is not None else mathutils.Vector((origin.x, origin.y, topOf(withDescendants(sceneObject)) + castLift))
    hit = surfaces.footingOn(start, maximumCastDistance)
    if hit is None:
      raise ValueError(f"No surface below {bridgeObjects.roundVector(start)} for '{sceneObject.name}'" + ("" if at is not None else ", cast from just above its top"))
    location, normal, surfaceName = hit
    _, rotation, scale = sceneObject.matrix_world.decompose()
    if alignToNormal:
      rotation = alignedRotation(normal, worldHeading(rotation))
    sceneObject.matrix_world = mathutils.Matrix.LocRotScale(location + (normal if alignToNormal else up) * offset, rotation, scale)
    bpy.context.view_layer.update()
    placements.append({
      "object": sceneObject.name, "location": bridgeObjects.roundVector(sceneObject.matrix_world.translation), "surface": surfaceName,
      "normal": bridgeObjects.roundVector(normal), "slopeDegrees": round(slopeDegrees(normal), 2),
    })
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
  surfaces = landingSurfaces(surfaceObjects, withDescendants(source))
  rejected = {"noSurface": 0, "tooSteep": 0, "nearAvoidedObject": 0}
  landings = []
  depsgraph = bpy.context.evaluated_depsgraph_get()
  for point in candidates:
    hit = surfaces.footingOn(mathutils.Vector((point[0], point[1], castHeight)), maximumCastDistance)
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
    factor = generator.uniform(*scaleRange)
    instance.scale = [component * factor for component in source.scale]
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
  bpy.context.view_layer.update()
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
