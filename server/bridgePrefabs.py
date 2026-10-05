"""Buildings from kit pieces, as the client splits its buildings into models placed together: placed pieces of a kit file gathered into
a prefab's parts (assemblePrefab), and a prefab placed in a zone as a structure, one instance per part, seated on the ground under its
footprint, on a plinth where the ground falls away (placePrefab, kind prefab). Runs under Blender's Python."""
import math
import numbers
import re

import bpy
import mathutils
import numpy

import bridgeKitData
import bridgeKits
import bridgeMeshAccess
import bridgeReview
import bridgeStructureData
import bridgeStructures
import bridgeSurfacing
import playerScale

partNamePattern = re.compile(r"[a-z][A-Za-z0-9]*")
entranceKeys = {"name", "at", "facingDegrees"}
plinthKeys = {"material", "worldUnitsPerRepeat", "sink", "margin"}
plinthDefaults = {"sink": 2.0, "margin": 0.0}
definitionKeys = ("kitPath", "prefab", "location", "facingDegrees", "plinth", "collection")
interiorPart = "interior"
# The ground under a footprint is looked up this far apart across it and along its sides.
sampleSpacing = 2.0
# An entrance's walk runs from this far outside it to this far inside; its view stands this far out.
entranceWalk = 10.0
entranceViewDistance = 25.0
walkSampleSpacing = 1.0
stepHeight = playerScale.stepHeight
hullTolerance = 1e-3


def isNumber(value):
  return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value)


def convexHull(points):
  """The convex hull of [x, y] points, counterclockwise from the lowest-leftmost, without points within hullTolerance of the line
  between their neighbours (placements' float32 turns move a piece's corners by about a millionth)."""
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


def offsetHull(hull, distance):
  """A counterclockwise convex outline with each side moved out by distance, its corners mitered."""
  if distance == 0:
    return hull.copy()
  return bridgeKits.offsetOutline(hull, distance)


def distanceOutside(hull, point):
  """How far a point lies outside a counterclockwise convex outline in plan, 0 inside it."""
  sides = numpy.roll(hull, -1, axis=0) - hull
  normals = numpy.stack([sides[:, 1], -sides[:, 0]], axis=1) / numpy.linalg.norm(sides, axis=1)[:, None]
  outside = ((numpy.asarray(point) - hull) * normals).sum(axis=1)
  if outside.max() <= 0:
    return 0.0
  nearest = []
  for start, side in zip(hull, sides):
    share = numpy.clip(numpy.dot(point - start, side) / numpy.dot(side, side), 0, 1)
    nearest.append(numpy.linalg.norm(point - (start + share * side)))
  return float(min(nearest))


def exitDistance(hull, point, direction):
  """How far from a point inside a convex outline a ray in a plan direction leaves it, 0 for a point outside."""
  if distanceOutside(hull, point) > 0:
    return 0.0
  sides = numpy.roll(hull, -1, axis=0) - hull
  normals = numpy.stack([sides[:, 1], -sides[:, 0]], axis=1)
  facing = normals @ direction
  reaches = [numpy.dot(start - point, normal) / along for start, normal, along in zip(hull, normals, facing) if along > 1e-12]
  return float(min(reaches))


def footprintSamples(hull):
  """Points across a footprint where its ground is looked up: its corners, along its sides, and a grid inside it."""
  samples = [corner for corner in hull]
  for start, end in zip(hull, numpy.roll(hull, -1, axis=0)):
    count = max(1, math.ceil(numpy.linalg.norm(end - start) / sampleSpacing))
    samples.extend(start + (end - start) * step / count for step in range(1, count))
  low, high = hull.min(0), hull.max(0)
  xs = numpy.arange(low[0] + sampleSpacing / 2, high[0], sampleSpacing)
  ys = numpy.arange(low[1] + sampleSpacing / 2, high[1], sampleSpacing)
  grid = numpy.array([[x, y] for x in xs for y in ys]).reshape(-1, 2)
  if len(grid):
    samples.extend(grid[bridgeMeshAccess.insidePolygon(grid, hull)])
  return numpy.array(samples)


