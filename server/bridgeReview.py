"""The open scene's zone gathered as the zone survey gathers a client zone, so it is measured by the same methods: what export ships
(bridgeExport.exportedObjects), the terrain collection's meshes as its terrain and every other mesh and collection instance placed on
it. Runs under Blender's Python."""
import bisect
import math
import os

import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeBoundaries
import bridgeExport
import bridgeMeshAccess
import bridgeReviewGuides
import bridgeViews
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
# A route strip's frame looks toward the route this far on from where it stands, so the way ahead shows.
stripLookAhead = 30.0
# Stations and route points closer than this along a route are one place.
samePlace = 1e-6


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


def boundaryAcross(boundaries, footing, spot):
  """Where a boundary (bridgeBoundaries) stands across the way from footing to spot's [x, y] at half a player's height, or None."""
  if boundaries is None:
    return None
  run = mathutils.Vector((spot.x - footing.x, spot.y - footing.y, 0.0))
  return boundaries.cast(footing + up * (playerHeight / 2), run.normalized(), run.length)


class RouteWalk:
  """A player's walk along a route stride by stride; a rise or a drop stops it until the route's own heights find footing again."""

  def __init__(self, surfaces, water, boundaries):
    self.surfaces, self.water, self.boundaries = surfaces, water, boundaries
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
    across = boundaryAcross(self.boundaries, self.footing, spot)
    if across is not None:
      self.stop({"kind": "blocked", "at": roundVector(self.footing), "boundary": roundVector(across)})
      return
    outcome = stepAcross(self.surfaces, self.footing, spot, direction, routeFootingReach)
    if outcome["kind"] == "rise":
      height = riseHeight(self.surfaces, self.footing, outcome, direction)
      self.stop({"kind": "rise", "at": roundVector(self.footing), "height": None if height is None else round(height, 1)})
      return
    if outcome["kind"] == "drop":
      self.stop({"kind": "drop", "at": roundVector(self.footing)})
      return
    if outcome["kind"] == "ledge":
      self.oneWay.append({"kind": "ledge", "at": roundVector(self.footing), "height": round(outcome["height"], 1)})
    self.travelled += outcome["run"]
    self.stand(outcome["landing"], outcome["slope"], outcome["ceiling"], outcome["climbing"] if outcome["kind"] == "steep" else None)

  def stop(self, problem):
    self.stopped = problem | {"resumesAt": None}
    self.problems.append(self.stopped)
    self.footing, self.runs = None, {}

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
  walk = RouteWalk(bridgeBoundaries.collisionSurfaces(), bridgeMeshAccess.swimSurfaces(), bridgeBoundaries.boundarySurfaces())
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


def givenPath(path, route):
  if (path is None) == (route is None):
    raise ValueError("Give path, the route's [x, y, z] points, or route, the name of a saved review route")
  return bridgeReviewGuides.routePath(route) if route is not None else path


def walkGivenRoute(path, route, sampleSpacing):
  return walkRoute(givenPath(path, route), sampleSpacing)


class RouteLine:
  """A route's points by plan distance along it; past its end it runs on level with its last point, along its last segment."""

  def __init__(self, path):
    bridgeReviewGuides.requireRoutePath(path)
    self.points = [mathutils.Vector(point) for point in path]
    self.starts = numpy.concatenate([[0.0], numpy.cumsum([math.hypot(end.x - start.x, end.y - start.y) for start, end in zip(self.points[:-1], self.points[1:])])])
    self.length = float(self.starts[-1])

  def segmentAt(self, distance):
    """The segment a distance lies on: at a route point the one starting there, past the end the last."""
    return int(min(numpy.searchsorted(self.starts, distance, side="right") - 1, len(self.points) - 2))

  def direction(self, segment):
    return mathutils.Vector((self.points[segment + 1].x - self.points[segment].x, self.points[segment + 1].y - self.points[segment].y, 0.0)).normalized()

  def pointAt(self, distance):
    segment = self.segmentAt(distance)
    if distance > self.length:
      last = self.points[-1]
      return last + self.direction(segment) * (distance - self.length)
    share = (distance - self.starts[segment]) / (self.starts[segment + 1] - self.starts[segment])
    return self.points[segment].lerp(self.points[segment + 1], share)


def lookedAt(strides, distance, pastStops):
  """Where the walk stands stripLookAhead on from a distance along the route (strides: its footing, None while stopped, by distance);
  short of pastStops, its last footing before a stop that comes sooner, None when it stops at once."""
  target = None
  for strideDistance, footing in strides[bisect.bisect_right([entry[0] for entry in strides], distance + samePlace):]:
    if strideDistance > distance + stripLookAhead + samePlace:
      break
    if footing is not None:
      target = footing
    elif not pastStops:
      break
  return target


