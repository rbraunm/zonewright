"""Buildings from kit pieces, as the client splits its buildings into models placed together: placed pieces of a kit file gathered into
a prefab's parts (assemblePrefab), and a prefab placed in a zone as a structure, one instance per part, seated on the ground under its
footprint, on a plinth where the ground falls away (placePrefab, kind prefab). Registered with bridgeStructures. Runs under Blender's
Python."""
import math
import re

import bpy
import numpy

import bridgeKitData
import bridgeKitGeometry
import bridgeKits
import bridgeMeshAccess
import bridgeReview
import bridgeStructures
import bridgeSurfacing
from bridgeStructures import isNumber, requireKeys, requireNonNegative, requirePoint, requirePositive, roundVector
from playerScale import stepHeight

partNamePattern = re.compile(r"[a-z][A-Za-z0-9]*")
entranceKeys = {"name", "at", "facingDegrees"}
prefabKeys = ("kitPath", "prefab", "location", "facingDegrees", "plinth", "collection")
plinthDefaults = {"sink": 2.0, "margin": 0.0}
interiorPart = "interior"
# The ground under a footprint is looked up this far apart across it and along its sides.
sampleSpacing = 2.0
# An entrance's ground is looked for this far below a step outside it; its walk runs from this far outside it to this far inside; its
# view stands this far out.
entranceFootingReach = 60.0
entranceWalkDistance = 10.0
entranceViewDistance = 25.0
walkSampleSpacing = 1.0
# Footprint corners closer than this to the line between their neighbours are on it: a placement's float32 turn moves a piece's
# corners by about a millionth.
hullTolerance = 1e-3


def convexHull(points):
  """The convex hull of [x, y] points, counterclockwise from the lowest-leftmost, without points on the line between their neighbours."""
  ordered = sorted({(round(float(x), 4), round(float(y), 4)) for x, y in points})
  if len(ordered) < 3:
    raise ValueError(f"A footprint needs at least three points not in a line; the parts give {ordered}")

  def turnsLeft(origin, middle, following):
    cross = (middle[0] - origin[0]) * (following[1] - origin[1]) - (middle[1] - origin[1]) * (following[0] - origin[0])
    return cross > hullTolerance * math.hypot(following[0] - origin[0], following[1] - origin[1])

  lower, upper = [], []
  for point in ordered:
    while len(lower) >= 2 and not turnsLeft(lower[-2], lower[-1], point):
      lower.pop()
    lower.append(point)
  for point in reversed(ordered):
    while len(upper) >= 2 and not turnsLeft(upper[-2], upper[-1], point):
      upper.pop()
    upper.append(point)
  hull = lower[:-1] + upper[:-1]
  if len(hull) < 3:
    raise ValueError("The parts' footprint has no area: their pieces stand in a line in plan")
  return numpy.array(hull)


def sideNormals(hull):
  sides = numpy.roll(hull, -1, axis=0) - hull
  return sides, numpy.stack([sides[:, 1], -sides[:, 0]], axis=1) / numpy.linalg.norm(sides, axis=1)[:, None]


def distanceOutside(hull, point):
  """How far a point lies outside a counterclockwise convex outline in plan, 0 inside it."""
  sides, normals = sideNormals(hull)
  if (((point - hull) * normals).sum(axis=1)).max() <= 0:
    return 0.0
  shares = numpy.clip(((point - hull) * sides).sum(axis=1) / (sides * sides).sum(axis=1), 0, 1)
  return float(numpy.linalg.norm(point - (hull + shares[:, None] * sides), axis=1).min())


def exitDistance(hull, point, direction):
  """How far from a point inside a convex outline a ray in a plan direction leaves it, 0 for a point outside."""
  if distanceOutside(hull, point) > 0:
    return 0.0
  _, normals = sideNormals(hull)
  facing = normals @ direction
  return float(min(((start - point) @ normal) / along for start, normal, along in zip(hull, normals, facing) if along > 1e-12))


def footprintSamples(hull):
  """Points across a footprint where its ground is looked up: its corners, along its sides, and a grid inside it."""
  samples = list(hull)
  for start, end in zip(hull, numpy.roll(hull, -1, axis=0)):
    count = max(1, math.ceil(numpy.linalg.norm(end - start) / sampleSpacing))
    samples.extend(start + (end - start) * step / count for step in range(1, count))
  low, high = hull.min(0), hull.max(0)
  grid = numpy.array([[x, y] for x in numpy.arange(low[0] + sampleSpacing / 2, high[0], sampleSpacing) for y in numpy.arange(low[1] + sampleSpacing / 2, high[1], sampleSpacing)]).reshape(-1, 2)
  if len(grid):
    samples.extend(grid[bridgeMeshAccess.insidePolygon(grid, hull)])
  return numpy.array(samples)