def capitalized(part):
  return part[0].upper() + part[1:]


def prefabPartOf(sceneObject):
  """The prefab and part an object is gathered into, or None."""
  for collection in sceneObject.users_collection:
    for prefab in bpy.data.collections:
      if bridgeKitData.prefabProperty in prefab and collection.name in prefab.children:
        record = bridgeKitData.readPrefab(prefab)
        part = next((name for name in record["parts"] if bridgeKitData.partCollectionName(prefab.name, name) == collection.name), None)
        if part is not None:
          return prefab, part
  return None


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
      owner = prefabPartOf(sceneObject)
      if owner is not None and owner[0].name != name:
        raise ValueError(f"'{entry}' is part '{owner[1]}' of prefab '{owner[0].name}'; assemble that prefab again without it first")
      members.append(sceneObject)
    gathered[part] = members
  return gathered


def requireEntrances(entrances):
  if entrances is None:
    return []
  if not isinstance(entrances, list):
    raise ValueError(f"entrances is a list of {{name, at [x, y, z], facingDegrees}}, got {entrances!r}")
  names = set()
  checked = []
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
    checked.append({"name": entrance["name"], "at": bridgeKits.requirePoint(f"Entrance '{entrance['name']}''s at", entrance["at"]), "facingDegrees": float(entrance["facingDegrees"]) % 360.0})
  return checked


def worldVertices(sceneObject):
  positions, _ = bridgeMeshAccess.partTriangles(bridgeMeshAccess.objectParts(sceneObject))
  return positions


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
    taken = bpy.data.collections.get(partName)
    if taken is not None and (existing is None or partName not in existing.children):
      raise ValueError(f"Part '{part}' would be collection '{partName}', which is already the name of another collection in this file")
  entrances = requireEntrances(entrances)
  if interiorPart in gathered and not entrances:
    raise ValueError("A building with an interior part is walked into, so it names at least one entrance {name, at, facingDegrees} at a doorway")
  bpy.context.view_layer.update()
  floorers = gathered.get("exterior") or [member for members in gathered.values() for member in members]
  floor = min(member.matrix_world.translation.z for member in floorers)
  positions = numpy.concatenate([worldVertices(member) for members in gathered.values() for member in members])
  standing = positions[positions[:, 2] <= floor + stepHeight]
  hull = convexHull(standing[:, :2])
  low, high = hull.min(0), hull.max(0)
  origin = numpy.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, floor])
  footprint = hull - origin[:2]
  for entrance in entrances:
    outside = distanceOutside(footprint, numpy.array(entrance["at"][:2]) - origin[:2])
    if outside > stepHeight:
      raise ValueError(f"Entrance '{entrance['name']}' at {bridgeKitData.roundVector(entrance['at'], 3)} lies {outside:.2f} outside the building's footprint; an entrance is a doorway's threshold, within {stepHeight:g} of it")
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
  record = {
    "parts": list(gathered), "footprint": [[bridgeKitData.plain(round(value, 4)) for value in point] for point in footprint],
    "entrances": [{"name": entrance["name"], "at": bridgeKitData.roundVector(numpy.array(entrance["at"]) - origin, 4), "facingDegrees": entrance["facingDegrees"]} for entrance in entrances],
  }
  bridgeKitData.writePrefab(existing, record)
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
    "prefab": collection.name, "kit": bridgeKitData.kitPathOf(collection), "origin": bridgeKitData.roundVector(collection.instance_offset), "parts": parts,
    "footprint": record["footprint"], "footprintSize": bridgeKitData.roundVector(footprint.max(0) - footprint.min(0)), "entrances": record["entrances"],
    "triangles": sum(part["triangles"] for part in parts), "fingerprint": bridgeKitData.prefabFingerprint(collection),
  }


