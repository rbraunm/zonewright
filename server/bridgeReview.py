"""The open scene's zone gathered as the zone survey gathers a client zone, so it is measured by the same methods: what export ships
(bridgeExport.exportedObjects), the terrain collection's meshes as its terrain and every other mesh and collection instance placed on
it. Runs under Blender's Python."""
import os

import math

import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeExport
import bridgeMeshAccess
from playerScale import playerHeight, stepHeight, walkableNormalZ

# How far a route looks to each side for a drop or a wall, and how finely; how far above for a ceiling; and how far below it still
# finds footing.
routeSideReach = 60.0
routeSideStep = 2.0
routeHeadroomReach = 60.0
routeFootingReach = 60.0
routeProfileRows = 60
# A route is walked in strides no longer than this whatever its sampleSpacing, so a step, a wall, or a slope is judged alike at any
# spacing; sampleSpacing sets only the profile's rows.
routeStride = 0.5
# Casts start this far short of each spot and this far above a footing, so geometry built on round numbers (a block's face exactly on
# a spot) is not met edge on.
castNudge = bridgeMeshAccess.castNudge
# What blocks a step is climbed in rises this tall, then its top found within a thirty-second of one.
obstacleClimb = 1.0
obstacleRefinements = 5
steepestWalkableDegrees = math.degrees(math.acos(walkableNormalZ))
up = bridgeMeshAccess.up
down = bridgeMeshAccess.down


def triangulated(sceneObject, depsgraph, matrix):
  """World positions, triangles, and each triangle's material name, from the mesh as evaluated."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    coordinates = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", coordinates)
    positions = coordinates.reshape(-1, 3) @ numpy.array(matrix)[:3, :3].T + numpy.array(matrix)[:3, 3]
    triangles = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    materialIndices = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("material_index", materialIndices)
  finally:
    evaluated.to_mesh_clear()
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  return positions, triangles.reshape(-1, 3), [slotNames[index] if index < len(slotNames) else None for index in materialIndices]


def collectConstruction(outputPath):
  """Writes the zone's triangles to outputPath (.npz) and returns its texture names and placement count."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  shipped, _ = bridgeExport.exportedObjects()
  parts, placements = [], 0
  for sceneObject, role in shipped:
    if role in ("terrain", "mesh"):
      isTerrain = role == "terrain"
      parts.append((not isTerrain, triangulated(sceneObject, depsgraph, sceneObject.matrix_world)))
      placements += not isTerrain
    elif role == "instance":
      collection = sceneObject.instance_collection
      offset = mathutils.Matrix.Translation(-collection.instance_offset)
      for member in collection.all_objects:
        if member.type == "MESH" and not member.hide_render:
          parts.append((True, triangulated(member, depsgraph, sceneObject.matrix_world @ offset @ member.matrix_world)))
      placements += 1
  textureNames, vertexChunks, triangleChunks, textureChunks, objectChunks, vertexCount = {}, [], [], [], [], 0
  for isObject, (positions, triangles, materialNames) in parts:
    vertexChunks.append(positions)
    triangleChunks.append(triangles + vertexCount)
    textureChunks.append(numpy.array([-1 if name is None else textureNames.setdefault(name, len(textureNames)) for name in materialNames], dtype=numpy.int64))
    objectChunks.append(numpy.full(len(triangles), isObject))
    vertexCount += len(positions)
  os.makedirs(os.path.dirname(outputPath), exist_ok=True)
  numpy.savez(outputPath, vertices=numpy.concatenate(vertexChunks), triangles=numpy.concatenate(triangleChunks), triangleTextures=numpy.concatenate(textureChunks), triangleIsObject=numpy.concatenate(objectChunks))
  return {"arrays": outputPath, "textureNames": list(textureNames), "placements": placements}


def routeSamples(path, sampleSpacing):
  """Points every sampleSpacing units along the route, measured across the ground, with the route's horizontal direction at each."""
  points, directions = [], []
  for start, end in zip(path[:-1], path[1:]):
    run = mathutils.Vector((end[0] - start[0], end[1] - start[1], 0.0))
    if run.length < 1e-6:
      raise ValueError(f"The route stands still between {start} and {end}; each point must lie elsewhere across the ground")
    steps = max(1, math.ceil(run.length / sampleSpacing))
    for step in range(steps):
      share = step / steps
      points.append(mathutils.Vector(start) + share * (mathutils.Vector(end) - mathutils.Vector(start)))
      directions.append(run.normalized())
  points.append(mathutils.Vector(path[-1]))
  directions.append(directions[-1])
  return points, directions


def slopeOf(normal):
  return math.degrees(math.acos(min(1.0, normal.z)))


def standingBelow(surfaces, origin, distance):
  """The footing below origin (PlayerSurfaces.footingOn) with its face's normal and the ceiling over it within reach, or None."""
  footing = surfaces.footingOn(origin, distance)
  if footing is None:
    return None
  overhead = footing.overhead
  return footing.point, footing.normal, overhead[0] if overhead is not None and overhead[0].z - footing.point.z <= routeHeadroomReach else None