def stripFrame(line, distance, footing, target, problem):
  """An eye-level frame standing on footing at a distance along the route, heading along it, its pitch toward target (level for none)."""
  heading = line.direction(line.segmentAt(distance))
  pitch = 0.0 if target is None else math.degrees(math.atan2(target.z - (footing.z + bridgeViews.eyeHeight), math.hypot(target.x - footing.x, target.y - footing.y)))
  return {
    "distance": round(distance, 1), "standAt": [round(float(value), 3) for value in footing],
    "headingDegrees": round(math.degrees(math.atan2(heading.x, heading.y)) % 360, 2), "pitchDegrees": round(pitch, 2), "problem": problem,
  }


def planRouteStrip(path, route, spacing):
  """Walk a route as walkRoute does and plan an eye-level frame every `spacing` along it in plan, where the walk stands, and one where
  each of the walk's problems starts; stations the walk cannot reach (between a stop and where it takes up again) get none. A frame along
  the way looks at where the walk stands stripLookAhead on, or at its brink when it stops sooner; a problem's frame looks past it."""
  path = givenPath(path, route)
  if spacing <= 0:
    raise ValueError(f"spacing must be positive, got {spacing}")
  line = RouteLine(path)
  stations = [step * spacing for step in range(math.floor(line.length / spacing + samePlace) + 1)]
  marks = []
  for distance, isStation in sorted([(distance, True) for distance in stations] + [(float(start), False) for start in line.starts]):
    if marks and distance - marks[-1][0] < samePlace:
      marks[-1] = (marks[-1][0], marks[-1][1] or isStation)
    else:
      marks.append((distance, isStation))
  walk = RouteWalk(bridgeBoundaries.collisionSurfaces(), bridgeMeshAccess.swimSurfaces(), bridgeBoundaries.boundarySurfaces())
  walk.start(line.pointAt(0.0), line.direction(0))
  strides = [(marks[0][0], walk.footing.copy())]
  footings = {marks[0][0]: walk.footing.copy()}
  # Where each problem starts, along the route and exactly underfoot (its reported place is rounded): a stop where the walk stood
  # before the stride that stopped it, a stretch where the stride that began it landed.
  problemPlaces, resumeDistances = [], {}
  for (startDistance, _), (endDistance, isStation) in zip(marks[:-1], marks[1:]):
    direction = line.direction(line.segmentAt(startDistance))
    start, end = line.pointAt(startDistance), line.pointAt(endDistance)
    count = max(1, math.ceil(math.hypot(end.x - start.x, end.y - start.y) / routeStride))
    for stride in range(1, count + 1):
      share = stride / count
      awaiting = walk.stopped if walk.stopped is not None and walk.stopped["resumesAt"] is None else None
      before, standing = len(walk.problems), walk.footing
      walk.advance(start.lerp(end, share), direction)
      distance = startDistance + (endDistance - startDistance) * share
      strides.append((distance, None if walk.footing is None else walk.footing.copy()))
      problemPlaces += [(distance, standing if "at" in problem else walk.footing) for problem in walk.problems[before:]]
      if awaiting is not None and awaiting["resumesAt"] is not None:
        resumeDistances[id(awaiting)] = distance
    if isStation:
      footings[endDistance] = strides[-1][1]
  frames, notStoodOn = [], []
  for distance, isStation in marks:
    if not isStation:
      continue
    if footings[distance] is None:
      notStoodOn.append(round(distance, 1))
    else:
      frames.append(stripFrame(line, distance, footings[distance], lookedAt(strides, distance, False), None))
  for problem, (distance, footing) in zip(walk.problems, problemPlaces):
    shown = problem | ({"resumesAtDistance": round(resumeDistances[id(problem)], 1)} if id(problem) in resumeDistances else {})
    frames.append(stripFrame(line, distance, footing, lookedAt(strides, distance, True), shown))
  frames.sort(key=lambda frame: (frame["distance"], frame["problem"] is not None))
  return {
    "route": route, "length": round(line.length, 1), "spacing": spacing, "walkable": not walk.problems, "problems": walk.problems,
    "oneWay": walk.oneWay, "frames": frames, "stationsNotStoodOn": notStoodOn,
  }


commands = {
  "collectConstruction": (collectConstruction, False),
  "walkRoute": (walkGivenRoute, False),
  "planRouteStrip": (planRouteStrip, False),
}