def requirePlinth(plinth):
  if plinth is None:
    return None
  if not isinstance(plinth, dict) or not {"material", "worldUnitsPerRepeat"} <= set(plinth) <= plinthKeys:
    raise ValueError(f"plinth is {{material, worldUnitsPerRepeat, sink (2), margin (0)}}, got {plinth!r}")
  bridgeKits.requireMaterial(plinth["material"])
  checked = plinthDefaults | {key: value for key, value in plinth.items() if key != "material"}
  bridgeKits.requirePositive("plinth worldUnitsPerRepeat", checked["worldUnitsPerRepeat"])
  for key in ("sink", "margin"):
    if not isNumber(checked[key]) or checked[key] < 0:
      raise ValueError(f"plinth {key} is 0 or more, got {checked[key]!r}")
  return {"material": plinth["material"]} | {key: float(checked[key]) for key in ("worldUnitsPerRepeat", "sink", "margin")}


def definePrefab(arguments):
  bridgeStructures.requireStructureCollection(arguments["collection"])
  if not isinstance(arguments["prefab"], str) or not arguments["prefab"]:
    raise ValueError(f"prefab names a prefab of the kit, got {arguments['prefab']!r}")
  location = bridgeKits.requirePoint("location", arguments["location"], (2, 3))
  if not isNumber(arguments["facingDegrees"]):
    raise ValueError(f"facingDegrees is a number, got {arguments['facingDegrees']!r}")
  return {
    "kitPath": bridgeStructures.keptKitPath(arguments["kitPath"]), "prefab": arguments["prefab"], "location": location,
    "facingDegrees": float(arguments["facingDegrees"]) % 360.0, "plinth": requirePlinth(arguments["plinth"]), "collection": arguments["collection"],
  }


def placePrefab(name, kitPath, prefab, location, facingDegrees, plinth, collection):
  bridgeStructures.requireNewStructureName(name)
  definition = definePrefab({"kitPath": kitPath, "prefab": prefab, "location": location, "facingDegrees": facingDegrees, "plinth": plinth, "collection": collection})
  return bridgeStructures.layStructure(name, "prefab", definition)


def requirePartCollection(prefab, part):
  partCollection = prefab.children.get(bridgeKitData.partCollectionName(prefab.name, part))
  if partCollection is None:
    raise ValueError(f"Prefab '{prefab.name}' names part '{part}' but holds no collection '{bridgeKitData.partCollectionName(prefab.name, part)}'; assemble it again in its kit")
  return partCollection


def headingVector(degrees):
  return numpy.array([math.sin(math.radians(degrees)), math.cos(math.radians(degrees))])


class Placement:
  """Where a prefab stands: the building's [x, y] and floor, its facing, and points of its frame in the world."""

  def __init__(self, x, y, floor, facingDegrees):
    self.x, self.y, self.floor, self.facingDegrees = x, y, floor, facingDegrees
    self.turn = numpy.array(bridgeStructures.turnAbout(facingDegrees))

  def plan(self, points):
    return numpy.asarray(points, dtype=numpy.float64)[:, :2] @ self.turn[:2, :2].T + [self.x, self.y]

  def point(self, at):
    return self.turn @ numpy.asarray(at, dtype=numpy.float64) + [self.x, self.y, self.floor]


def roundPoint(values, digits=3):
  return bridgeKitData.roundVector(values, digits)


def seat(ground, placement, samples, given):
  """The floor's height (given, or the highest ground under the footprint found from above, refused where rock lies over ground) and
  the ground under each sample for a floor there."""
  world = placement.plan(numpy.column_stack([samples, numpy.zeros(len(samples))]))
  if given is None:
    tops = []
    for x, y in world:
      top, levels = ground.overhead(float(x), float(y))
      if levels is not None:
        raise ValueError(f"At [{x:.1f}, {y:.1f}] under the footprint {bridgeMeshAccess.describeRockOverGround([round(float(x), 1), round(float(y), 1)], [round(level, 1) for level in levels])}, so which ground is a choice: give location [x, y, z]")
      if top is None:
        raise ValueError(f"Nothing lies under the footprint at [{x:.1f}, {y:.1f}] to seat the building on")
      tops.append(top)
    placement.floor = max(tops)
  grounds = []
  for x, y in world:
    found = ground.level(float(x), float(y), placement.floor)
    if found is None:
      raise ValueError(f"No ground under the footprint at [{x:.1f}, {y:.1f}] for a floor at {placement.floor:.2f}")
    grounds.append(found)
  return world, numpy.array(grounds)