def passageBlocker(surfaces, footing, end):
  """The first thing above a step's height in the way from footing to end, along the slope to a higher end; None when clear."""
  start = footing + up * (stepHeight + castNudge)
  span = mathutils.Vector((end.x, end.y, max(footing.z, end.z) + stepHeight + castNudge)) - start
  return surfaces.cast(start, span.normalized(), span.length)


def obstacleHeight(surfaces, footing, blocker, direction):
  """How far above the footing what blocks a step stays steeper than walkable, or None when it goes on past routeHeadroomReach."""
  # Level casts from over the footing climb the blocker's face for as long as each finds it no further back than a walkable slope
  # would lean from the last; past its top, a plane's as much as a block's, they find nothing so near.
  lean = 1 / math.tan(math.radians(steepestWalkableDegrees))

  def faceDistance(height, low, nearest):
    hit = surfaces.cast(mathutils.Vector((footing.x, footing.y, footing.z + height)), direction, nearest + lean * (height - low) + castNudge)
    return None if hit is None else math.hypot(hit.x - footing.x, hit.y - footing.y)

  low, nearest = blocker.z - footing.z, math.hypot(blocker.x - footing.x, blocker.y - footing.y)
  while True:
    high = low + obstacleClimb
    if high > routeHeadroomReach:
      return None
    found = faceDistance(high, low, nearest)
    if found is None:
      break
    low, nearest = high, found
  for _ in range(obstacleRefinements):
    middle = (low + high) / 2
    found = faceDistance(middle, low, nearest)
    if found is None:
      high = middle
    else:
      low, nearest = middle, found
  return (low + high) / 2


def stepAcross(surfaces, footing, target, direction, reach):
  """What stepping from footing to target's [x, y] comes to, searching reach below: step, ledge, steep, rise, or drop (walkRoute)."""
  run = math.hypot(target.x - footing.x, target.y - footing.y)
  climb = max(stepHeight, run * math.tan(math.radians(steepestWalkableDegrees)))
  standing = standingBelow(surfaces, mathutils.Vector((target.x, target.y, footing.z + climb + castNudge)), reach + climb + castNudge)
  blocker = passageBlocker(surfaces, footing, standing[0] if standing is not None else mathutils.Vector((target.x, target.y, footing.z)))
  if blocker is not None:
    return {"kind": "rise", "blocker": blocker}
  if standing is None:
    return {"kind": "drop"}
  landing, normal, ceiling = standing
  slope = slopeOf(normal)
  outcome = {"landing": landing, "slope": slope, "ceiling": ceiling, "run": run}
  if slope > steepestWalkableDegrees:
    return outcome | {"kind": "steep", "climbing": normal.x * direction.x + normal.y * direction.y < 0}
  allowance = stepHeight + run * math.tan(math.radians(slope))
  rise = landing.z - footing.z
  if rise > allowance:
    return outcome | {"kind": "rise"}
  if -rise > allowance:
    return outcome | {"kind": "ledge", "height": -rise}
  return outcome | {"kind": "step"}


def riseHeight(surfaces, footing, outcome, direction):
  """How high a rise stepAcross met stands over the footing: its landing's height, or the height of what blocked the way."""
  if "landing" in outcome:
    return outcome["landing"].z - footing.z
  return obstacleHeight(surfaces, footing, outcome["blocker"], direction)


def sideClearance(surfaces, footing, side):
  """How far to one side footing runs, stepped across, before a drop over a player's height, a rise, or a steep face; None past reach."""
  current = footing
  for offset in numpy.arange(routeSideStep, routeSideReach + routeSideStep / 2, routeSideStep):
    outcome = stepAcross(surfaces, current, footing + side * (float(offset) - castNudge), side, playerHeight)
    if outcome["kind"] not in ("step", "ledge"):
      return float(offset) - routeSideStep
    current = outcome["landing"]
  return None


