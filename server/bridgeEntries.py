"""Where players arrive in the zone. The author places entries: a zoneIn, where a neighbour's zone line lands players (fromZone, and
fromNumber, that line's zone_points number, when known), and a landing, where a port inside the world lands them; each an arrow empty
in the entries collection at its footing, its local +Y turned to its heading. The zone holds others already, read where they are
needed and never stored: the safe point, the landing of each zone line leading back into this zone (a teleport), and each plot's
entrance. Footing is read on what the zone ships and the client collides with (bridgeExport.collisionTriangles), so a reference zone or
a placed client object under a point is no ground. Runs under Blender's Python."""
import json
import math

import bpy
import mathutils

import bridgeBoundaries
import bridgeCommands
import bridgeExport
import bridgeHousing
import bridgeMeshAccess
import bridgeObjects
import bridgeSwim
import bridgeViews
import eqgFiles
from playerScale import playerHeight, steepestWalkableDegrees, walkableNormalZ

entryCollectionName = "entries"
entryKinds = ("zoneIn", "landing")
# A stored entry within this of the footing found at its point stands on it.
footingTolerance = 0.01
# A point given as [x, y] looks down for its highest footing from this far above everything the zone ships.
sceneClearance = 100.0
up = bridgeMeshAccess.up


class ShippedGround:
  """Footing on what the zone ships and the client collides with (bridgeExport.collisionTriangles)."""

  def __init__(self):
    collision = bridgeExport.collisionTriangles()
    self.surfaces = bridgeExport.collisionSurfaces(collision)
    heights = collision["positions"][collision["triangles"].ravel(), 2]
    self.bottom, self.top = (float(heights.min()), float(heights.max())) if len(heights) else (None, None)

  def footing(self, at):
    """The footing (bridgeMeshAccess.Footing) at [x, y], the highest there, or at [x, y, z], from bridgeViews.groundSearchAbove over z down
    to groundSearchDistance below it, as standAt finds it; None where there is none."""
    if self.surfaces is None:
      return None
    if len(at) == 2:
      return self.surfaces.footingOn(mathutils.Vector((at[0], at[1], self.top + sceneClearance)), self.top - self.bottom + 2 * sceneClearance)
    return self.surfaces.footingOn(mathutils.Vector(at) + up * bridgeViews.groundSearchAbove, bridgeViews.groundSearchAbove + bridgeViews.groundSearchDistance)

  def state(self, point):
    """Whether a point stands on its footing (found as for [x, y, z]): onFooting, or offFooting with the footing and how far off."""
    footing = self.footing(list(point))
    distance = None if footing is None else (footing.point - mathutils.Vector(point)).length
    if distance is not None and distance <= footingTolerance:
      return {"state": "onFooting"}
    return {"state": "offFooting", "footing": None if footing is None else roundPoint(footing.point), "distance": None if distance is None else round(distance, 3)}


def roundPoint(point):
  return [round(float(value), 3) + 0.0 for value in point]


def entryObjects():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.entryProperty in sceneObject), key=lambda entry: entry.name)


def readEntry(entry):
  return json.loads(entry[bridgeMeshAccess.entryProperty])


def entryHeading(entry):
  """The way an entry faces (its local +Y) as a heading: 0 = +Y, clockwise."""
  forward = entry.matrix_world.to_3x3() @ mathutils.Vector((0.0, 1.0, 0.0))
  return math.degrees(math.atan2(forward.x, forward.y)) % 360


def standsInto(box, footing):
  """Whether the space a player stands in over a footing, playerHeight up from it, meets a box empty (a swim volume or zone line: a
  unit cube scaled to its half extents), its faces included. The client tests a player's origin, which stands somewhere in that space
  by race and size."""
  inverse = box.matrix_world.inverted()
  start, end = inverse @ footing, inverse @ (footing + up * playerHeight)
  low, high = 0.0, 1.0
  for axis in range(3):
    step = end[axis] - start[axis]
    if step == 0:
      if abs(start[axis]) > 1.0:
        return False
      continue
    near, far = sorted(((-1.0 - start[axis]) / step, (1.0 - start[axis]) / step))
    low, high = max(low, near), min(high, far)
    if low > high:
      return False
  return True