def plinthMesh(meshName, hull, top, bottom, material, repeat):
  """A skirt down a counterclockwise outline from top to bottom (heights relative to the floor), one quad a side, no top or bottom,
  mapped along its sides (u round the outline, v down from the floor) so its courses run level and round its corners."""
  count = len(hull)
  positions = [[x, y, top] for x, y in hull] + [[x, y, bottom] for x, y in hull]
  faces = [[count + index, count + (index + 1) % count, (index + 1) % count, index] for index in range(count)]
  mesh = bpy.data.meshes.new(meshName)
  mesh.from_pydata(positions, [], faces)
  mesh.update()
  mesh.materials.append(material)
  runs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.roll(hull, -1, axis=0) - hull, axis=1))])
  uvs = []
  for index in range(count):
    for along, height in ((runs[index], bottom), (runs[index + 1], bottom), (runs[index + 1], top), (runs[index], top)):
      uvs.append([along / repeat, (height - top) / repeat])
  mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName).data.foreach_set("uv", numpy.array(uvs, dtype=numpy.float32).ravel())
  mesh.update()
  return mesh


def entranceReport(laying, placement, footprint, entrance, walked):
  at = placement.point(entrance["at"])
  heading = placement.facingDegrees + entrance["facingDegrees"]
  outward = headingVector(heading)
  local = numpy.array(entrance["at"][:2])
  leaving = exitDistance(footprint, local, headingVector(entrance["facingDegrees"]))
  outside = numpy.append(at[:2] + outward * (leaving + stepHeight), at[2])
  groundOutside = laying.ground.footing(outside)
  report = {
    "name": entrance["name"], "at": roundPoint(at), "facingDegrees": round(heading % 360.0, 4), "groundOutside": None if groundOutside is None else round(groundOutside, 3),
    "stepUp": None if groundOutside is None else round(at[2] - groundOutside, 3),
  }
  if walked:
    start = numpy.append(at[:2] + outward * entranceWalk, at[2] if groundOutside is None else groundOutside)
    end = numpy.append(at[:2] - outward * entranceWalk, at[2])
    path = [[float(value) for value in start], [float(value) for value in end]]
    try:
      walk = bridgeReview.walkRoute(path, walkSampleSpacing, laying.ownParts)
    except ValueError as refusal:
      walk = {"walkable": False, "refused": str(refusal)}
    report["walk"] = {"path": [roundPoint(point) for point in path]} | {key: walk[key] for key in ("walkable", "refused", "problems", "oneWay", "narrowest", "lowestHeadroom") if key in walk}
  return report