class RouteWalk:
  """A player's walk along a route stride by stride; a rise or a drop stops it until the route's own heights find footing again."""

  def __init__(self, surfaces, water):
    self.surfaces, self.water = surfaces, water
    self.footing, self.slope, self.headroom = None, None, None
    self.travelled = 0.0
    self.problems, self.oneWay, self.rows = [], [], []
    self.runs = {}
    self.stopped = None
    self.steepest, self.lowest = None, None

  def footingAt(self, spot):
    return standingBelow(self.surfaces, spot + up * (stepHeight + castNudge), routeFootingReach + stepHeight + castNudge)

  def start(self, point, direction):
    standing = self.footingAt(point + direction * castNudge)
    if standing is None:
      raise ValueError(f"No footing within {routeFootingReach:g} below the route's start {roundVector(point)}")
    self.stand(standing[0], slopeOf(standing[1]), standing[2], None)

  def advance(self, point, direction):
    spot = point - direction * castNudge
    if self.footing is None:
      self.takeUp(spot)
      return
    outcome = stepAcross(self.surfaces, self.footing, spot, direction, routeFootingReach)
    if outcome["kind"] in ("rise", "drop"):
      self.stopped = {"kind": outcome["kind"], "at": roundVector(self.footing)}
      if outcome["kind"] == "rise":
        height = riseHeight(self.surfaces, self.footing, outcome, direction)
        self.stopped["height"] = None if height is None else round(height, 1)
      self.stopped["resumesAt"] = None
      self.problems.append(self.stopped)
      self.footing, self.runs = None, {}
      return
    if outcome["kind"] == "ledge":
      self.oneWay.append({"kind": "ledge", "at": roundVector(self.footing), "height": round(outcome["height"], 1)})
    self.travelled += outcome["run"]
    self.stand(outcome["landing"], outcome["slope"], outcome["ceiling"], outcome["climbing"] if outcome["kind"] == "steep" else None)

  def takeUp(self, spot):
    standing = self.footingAt(spot)
    if standing is not None:
      self.stopped["resumesAt"] = roundVector(standing[0])
      self.stand(standing[0], slopeOf(standing[1]), standing[2], None)

  def stand(self, landing, slope, ceiling, climbingSteep):
    self.footing, self.slope = landing, slope
    self.headroom = None if ceiling is None else ceiling.z - landing.z
    if self.steepest is None or slope > self.steepest[0]:
      self.steepest = (slope, landing)
    if self.headroom is not None and (self.lowest is None or self.headroom < self.lowest[0]):
      self.lowest = (self.headroom, landing)
    found = {}
    if climbingSteep is not None:
      found[("problems" if climbingSteep else "oneWay", "steep")] = ("steepestDegrees", slope, max)
    if self.headroom is not None and self.headroom < playerHeight:
      found[("problems", "headroom")] = ("lowest", self.headroom, min)
    self.runs = {key: entry for key, entry in self.runs.items() if key in found}
    for key, (field, value, keep) in found.items():
      if key not in self.runs:
        self.runs[key] = {"kind": key[1], "from": roundVector(landing), "to": None, field: round(value, 1)}
        getattr(self, key[0]).append(self.runs[key])
      entry = self.runs[key]
      entry["to"] = roundVector(landing)
      entry[field] = keep(entry[field], round(value, 1))

  def record(self, direction):
    if self.footing is None:
      return
    side = mathutils.Vector((-direction.y, direction.x, 0.0))
    waterDepth = bridgeMeshAccess.waterDepthAt(self.water, self.footing)
    self.rows.append({
      "distance": round(self.travelled, 1), "at": roundVector(self.footing), "slopeDegrees": round(self.slope, 1),
      "headroom": None if self.headroom is None else round(self.headroom, 1),
      "left": sideClearance(self.surfaces, self.footing, side), "right": sideClearance(self.surfaces, self.footing, -side),
      "waterDepth": None if waterDepth is None else round(waterDepth, 1),
    })


def walkRoute(path, sampleSpacing):
  if len(path) < 2 or any(len(point) != 3 for point in path):
    raise ValueError(f"A route is at least two [x, y, z] points, got {path!r}")
  if sampleSpacing <= 0:
    raise ValueError(f"sampleSpacing must be positive, got {sampleSpacing}")
  walk = RouteWalk(bridgeMeshAccess.PlayerSurfaces(), bridgeMeshAccess.swimSurfaces())
  points, directions = routeSamples(path, sampleSpacing)
  walk.start(points[0], directions[0])
  walk.record(directions[0])
  for start, end, direction in zip(points[:-1], points[1:], directions[:-1]):
    strides = max(1, math.ceil(math.hypot(end.x - start.x, end.y - start.y) / routeStride))
    for stride in range(1, strides + 1):
      walk.advance(start.lerp(end, stride / strides), direction)
    walk.record(direction)
  rows = walk.rows
  widths = [(row["left"] if row["left"] is not None else routeSideReach) + (row["right"] if row["right"] is not None else routeSideReach) for row in rows]
  narrowest = int(numpy.argmin(widths))
  wet = [row for row in rows if row["waterDepth"] is not None]
  return {
    "length": round(walk.travelled, 1), "samples": len(rows), "walkable": not walk.problems, "problems": walk.problems, "oneWay": walk.oneWay,
    "steepest": {"slopeDegrees": round(walk.steepest[0], 1), "at": roundVector(walk.steepest[1])},
    "narrowest": {"width": round(widths[narrowest], 1), "at": rows[narrowest]["at"], "left": rows[narrowest]["left"], "right": rows[narrowest]["right"]},
    "lowestHeadroom": None if walk.lowest is None else {"headroom": round(walk.lowest[0], 1), "at": roundVector(walk.lowest[1])},
    "deepestWater": max(({"depth": row["waterDepth"], "at": row["at"]} for row in wet), key=lambda item: item["depth"]) if wet else None,
    "profile": rows[::max(1, math.ceil(len(rows) / routeProfileRows))],
  }


def roundVector(vector):
  return [round(float(component), 1) for component in vector]


commands = {
  "collectConstruction": (collectConstruction, False),
  "walkRoute": (walkRoute, False),
}
