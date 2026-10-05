"""Arranging copies as an artist sets props: many linked copies in one call, each with its own place, rotation, and scale (what an EQ
placement holds); sets generated along rows, grids, rings, and routes, returned copy by copy so they can be edited; and settling onto the
ground by footprint from above the whole scene, or onto another object. Runs under Blender's Python."""
import math

import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeMeshAccess
import bridgeObjects

copyKeys = {"name", "location", "rotationDegrees", "scale"}
patternKinds = ("row", "grid", "ring", "route")
jitterKeys = {"turnDegrees", "tiltDegrees", "scale", "position"}
facings = ("out", "in", "along")
settleLift = 10.0
# Settling turns a copy at most this many times to find where its tilted footprint rests.
settleRounds = 3


def requireCopy(index, copy, settle):
  if not isinstance(copy, dict) or not copyKeys >= set(copy) or "location" not in copy:
    raise ValueError(f"Copy {index} is {{location, rotationDegrees, scale, name}} with a location, got {copy!r}")
  location = copy["location"]
  if len(location) not in ((2, 3) if settle else (3,)):
    raise ValueError(f"Copy {index}: location is [x, y, z]" + (" or [x, y] when settled" if settle else "") + f", got {location!r}")
  rotation = copy.get("rotationDegrees", [0, 0, 0])
  if len(rotation) != 3:
    raise ValueError(f"Copy {index}: rotationDegrees is [x, y, z], got {rotation!r}")
  scale = copy.get("scale", 1.0)
  if not isinstance(scale, (int, float)) or scale <= 0:
    raise ValueError(f"Copy {index}: scale is one positive number (a placement takes one uniform scale), got {scale!r}")
  return {"name": copy.get("name"), "location": [float(value) for value in location] + ([0.0] if len(location) == 2 else []), "rotationDegrees": [float(value) for value in rotation], "scale": float(scale)}


def describeCopy(sceneObject):
  return {
    "name": sceneObject.name, "location": bridgeObjects.roundVector(sceneObject.location),
    "rotationDegrees": bridgeObjects.roundVector([math.degrees(angle) for angle in sceneObject.rotation_euler], 2), "scale": round(sceneObject.scale[0], 4),
  }


def placeCopies(source, copies, collection, settle, depth, tiltShare):
  """Linked copies of an object (sharing its mesh), each placed, turned, tilted, and scaled as given; settled onto the ground by footprint
  when asked (then each location's z is ignored)."""
  sourceObject = bridgeMeshAccess.requireEditableObject(source, "copy")
  if not copies:
    raise ValueError("placeCopies needs at least one copy")
  specs = [requireCopy(index, copy, settle) for index, copy in enumerate(copies)]
  for spec in specs:
    if spec["name"] is not None:
      bridgeObjects.requireNewName(spec["name"])
  destinations = [bridgeObjects.targetCollection(collection)] if collection is not None else list(sourceObject.users_collection)
  placed = []
  for spec in specs:
    copy = sourceObject.copy()
    if spec["name"] is not None:
      copy.name = spec["name"]
    copy.location = spec["location"]
    copy.rotation_mode = "XYZ"
    copy.rotation_euler = [math.radians(value) for value in spec["rotationDegrees"]]
    copy.scale = [spec["scale"]] * 3
    for destination in destinations:
      destination.objects.link(copy)
    placed.append(copy)
  bpy.context.view_layer.update()
  return {"source": source, "copies": settleObjects([copy.name for copy in placed], depth, tiltShare, None)["settled"] if settle else [describeCopy(copy) for copy in placed]}


def requirePattern(pattern):
  if not isinstance(pattern, dict) or len(pattern) != 1 or next(iter(pattern)) not in patternKinds:
    raise ValueError(f"A pattern is one of {list(patternKinds)}: {{\"row\": {{...}}}} and so on, got {pattern!r}")
  return next(iter(pattern.items()))