def layPrefab(laying):
  definition = laying.definition
  prefab = laying.useSource(bridgeKitData.requirePrefab(bridgeStructures.absoluteKitPath(definition["kitPath"]), definition["prefab"]))
  record = bridgeKitData.readPrefab(prefab)
  partCollections = [(part, requirePartCollection(prefab, part)) for part in record["parts"]]
  location = definition["location"]
  placement = Placement(location[0], location[1], location[2] if len(location) == 3 else None, definition["facingDegrees"])
  footprint = numpy.array(record["footprint"])
  samples = footprintSamples(footprint)
  world, grounds = seat(laying.ground, placement, samples, placement.floor)
  highest, lowest = int(numpy.argmax(grounds)), int(numpy.argmin(grounds))
  rise = grounds[highest] - placement.floor
  if rise > stepHeight + 1e-6:
    raise ValueError(
      f"The ground at [{world[highest][0]:.1f}, {world[highest][1]:.1f}] inside the footprint stands {rise:.2f} over the floor at {placement.floor:.2f},"
      f" so it would come up through the floor: grade the site, or raise the floor to at least {grounds[highest] - stepHeight:.2f}"
    )
  drop = placement.floor - grounds[lowest]
  plinth = definition["plinth"]
  if plinth is None and drop > stepHeight + 1e-6:
    raise ValueError(
      f"The floor at {placement.floor:.2f} stands {drop:.2f} over the ground at [{world[lowest][0]:.1f}, {world[lowest][1]:.1f}] under the footprint,"
      f" so it would float: give a plinth, or lower the floor to {grounds[lowest] + stepHeight:.2f} or less"
    )
  for part, partCollection in partCollections:
    instance = bpy.data.objects.new(laying.name + capitalized(part), None)
    instance.instance_type = "COLLECTION"
    instance.instance_collection = partCollection
    instance.location = (placement.x, placement.y, placement.floor)
    instance.rotation_euler = (0.0, 0.0, math.radians(-placement.facingDegrees))
    laying.addPart(instance, laying.name + capitalized(part))
  plinthReport = None
  if plinth is not None:
    outline = offsetHull(footprint, plinth["margin"])
    bottom = grounds[lowest] - plinth["sink"] - placement.floor
    meshName = laying.name + "Plinth"
    mesh = plinthMesh(meshName, outline, 0.0, bottom, bridgeKits.requireMaterial(plinth["material"]), plinth["worldUnitsPerRepeat"])
    plinthObject = bpy.data.objects.new(meshName, mesh)
    plinthObject.location = (placement.x, placement.y, placement.floor)
    plinthObject.rotation_euler = (0.0, 0.0, math.radians(-placement.facingDegrees))
    laying.addPart(plinthObject, meshName)
    plinthReport = {"top": round(placement.floor, 3), "bottom": round(placement.floor + bottom, 3), "triangles": bridgeMeshAccess.triangleCount(plinthObject)}
  bpy.context.view_layer.update()
  walked = interiorPart in record["parts"]
  entrances = [entranceReport(laying, placement, footprint, entrance, walked) for entrance in record["entrances"]]
  return {
    "prefab": {"kit": bridgeKitData.kitPathOf(prefab), "prefab": prefab.name, "parts": record["parts"], "fingerprint": bridgeKitData.prefabFingerprint(prefab)},
    "location": roundPoint([placement.x, placement.y, placement.floor]), "facingDegrees": placement.facingDegrees, "floor": round(placement.floor, 3),
    "ground": {"lowest": round(float(grounds[lowest]), 3), "lowestAt": roundPoint(world[lowest], 2), "highest": round(float(grounds[highest]), 3), "highestAt": roundPoint(world[highest], 2), "samples": len(samples)},
    "plinth": plinthReport, "entrances": entrances,
  }


def placedFrame(structure):
  """A placed prefab's prefab record and where it stands, from its parts as laid; None when its prefab is missing."""
  sources = structure.get(bridgeStructures.sourcesProperty) or {}
  definition = bridgeStructureData.readStructure(structure)["definition"]
  prefab = sources.get(definition["prefab"])
  instances = [part for part in bridgeStructureData.partsOf(structure) if bridgeMeshAccess.isCollectionInstance(part)]
  if prefab is None or prefab.is_missing or not instances:
    return None
  location = instances[0].matrix_world.translation
  return bridgeKitData.readPrefab(prefab), Placement(location.x, location.y, location.z, definition["facingDegrees"])


def prefabViews(structure):
  framed = placedFrame(structure)
  views = {}
  if framed is not None:
    record, placement = framed
    for entrance in record["entrances"]:
      at = placement.point(entrance["at"])
      heading = placement.facingDegrees + entrance["facingDegrees"]
      standAt = at[:2] + headingVector(heading) * entranceViewDistance
      views["entrance" + capitalized(entrance["name"])] = {
        "standAt": roundPoint([standAt[0], standAt[1], at[2]]), "headingDegrees": round((heading + 180.0) % 360.0, 4), "pitchDegrees": 0.0,
      }
  views["orbit"] = {"objects": [part.name for part in bridgeStructureData.partsOf(structure)]}
  return views


bridgeStructures.registerKind("prefab", definitionKeys, definePrefab, layPrefab, prefabViews)

commands = {
  "assemblePrefab": (assemblePrefab, True),
  "placePrefab": (placePrefab, True),
}