def capitalized(part):
  return part[0].upper() + part[1:]


def requireParts(name, parts):
  if not isinstance(parts, dict) or not parts:
    raise ValueError(f"parts maps each part's name (exterior, interior, roof, or any camelCase name) to the placed pieces it gathers, got {parts!r}")
  seen = {}
  for part, names in parts.items():
    if not isinstance(part, str) or not partNamePattern.fullmatch(part):
      raise ValueError(f"A part's name is camelCase (exterior, interior, roof, porch), got {part!r}")
    if not isinstance(names, list) or not names or not all(isinstance(entry, str) for entry in names):
      raise ValueError(f"Part '{part}' lists the placed pieces it gathers, at least one; got {names!r}")
    for entry in names:
      if entry in seen:
        raise ValueError(f"'{entry}' is named in parts '{seen[entry]}' and '{part}'; an instance belongs to one part")
      seen[entry] = part
  gathered = {}
  for part, names in parts.items():
    members = []
    for entry in names:
      sceneObject = bridgeMeshAccess.requireObject(entry)
      if not bridgeKitData.isPlacedPiece(sceneObject) or sceneObject.instance_collection.library is not None:
        raise ValueError(f"'{entry}' is not an instance of one of this file's pieces (placeKitPiece with kitPath null places them); a prefab gathers placed pieces")
      owner = bridgeKitData.prefabPartOf(sceneObject)
      if owner is not None and owner["prefab"] != name:
        raise ValueError(f"'{entry}' is part '{owner['part']}' of prefab '{owner['prefab']}'; assemble that prefab again without it first")
      members.append(sceneObject)
    gathered[part] = members
  return gathered


def requireEntrances(entrances):
  if entrances is None:
    return []
  if not isinstance(entrances, list):
    raise ValueError(f"entrances is a list of {{name, at [x, y, z], facingDegrees}}, got {entrances!r}")
  names, checked = set(), []
  for entrance in entrances:
    if not isinstance(entrance, dict) or set(entrance) != entranceKeys:
      raise ValueError(f"An entrance is {{name, at [x, y, z] in this file, facingDegrees}}: a doorway's threshold middle and the way out of it; got {entrance!r}")
    if not isinstance(entrance["name"], str) or not partNamePattern.fullmatch(entrance["name"]):
      raise ValueError(f"An entrance's name is camelCase (front, back, side), got {entrance['name']!r}")
    if entrance["name"] in names:
      raise ValueError(f"Entrance '{entrance['name']}' is given twice")
    names.add(entrance["name"])
    if not isNumber(entrance["facingDegrees"]):
      raise ValueError(f"Entrance '{entrance['name']}''s facingDegrees is a number, got {entrance['facingDegrees']!r}")
    checked.append({"name": entrance["name"], "at": requirePoint(f"Entrance '{entrance['name']}''s at", entrance["at"]), "facingDegrees": float(entrance["facingDegrees"]) % 360.0})
  return checked