def evenlyAlong(points, spacing=None, count=None):
  """Positions evenly along a polyline (by count, ends included, or every `spacing` from its start) with its direction there."""
  points = numpy.asarray(points, dtype=numpy.float64)
  lengths = numpy.linalg.norm(numpy.diff(points, axis=0), axis=1)
  if (lengths < 1e-6).any():
    raise ValueError("A row or route has two points at the same place")
  total = float(lengths.sum())
  if count is not None:
    if count < 1:
      raise ValueError(f"count must be at least 1, got {count}")
    stations = numpy.linspace(0, total, count) if count > 1 else numpy.array([total / 2])
  else:
    if spacing <= 0:
      raise ValueError(f"spacing must be positive, got {spacing}")
    stations = numpy.arange(0, total + 1e-9, spacing)
  starts = numpy.concatenate([[0.0], numpy.cumsum(lengths)])
  placed = []
  for station in stations:
    segment = min(int(numpy.searchsorted(starts, station, side="right")) - 1, len(lengths) - 1)
    share = (station - starts[segment]) / lengths[segment]
    direction = (points[segment + 1] - points[segment]) / lengths[segment]
    placed.append((points[segment] + share * (points[segment + 1] - points[segment]), direction))
  return placed


def faceAngle(direction):
  """The turn (degrees counterclockwise from above) that points a copy's +X along a plan direction: EQ models face +X."""
  return math.degrees(math.atan2(direction[1], direction[0]))