def undecidedSurfaceOver(footing):
  """The pool or river of undecided swimming whose surface stands over a footing with nothing the zone ships between (players would
  arrive in its water), or None. A surface with ground or a basin between, as over a floating pool's underside, is not over it."""
  ceiling = footing.overhead[0].z if footing.overhead is not None else math.inf
  for body in bridgeSwim.swimDecisions()["undecided"]:
    location, _, _, _ = bridgeMeshAccess.worldTree([body]).ray_cast(footing.point + up * bridgeMeshAccess.castNudge, up, bridgeMeshAccess.waterReach)
    if location is not None and location.z <= ceiling:
      return body.name
  return None


def footingProblem(footing, at):
  """Why players cannot arrive on a footing found for `at`, or None."""
  if footing is None:
    return f"No ground the zone ships lies at {at}: reference zones, placed client objects, guides, and what players pass through are not ground"
  point = roundPoint(footing.point)
  slope = math.degrees(math.acos(max(-1.0, min(1.0, footing.normal.z))))
  if footing.normal.z < walkableNormalZ:
    return f"The footing at {point} slopes {slope:.1f} degrees, steeper than players walk ({steepestWalkableDegrees:.1f}, playerScale)"
  if footing.overhead is not None and footing.overhead[0].z - footing.point.z < playerHeight:
    return f"The footing at {point} has {footing.overhead[0].z - footing.point.z:.2f} of headroom, under a player's height ({playerHeight:g}, playerScale)"
  for line in bridgeBoundaries.zoneLineObjects():
    if bridgeBoundaries.isAuthored(line) and standsInto(line, footing.point):
      return (
        f"A player standing on the footing at {point} ({playerHeight:g} tall, playerScale) reaches into zone line '{line.name}': players"
        " arriving there would zone out at once"
      )
  for box in bridgeSwim.swimBoxes():
    if standsInto(box, footing.point):
      return (
        f"A player standing on the footing at {point} ({playerHeight:g} tall, playerScale) reaches into swim volume '{box.name}': players"
        " would arrive in its water, wading or swimming by its depth"
      )
  body = undecidedSurfaceOver(footing)
  if body is not None:
    return f"The footing at {point} lies under the surface of '{body}', whose swimming is undecided: whether players arrive wading or swimming is not designed yet (buildSwimVolumes, placeSwimVolume, or editWater swimmable false)"
  return None


def requireEntryValues(name, at, headingDegrees, kind, fromZone, fromNumber, isolated):
  if not isinstance(name, str) or not name.strip():
    raise ValueError(f"An entry needs a name, got {name!r}")
  taken = bpy.data.objects.get(name)
  if taken is not None and bridgeMeshAccess.entryProperty not in taken:
    raise ValueError(f"'{name}' is the name of '{taken.name}', which is not an entry; name the entry otherwise")
  if kind not in entryKinds:
    raise ValueError(f"An entry's kind is one of {list(entryKinds)}, got {kind!r}")
  if kind == "zoneIn":
    if fromZone is None:
      raise ValueError("A zoneIn needs fromZone: the short name of the neighbour whose zone line lands players here")
    if not isinstance(fromZone, str) or not eqgFiles.zoneNamePattern.match(fromZone):
      raise ValueError(f"fromZone is a zone's short name, {eqgFiles.zoneNameRule}, got {fromZone!r}")
  elif fromZone is not None or fromNumber is not None:
    raise ValueError("fromZone and fromNumber are a zoneIn's: a landing is reached by a port inside the world")
  if fromNumber is not None and (isinstance(fromNumber, bool) or not isinstance(fromNumber, int) or fromNumber < 1):
    raise ValueError(f"fromNumber is the neighbour's zone_points number, a whole number of at least 1, got {fromNumber!r}")
  if not bridgeCommands.isFiniteNumber(headingDegrees) or not 0 <= headingDegrees < 360:
    raise ValueError(f"headingDegrees runs from 0 up to 360 (0 = +Y, clockwise), got {headingDegrees!r}")
  if not isinstance(isolated, bool):
    raise ValueError(f"isolated is true or false, got {isolated!r}")
  if not isinstance(at, list) or len(at) not in (2, 3) or not all(bridgeCommands.isFiniteNumber(value) for value in at):
    raise ValueError(
      f"at is [x, y] (the highest footing there) or [x, y, z] (the footing from {bridgeViews.groundSearchAbove:g} above z down to"
      f" {bridgeViews.groundSearchDistance:g} below it), got {at!r}"
    )