def assemblePrefab(name, parts, entrances):
  bridgeKitData.requireStemmed(name, "A prefab's name")
  existing = next((found for found in bpy.data.collections if found.name == name and found.library is None), None)
  if existing is not None and bridgeKitData.prefabProperty not in existing:
    raise ValueError(f"'{name}' is already the name of a collection in this file that is not a prefab")
  if bpy.data.objects.get(name) is not None:
    raise ValueError(f"'{name}' is already the name of an object in this file")
  gathered = requireParts(name, parts)
  for part in gathered:
    partName = bridgeKitData.partCollectionName(name, part)
    if bpy.data.collections.get(partName) is not None and (existing is None or partName not in existing.children):
      raise ValueError(f"Part '{part}' would be collection '{partName}', which is already the name of another collection in this file")
  entrances = requireEntrances(entrances)
  if interiorPart in gathered and not entrances:
    raise ValueError("A building with an interior part is walked into, so it names at least one entrance {name, at, facingDegrees} at a doorway")
  bpy.context.view_layer.update()
  floor = min(member.matrix_world.translation.z for member in gathered.get("exterior") or [member for members in gathered.values() for member in members])
  positions = numpy.concatenate([bridgeMeshAccess.partTriangles(bridgeMeshAccess.objectParts(member))[0] for members in gathered.values() for member in members])
  hull = convexHull(positions[positions[:, 2] <= floor + stepHeight][:, :2])
  low, high = hull.min(0), hull.max(0)
  origin = numpy.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, floor])
  footprint = hull - origin[:2]
  for entrance in entrances:
    outside = distanceOutside(footprint, numpy.array(entrance["at"][:2]) - origin[:2])
    if outside > stepHeight:
      raise ValueError(f"Entrance '{entrance['name']}' at {roundVector(entrance['at'])} lies {outside:.2f} outside the building's footprint; an entrance is a doorway's threshold, within {stepHeight:g} of it")
  if existing is None:
    existing = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(existing)
  sceneCollection = bpy.context.scene.collection
  wanted = {bridgeKitData.partCollectionName(name, part): members for part, members in gathered.items()}
  for child in list(existing.children):
    for member in list(child.objects):
      if child.name not in wanted or member not in wanted[child.name]:
        child.objects.unlink(member)
        if not member.users_collection:
          sceneCollection.objects.link(member)
    if child.name not in wanted:
      existing.children.unlink(child)
      bpy.data.collections.remove(child)
  for partName, members in wanted.items():
    partCollection = existing.children.get(partName)
    if partCollection is None:
      partCollection = bpy.data.collections.new(partName)
      existing.children.link(partCollection)
    partCollection.instance_offset = origin.tolist()
    for member in members:
      for holder in list(member.users_collection):
        if holder != partCollection:
          holder.objects.unlink(member)
      if partCollection not in member.users_collection:
        partCollection.objects.link(member)
  existing.instance_offset = origin.tolist()
  if existing.asset_data is None:
    existing.asset_mark()
  bridgeKitData.writePrefab(existing, {
    "parts": list(gathered), "footprint": [roundVector(point, 4) for point in footprint],
    "entrances": [{"name": entrance["name"], "at": roundVector(numpy.array(entrance["at"]) - origin, 4), "facingDegrees": entrance["facingDegrees"]} for entrance in entrances],
  })
  bpy.context.view_layer.update()
  return describePrefab(existing)


def describePrefab(collection):
  record = bridgeKitData.readPrefab(collection)
  parts = []
  for part in record["parts"]:
    partCollection = collection.children.get(bridgeKitData.partCollectionName(collection.name, part))
    members = sorted(partCollection.objects, key=lambda member: member.name)
    pieces = {}
    for member in members:
      pieces[member.instance_collection.name] = pieces.get(member.instance_collection.name, 0) + 1
    triangles = sum(bridgeMeshAccess.triangleCount(mesh) for mesh, _ in bridgeMeshAccess.collectionParts(partCollection))
    parts.append({"part": part, "collection": partCollection.name, "instances": [member.name for member in members], "pieces": pieces, "triangles": triangles})
  footprint = numpy.array(record["footprint"])
  return {
    "prefab": collection.name, "kit": bridgeKitData.kitPathOf(collection), "origin": roundVector(collection.instance_offset), "parts": parts,
    "footprint": record["footprint"], "footprintSize": roundVector(footprint.max(0) - footprint.min(0)), "entrances": record["entrances"],
    "triangles": sum(part["triangles"] for part in parts), "fingerprint": bridgeKitData.prefabFingerprint(collection),
  }


def placePrefab(name, kitPath, prefab, location, facingDegrees, plinth, collection):
  return bridgeStructures.buildStructure("prefab", name, {
    "kitPath": kitPath, "prefab": prefab, "location": location, "facingDegrees": facingDegrees, "plinth": plinth, "collection": collection,
  })


def requirePlinth(plinth):
  if plinth is None:
    return None
  requireKeys("plinth", plinth, ("material", "worldUnitsPerRepeat"), ("sink", "margin"))
  given = plinthDefaults | plinth
  return {
    "material": bridgeKits.requireMaterial(given["material"]), "worldUnitsPerRepeat": requirePositive("plinth worldUnitsPerRepeat", given["worldUnitsPerRepeat"]),
    "sink": requireNonNegative("plinth sink", given["sink"]), "margin": requireNonNegative("plinth margin", given["margin"]),
  }


def requirePartCollection(prefab, part):
  partCollection = prefab.children.get(bridgeKitData.partCollectionName(prefab.name, part))
  if partCollection is None:
    raise ValueError(f"Prefab '{prefab.name}' names part '{part}' but holds no collection '{bridgeKitData.partCollectionName(prefab.name, part)}'; assemble it again in its kit")
  return partCollection