def patternStations(kind, spec):
  """Plan positions and, where the pattern decides it, the turn that faces each copy."""
  if kind == "row":
    if set(spec) - {"from", "to", "count", "spacing", "facing"} or ("count" in spec) == ("spacing" in spec):
      raise ValueError(f"A row is {{from, to, count or spacing, facing}}, got {spec!r}")
    stations = evenlyAlong([spec["from"], spec["to"]], spec.get("spacing"), spec.get("count"))
    return [(point, faceAngle(direction) if spec.get("facing") == "along" else None) for point, direction in stations]
  if kind == "route":
    if set(spec) - {"path", "spacing", "offset", "facing"} or "spacing" not in spec:
      raise ValueError(f"A route is {{path, spacing, offset, facing}}, got {spec!r}")
    offset = spec.get("offset", 0.0)
    stations = evenlyAlong(spec["path"], spec["spacing"])
    return [(point + offset * numpy.array([direction[1], -direction[0]]), faceAngle(direction) if spec.get("facing") == "along" else None) for point, direction in stations]
  if kind == "ring":
    if set(spec) - {"center", "radius", "count", "startDegrees", "facing"} or spec.get("radius", 0) <= 0 or spec.get("count", 0) < 1:
      raise ValueError(f"A ring is {{center, radius, count, startDegrees, facing}} with a positive radius and count, got {spec!r}")
    center = numpy.asarray(spec["center"], dtype=numpy.float64)
    stations = []
    for index in range(spec["count"]):
      angle = math.radians(spec.get("startDegrees", 0.0)) + 2 * math.pi * index / spec["count"]
      outward = numpy.array([math.cos(angle), math.sin(angle)])
      facing = {"out": outward, "in": -outward, "along": numpy.array([-outward[1], outward[0]])}.get(spec.get("facing"))
      stations.append((center + spec["radius"] * outward, faceAngle(facing) if facing is not None else None))
    return stations
  if set(spec) - {"center", "size", "spacing", "turnDegrees"} or len(spec.get("size", [])) != 2 or len(spec.get("spacing", [])) != 2 or min(spec["spacing"]) <= 0:
    raise ValueError(f"A grid is {{center, size [across, along], spacing [across, along], turnDegrees}}, got {spec!r}")
  turn = math.radians(spec.get("turnDegrees", 0.0))
  across, along = numpy.array([math.cos(turn), math.sin(turn)]), numpy.array([-math.sin(turn), math.cos(turn)])
  center = numpy.asarray(spec["center"], dtype=numpy.float64)
  columns = int(spec["size"][0] // spec["spacing"][0]) + 1
  rows = int(spec["size"][1] // spec["spacing"][1]) + 1
  return [
    (center + across * (column - (columns - 1) / 2) * spec["spacing"][0] + along * (row - (rows - 1) / 2) * spec["spacing"][1], None)
    for row in range(rows) for column in range(columns)
  ]


def generateCopies(source, pattern, jitter, seed, collection, settle, depth, tiltShare):
  """A set laid out by a pattern with seeded variation, placed as placeCopies places a list, and returned as that list."""
  kind, spec = requirePattern(pattern)
  jitter = jitter or {}
  if set(jitter) - jitterKeys:
    raise ValueError(f"jitter takes {sorted(jitterKeys)}, got {sorted(jitter)}")
  for key in ("turnDegrees", "tiltDegrees", "scale"):
    if key in jitter and (len(jitter[key]) != 2 or jitter[key][0] > jitter[key][1]):
      raise ValueError(f"jitter {key} is [low, high], got {jitter[key]!r}")
  if "scale" in jitter and jitter["scale"][0] <= 0:
    raise ValueError(f"jitter scale must stay positive, got {jitter['scale']!r}")
  if "tiltDegrees" in jitter and settle and tiltShare > 0:
    raise ValueError("jitter tiltDegrees and tiltShare both set each copy's tilt (settling with tiltShare replaces it); use one")
  generator = numpy.random.default_rng(seed)
  copies = []
  for point, facing in patternStations(kind, spec):
    nudge = generator.uniform(-1, 1, 2) * jitter.get("position", 0.0)
    turn = (facing or 0.0) + (generator.uniform(*jitter["turnDegrees"]) if "turnDegrees" in jitter else 0.0)
    tilt = [generator.uniform(*jitter["tiltDegrees"]) * generator.choice([-1, 1]) for _ in range(2)] if "tiltDegrees" in jitter else [0.0, 0.0]
    scale = generator.uniform(*jitter["scale"]) if "scale" in jitter else 1.0
    copies.append({"location": [float(point[0] + nudge[0]), float(point[1] + nudge[1])] + ([] if settle else [0.0]), "rotationDegrees": [tilt[0], tilt[1], turn], "scale": scale})
  return placeCopies(source, copies, collection, settle, depth, tiltShare) | {"pattern": kind}


def surfacesExcept(names, onto):
  """A BVH over what objects settle on: one named object, or what players stand on apart from the objects being settled."""
  parts = bridgeMeshAccess.objectParts(bridgeMeshAccess.requireObject(onto)) if onto is not None else bridgeMeshAccess.playerSolidParts(names)
  positions, triangles = bridgeMeshAccess.partTriangles(parts)
  if len(triangles) == 0:
    raise ValueError("Nothing to settle onto")
  return mathutils.bvhtree.BVHTree.FromPolygons(positions.tolist(), triangles.tolist())


def footprint(sceneObject):
  """The object's evaluated bounding box in world space: its bottom and top heights and plan samples (corners, edge middles, and
  middle)."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  corners = numpy.array([list(corner) for corner in bridgeMeshAccess.worldBoundsCorners(sceneObject, depsgraph)])
  low, high = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)
  samples = [[x, y] for x in (low[0], (low[0] + high[0]) / 2, high[0]) for y in (low[1], (low[1] + high[1]) / 2, high[1])]
  return float(corners[:, 2].min()), float(corners[:, 2].max()), samples


def heading(sceneObject):
  """The turn about the vertical left once the object's lean is taken off (a swing-twist split), so settling again keeps it exactly."""
  rotation = sceneObject.rotation_euler.to_quaternion()
  swing = mathutils.Vector((0.0, 0.0, 1.0)).rotation_difference(rotation @ mathutils.Vector((0.0, 0.0, 1.0)))
  return (swing.inverted() @ rotation).to_euler("XYZ").z


def restingLift(sceneObject, surfaces, castHeight):
  """How far to raise the object so none of its vertices lies below the surface under it: where it rests on that surface."""
  positions, _ = bridgeMeshAccess.partTriangles(bridgeMeshAccess.objectParts(sceneObject))
  down = mathutils.Vector((0.0, 0.0, -1.0))
  gaps = []
  for x, y, z in positions:
    location = surfaces.ray_cast(mathutils.Vector((x, y, castHeight)), down, castHeight + bridgeMeshAccess.waterReach)[0]
    if location is not None:
      gaps.append(location.z - z)
  return max(gaps) if gaps else None


def dropHeight(sceneObject, surfaces, castHeight):
  """Where an object drops from: from its own top where rock lies over it (in a cave, under an overhang), so it lands on the ground
  under that rock; else from above the whole scene."""
  _, top, samples = footprint(sceneObject)
  middle = numpy.mean(samples, axis=0)
  location, normal, _, _ = surfaces.ray_cast(mathutils.Vector((middle[0], middle[1], top)), mathutils.Vector((0.0, 0.0, 1.0)), castHeight)
  return top if location is not None and normal.z < 0 else castHeight


def settleObjects(names, depth, tiltShare, onto):
  """Drop each object by its footprint, from above the whole scene, or from where it is when rock lies over it: onto the ground, sunk
  to the lowest ground under its footprint so no edge floats; or onto a named object, resting on it with no vertex below its surface;
  then `depth` lower. tiltShare (0 to 1) turns it that share of the way toward the slope under it, keeping its heading."""
  if not 0 <= tiltShare <= 1:
    raise ValueError(f"tiltShare is 0 to 1, got {tiltShare}")
  objects = [bridgeMeshAccess.requireEditableObject(name, "settle") for name in names]
  surfaces = surfacesExcept(set(names), onto)
  castHeight = bridgeMeshAccess.sceneTopHeight() + settleLift
  down = mathutils.Vector((0.0, 0.0, -1.0))
  settled, missed = [], []
  for sceneObject in objects:
    bpy.context.view_layer.update()
    dropFrom = dropHeight(sceneObject, surfaces, castHeight)
    for _ in range(settleRounds if tiltShare > 0 else 1):
      bpy.context.view_layer.update()
      _, _, samples = footprint(sceneObject)
      hits = [surfaces.ray_cast(mathutils.Vector((x, y, dropFrom)), down, dropFrom + bridgeMeshAccess.waterReach)[0] for x, y in samples]
      hits = [location for location in hits if location is not None]
      if not hits:
        break
      heights = [location.z for location in hits]
      if tiltShare > 0 and len(hits) >= 3:
        points = numpy.array([list(location) for location in hits])
        fit = numpy.linalg.lstsq(numpy.column_stack([points[:, :2], numpy.ones(len(points))]), points[:, 2], rcond=None)[0]
        normal = mathutils.Vector((-fit[0], -fit[1], 1.0)).normalized()
        leaning = mathutils.Vector((0.0, 0.0, 1.0)).lerp(normal, tiltShare).normalized()
        turn = heading(sceneObject)
        sceneObject.rotation_mode = "XYZ"
        sceneObject.rotation_euler = (mathutils.Vector((0, 0, 1)).rotation_difference(leaning) @ mathutils.Quaternion((0, 0, 1), turn)).to_euler("XYZ")
        bpy.context.view_layer.update()
      if onto is None:
        lift = min(heights) - footprint(sceneObject)[0]
      else:
        lift = restingLift(sceneObject, surfaces, dropFrom)
        if lift is None:
          hits = []
          break
      sceneObject.location.z += lift - depth
    if not hits:
      missed.append(sceneObject.name)
      continue
    bpy.context.view_layer.update()
    bottom, top, _ = footprint(sceneObject)
    settled.append(describeCopy(sceneObject) | {"under": [round(min(heights), 2), round(max(heights), 2)], "spans": [round(bottom, 2), round(top, 2)]})
  if missed:
    raise ValueError(f"Nothing under {missed} to settle onto" + (f" ('{onto}' lies elsewhere)" if onto is not None else ""))
  return {"settled": settled}


commands = {
  "placeCopies": (placeCopies, True),
  "generateCopies": (generateCopies, True),
  "settleObjects": (settleObjects, True),
}