def placeEntry(name, at, headingDegrees, kind, fromZone, fromNumber, isolated):
  """Place an entry at its footing, turned to its heading; placing an existing entry's name replaces it."""
  requireEntryValues(name, at, headingDegrees, kind, fromZone, fromNumber, isolated)
  ground = ShippedGround()
  footing = ground.footing(at)
  problem = footingProblem(footing, at)
  if problem is not None:
    raise ValueError(problem)
  replaced = bpy.data.objects.get(name)
  if replaced is not None:
    bpy.data.objects.remove(replaced)
  entry = bpy.data.objects.new(name, None)
  entry.empty_display_type = "ARROWS"
  entry.empty_display_size = playerHeight
  entry.location = footing.point
  entry.rotation_euler = (0.0, 0.0, math.radians(-headingDegrees))
  entry[bridgeMeshAccess.entryProperty] = json.dumps({"kind": kind, "fromZone": fromZone, "fromNumber": fromNumber, "isolated": isolated})
  bridgeObjects.targetCollection(entryCollectionName).objects.link(entry)
  bpy.context.view_layer.update()
  return describeEntry(entry, ground) | {"replaced": replaced is not None}


def describeEntry(entry, ground):
  spec = readEntry(entry)
  return {
    "name": entry.name, "kind": spec["kind"], "at": roundPoint(entry.matrix_world.translation), "headingDegrees": round(entryHeading(entry), 2),
    "source": "placeEntry", "fromZone": spec["fromZone"], "fromNumber": spec["fromNumber"], "isolated": spec["isolated"],
  } | ground.state(entry.matrix_world.translation)


def teleportLandings(shortName):
  """The landings of the zone's own zone lines leading back into it (target zone shortName) at a numeric point, and the lines whose
  landing cannot be placed, each with why."""
  landings, notFollowed = [], []
  for line in bridgeBoundaries.zoneLineObjects():
    target = bridgeBoundaries.readSpec(line, bridgeMeshAccess.zoneLineProperty) if bridgeBoundaries.isAuthored(line) else None
    if target is None:
      continue
    if shortName is None:
      notFollowed.append({"zoneLine": line.name, "why": "shortName is not set (setZoneProperties), so whether it leads back into this zone is unknown"})
    elif target["zone"] == shortName:
      kept = [axis for axis in bridgeBoundaries.targetCoordinates if target[axis] == bridgeBoundaries.keep]
      if kept:
        notFollowed.append({"zoneLine": line.name, "why": f"its target keeps the player's own {', '.join(kept)}, so where it lands is not one point"})
      else:
        landings.append((line, target))
  return landings, notFollowed


def getEntries():
  """Every stored and derived entry with its point, heading, source, and state (onFooting, or offFooting with the footing found and
  how far off), and the zone lines leading back into the zone whose landing cannot be placed."""
  ground = ShippedGround()
  zone = bridgeCommands.readZoneProperties(bpy.context.scene)
  entries = [describeEntry(entry, ground) for entry in entryObjects()]
  if "safePoint" in zone:
    point = zone["safePoint"][:3]
    entries.append({"name": "safe point", "kind": "safePoint", "at": roundPoint(point), "headingDegrees": zone["safePoint"][3], "source": "setZoneProperties safePoint"} | ground.state(point))
  landings, notFollowed = teleportLandings(zone.get("shortName"))
  for line, target in landings:
    point = [target[axis] for axis in bridgeBoundaries.targetCoordinates]
    number = bridgeBoundaries.parsedName(line.name)[0]
    entries.append({"name": f"T{number}", "kind": "teleport", "at": roundPoint(point), "headingDegrees": target["headingDegrees"], "source": f"zone line {line.name}"} | ground.state(point))
  for entrance in bridgeHousing.plotEntrances():
    entries.append({"name": entrance["plot"], "kind": "plotEntrance", "at": entrance["at"], "headingDegrees": entrance["headingDegrees"], "source": f"plot {entrance['plot']}"} | ground.state(entrance["at"]))
  return {"entries": entries, "notFollowed": notFollowed}


commands = {
  "placeEntry": (placeEntry, True),
  "getEntries": (getEntries, False),
}