def headingVector(degrees):
  return numpy.array([math.sin(math.radians(degrees)), math.cos(math.radians(degrees))])


class Placement:
  """Where a building stands: its [x, y], its floor, its facing, and points of its frame in the world."""

  def __init__(self, location, facingDegrees):
    self.x, self.y = location[0], location[1]
    self.floor = location[2] if len(location) == 3 else None
    self.facingDegrees = facingDegrees
    self.turn = numpy.array(bridgeKitGeometry.turnAbout(facingDegrees))

  def plan(self, points):
    return numpy.asarray(points, dtype=numpy.float64)[:, :2] @ self.turn[:2, :2].T + [self.x, self.y]

  def point(self, at):
    return self.turn @ numpy.asarray(at, dtype=numpy.float64) + [self.x, self.y, self.floor]


def seat(lookups, placement, samples):
  """The floor's height (given, or the highest ground under the footprint found from above, refused where rock lies over ground) and
  the ground under each sample for a floor there."""
  world = placement.plan(samples)
  if placement.floor is None:
    tops = []
    for x, y in world:
      top = lookups.overhead(float(x), float(y))
      if top is None:
        raise ValueError(f"Nothing lies under the footprint at [{x:.1f}, {y:.1f}] to seat the building on")
      tops.append(top)
    placement.floor = max(tops)
  grounds = []
  for x, y in world:
    found = lookups.below((float(x), float(y), placement.floor + stepHeight))
    if found is None:
      raise ValueError(f"No ground within {bridgeStructures.groundReach:g} under the footprint at [{x:.1f}, {y:.1f}] for a floor at {placement.floor:.2f}")
    grounds.append(found)
  return world, numpy.array(grounds)


def plinthMesh(meshName, outline, bottom, material, repeat):
  """A skirt down a counterclockwise outline from the floor (0) to bottom, one quad a side, no top or bottom, mapped along its sides (u
  round the outline, v down from the floor) so its courses run level and round its corners."""
  count = len(outline)
  mesh = bpy.data.meshes.new(meshName)
  mesh.from_pydata([[x, y, 0.0] for x, y in outline] + [[x, y, bottom] for x, y in outline], [], [[count + index, count + (index + 1) % count, (index + 1) % count, index] for index in range(count)])
  mesh.update()
  mesh.materials.append(material)
  runs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.roll(outline, -1, axis=0) - outline, axis=1))])
  uvs = [[along / repeat, height / repeat] for index in range(count) for along, height in ((runs[index], bottom), (runs[index + 1], bottom), (runs[index + 1], 0.0), (runs[index], 0.0))]
  mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName).data.foreach_set("uv", numpy.array(uvs, dtype=numpy.float32).ravel())
  mesh.update()
  return mesh


def entranceReport(lookups, placement, footprint, entrance):
  at = placement.point(entrance["at"])
  heading = placement.facingDegrees + entrance["facingDegrees"]
  leaving = exitDistance(footprint, numpy.array(entrance["at"][:2]), headingVector(entrance["facingDegrees"]))
  outside = numpy.append(at[:2] + headingVector(heading) * (leaving + stepHeight), at[2])
  groundOutside = lookups.footing(outside, entranceFootingReach)
  return {
    "name": entrance["name"], "at": roundVector(at), "facingDegrees": round(heading % 360.0, 4),
    "groundOutside": None if groundOutside is None else round(groundOutside, 3), "stepUp": None if groundOutside is None else round(at[2] - groundOutside, 3),
  }


def layPrefab(laying):
  definition = laying.definition
  prefab = laying.kit.prefab(definition["prefab"])
  record = bridgeKitData.readPrefab(prefab)
  partCollections = [(part, requirePartCollection(prefab, part)) for part in record["parts"]]
  location = requirePoint("location", definition["location"], (2, 3))
  if not isNumber(definition["facingDegrees"]):
    raise ValueError(f"facingDegrees is a number, got {definition['facingDegrees']!r}")
  plinth = requirePlinth(definition["plinth"])
  placement = Placement(location, definition["facingDegrees"] % 360.0)
  footprint = numpy.array(record["footprint"])
  samples = footprintSamples(footprint)
  world, grounds = seat(laying.lookups, placement, samples)
  highest, lowest = int(numpy.argmax(grounds)), int(numpy.argmin(grounds))
  rise = grounds[highest] - placement.floor
  if rise > stepHeight + 1e-6:
    raise ValueError(
      f"The ground at [{world[highest][0]:.1f}, {world[highest][1]:.1f}] inside the footprint stands {rise:.2f} over the floor at {placement.floor:.2f},"
      f" so it would come up through the floor: grade the site, or raise the floor to at least {grounds[highest] - stepHeight:.2f}"
    )
  drop = placement.floor - grounds[lowest]
  if plinth is None and drop > stepHeight + 1e-6:
    raise ValueError(
      f"The floor at {placement.floor:.2f} stands {drop:.2f} over the ground at [{world[lowest][0]:.1f}, {world[lowest][1]:.1f}] under the footprint,"
      f" so it would float: give a plinth, or lower the floor to {grounds[lowest] + stepHeight:.2f} or less"
    )
  standing = (placement.x, placement.y, placement.floor)
  for part, partCollection in partCollections:
    laying.addInstance(laying.name + capitalized(part), partCollection, standing, placement.facingDegrees)
  plinthReport = None
  if plinth is not None:
    bottom = grounds[lowest] - plinth["sink"] - placement.floor
    margin = plinth["margin"]
    outline = footprint if margin == 0 else bridgeKits.offsetOutline(footprint, margin)
    plinthObject = laying.addMeshObject(laying.name + "Plinth", plinthMesh(laying.name + "Plinth", outline, bottom, plinth["material"], plinth["worldUnitsPerRepeat"]), standing, placement.facingDegrees)
    plinthReport = {"top": round(placement.floor, 3), "bottom": round(placement.floor + bottom, 3), "triangles": bridgeMeshAccess.triangleCount(plinthObject)}
  return {
    "prefab": {"kit": bridgeKitData.kitPathOf(prefab), "prefab": prefab.name, "parts": record["parts"], "fingerprint": laying.kit.used[prefab.name]},
    "location": roundVector(standing), "facingDegrees": placement.facingDegrees, "floor": round(placement.floor, 3),
    "ground": {"lowest": round(float(grounds[lowest]), 3), "lowestAt": roundVector(world[lowest], 2), "highest": round(float(grounds[highest]), 3), "highestAt": roundVector(world[highest], 2), "samples": len(samples)},
    "plinth": plinthReport, "entrances": [entranceReport(laying.lookups, placement, footprint, entrance) for entrance in record["entrances"]],
  }


def entranceWalk(entrance):
  """A walk from entranceWalkDistance outside an entrance to as far inside, at its threshold's height (outside at the ground found there)."""
  at, outward = numpy.array(entrance["at"]), headingVector(entrance["facingDegrees"])
  start = numpy.append(at[:2] + outward * entranceWalkDistance, at[2] if entrance["groundOutside"] is None else entrance["groundOutside"])
  end = numpy.append(at[:2] - outward * entranceWalkDistance, at[2])
  path = [[float(value) for value in start], [float(value) for value in end]]
  try:
    walk = bridgeReview.walkRoute(path, walkSampleSpacing)
  except ValueError as refusal:
    return {"path": path, "walkable": False, "refused": str(refusal)}
  return {"path": [roundVector(point) for point in path]} | {key: walk[key] for key in ("walkable", "problems", "oneWay", "narrowest", "lowestHeadroom")}


def finishPrefab(collection, report):
  """A walk-in building's entrances walked, once its parts stand in the scene."""
  if interiorPart not in report["prefab"]["parts"]:
    return {}
  return {"entrances": [entrance | {"walk": entranceWalk(entrance)} for entrance in report["entrances"]]}


def prefabViews(definition, groundHeight):
  """entrance<Name>: standing entranceViewDistance out from each entrance, looking at it."""
  record = bridgeKitData.readPrefab(bridgeKitData.requirePrefab(bridgeStructures.absoluteKitPath(definition["kitPath"]), definition["prefab"]))
  placement = Placement(definition["location"][:2] + [0.0], definition["facingDegrees"] % 360.0)
  views = {}
  for entrance in record["entrances"]:
    heading = placement.facingDegrees + entrance["facingDegrees"]
    standAt = placement.plan([entrance["at"]])[0] + headingVector(heading) * entranceViewDistance
    views["entrance" + capitalized(entrance["name"])] = {"standAt": roundVector(standAt), "headingDegrees": round((heading + 180.0) % 360.0, 4), "pitchDegrees": 0.0}
  return views


bridgeStructures.registerKind("prefab", "placePrefab", prefabKeys, layPrefab, None, prefabViews, finish=finishPrefab)

commands = {
  "assemblePrefab": (assemblePrefab, True),
  "placePrefab": (placePrefab, True),
}
