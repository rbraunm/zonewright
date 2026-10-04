"""Player boundaries as an artist designs them, never derived: invisible walls (ribbons along a path from under the ground to a height
above it, as the client's invw walls run), lids and floors (flat outlines at a height), objects players pass through, and zone lines
(ATP_ boxes with a number, a label, and a target). Boundaries are never drawn in the client's view and block players; export merges
them into the terrain as material -1 triangles, flags liquid surfaces, cutout cards, and objects marked passable 0x1, and writes zone
lines as .zon regions. Runs under Blender's Python."""
import json
import math
import re

import bpy
import mathutils
import mathutils.bvhtree
import mathutils.geometry
import numpy

import bridgeMeshAccess
import bridgeObjects
import bridgeSurfacing
import bridgeSwim

boundaryCollectionName = "boundaries"
zoneLineCollectionName = "zoneLines"
planeKinds = ("lid", "floor")
# A wall runs from this far under the ground to its height above it, as the client's invw walls do (EQGraphicsDX9.dll 0x1010bb50).
wallDepth = 5.0
wallSampleSpacing = 4.0
# The client reads a zone line's number with atoi right after ATP_ (eqgame 0x487530), so its digits follow ATP_ directly.
zoneLinePattern = re.compile(r"^ATP_([0-9]+)_([A-Za-z0-9_]+)$")
labelPattern = re.compile(r"^[A-Za-z0-9_]+$")
leadingDigits = re.compile(r"^ATP_([0-9]*)_?(.*)$", re.IGNORECASE)
targetCoordinates = ("x", "y", "z")
targetKeys = ("zone",) + targetCoordinates + ("headingDegrees",)
keep = "keep"
turnTolerance = 1e-9


def isPassableMaterial(material):
  """Players pass through liquid surfaces (water, waterfall, lava) and cutout cards, as the client's own zones flag them (0x1)."""
  return material is not None and (bridgeSurfacing.liquidPropertyName in material or bool(material.get(bridgeSurfacing.cutoutPropertyName)))


def isAuthored(sceneObject):
  return bridgeMeshAccess.clientContentProperty not in sceneObject


def boundaryObjects():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.boundaryProperty in sceneObject), key=lambda found: found.name)


def zoneLineObjects():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.zoneLineProperty in sceneObject), key=lambda found: found.name)


def readSpec(sceneObject, propertyName):
  return json.loads(sceneObject[propertyName])


def evaluatedTriangles(sceneObject, depsgraph):
  """An evaluated mesh's vertex positions in its own space, its triangles, each triangle's polygon, and its material slots."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    positions = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", positions)
    triangles = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    polygons = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("polygon_index", polygons)
    slots = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("material_index", slots)
    flagged = numpy.zeros(len(mesh.polygons), dtype=bool)
    attribute = mesh.attributes.get(bridgeMeshAccess.passableAttribute)
    if attribute is not None:
      attribute.data.foreach_get("value", flagged)
    materials = [slot.material for slot in evaluated.material_slots]
  finally:
    evaluated.to_mesh_clear()
  return positions.reshape(-1, 3), triangles.reshape(-1, 3), polygons, slots, flagged, materials


def passableFaces(slots, flagged, materials):
  """Which faces players pass through: a liquid or cutout material, or flagged so by the zone file it came from."""
  slotPassable = numpy.array([isPassableMaterial(material) for material in materials] + [False], dtype=bool)
  return slotPassable[numpy.minimum(slots, len(materials))] | flagged


def collisionSurfaces(boundaries=True):
  """Ray casts against what the client collides with: what players stand on, without faces they pass through (liquids, cutout cards,
  objects marked passable, faces an imported zone file flags), and the boundaries unless boundaries is false."""
  owners = bridgeMeshAccess.playerSolidObjects(collision=True)
  depsgraph = bpy.context.evaluated_depsgraph_get()
  trees = []
  for owner in owners:
    if not boundaries and bridgeMeshAccess.boundaryProperty in owner:
      continue
    for part, matrix in bridgeMeshAccess.objectParts(owner):
      if bridgeMeshAccess.passableProperty in part:
        continue
      positions, triangles, polygons, slots, flagged, materials = evaluatedTriangles(part, depsgraph)
      kept = ~passableFaces(slots, flagged, materials)[polygons]
      if kept.all():
        trees.append((owner.name, matrix, mathutils.bvhtree.BVHTree.FromObject(part, depsgraph)))
      elif kept.any():
        trees.append((owner.name, matrix, mathutils.bvhtree.BVHTree.FromPolygons(positions.tolist(), triangles[kept].tolist())))
  return bridgeMeshAccess.PlayerSurfaces(trees=trees)


def boundarySurfaces():
  """Ray casts against the boundaries alone, or None when the scene has none."""
  bpy.context.view_layer.update()
  depsgraph = bpy.context.evaluated_depsgraph_get()
  trees = [(found.name, found.matrix_world.copy(), mathutils.bvhtree.BVHTree.FromObject(found, depsgraph)) for found in boundaryObjects() if found.type == "MESH"]
  return bridgeMeshAccess.PlayerSurfaces(trees=trees) if trees else None


def sceneHeightSpan():
  heights = [(sceneObject.matrix_world @ mathutils.Vector(corner)).z for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.bound_box]
  if not heights:
    raise ValueError("The scene has no meshes to stand a boundary on")
  return min(heights), max(heights)


def requireBoundaryName(name):
  """A boundary's name: new, or an existing boundary's, which is then redrawn."""
  if not isinstance(name, str) or not name.strip():
    raise ValueError(f"A boundary's name is a non-empty string, got {name!r}")
  existing = bpy.data.objects.get(name)
  if existing is not None and bridgeMeshAccess.boundaryProperty not in existing:
    raise ValueError(f"An object named '{name}' already exists and is not a boundary")
  if existing is not None and not isAuthored(existing):
    raise ValueError(f"'{name}' came in with an imported zone; it is reference, not this zone's boundary")
  return existing


def requirePositive(label, value):
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
    raise ValueError(f"{label} must be a positive number, got {value!r}")


def linkBoundary(name, existing, positions, faces, spec):
  if existing is not None:
    data = existing.data
    bpy.data.objects.remove(existing)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
      bpy.data.meshes.remove(data)
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata([list(map(float, point)) for point in positions], [], faces)
  mesh.update()
  mesh.validate()
  boundary = bpy.data.objects.new(name, mesh)
  boundary.hide_render = True
  boundary[bridgeMeshAccess.boundaryProperty] = json.dumps(spec)
  bridgeObjects.targetCollection(boundaryCollectionName).objects.link(boundary)
  bpy.context.view_layer.update()
  return boundary


def pathSamples(pathArray, closed):
  """Points along a path at most wallSampleSpacing apart, its own points among them; a closed path's last point is its first."""
  points = []
  for start, end in zip(pathArray[:-1], pathArray[1:]):
    pieces = max(1, math.ceil(numpy.linalg.norm(end - start) / wallSampleSpacing - 1e-9))
    points.extend(start + (end - start) * (numpy.arange(pieces) / pieces)[:, None])
  if not closed:
    points.append(pathArray[-1])
  return numpy.array(points)


def placeBoundaryWall(name, path, height):
  """A wall along a path of [x, y] points, from wallDepth under the ground players stand on to `height` above it, sampled every
  wallSampleSpacing along the path; a path ending where it began closes into a ring. It faces to the left of the path's direction
  (inward for a ring drawn counterclockwise). Placing a wall again by name redraws it against the ground as it is now."""
  existing = requireBoundaryName(name)
  requirePositive("height", height)
  pathArray = bridgeMeshAccess.toArray(path)
  if pathArray.ndim != 2 or pathArray.shape[1] != 2 or len(pathArray) < 2:
    raise ValueError(f"A wall's path is at least two [x, y] points, got {path!r}")
  steps = numpy.linalg.norm(numpy.diff(pathArray, axis=0), axis=1)
  if (steps < 1e-6).any():
    raise ValueError(f"The wall's path repeats a point at index {int(numpy.argmax(steps < 1e-6)) + 1}; each point must differ from the one before it")
  closed = bool(numpy.linalg.norm(pathArray[-1] - pathArray[0]) < 1e-6)
  if closed and len(pathArray) < 4:
    raise ValueError("A closed wall's path needs at least three corners before it returns to its first point")
  samples = pathSamples(pathArray, closed)
  bottom, top = sceneHeightSpan()
  ground = collisionSurfaces(boundaries=False)
  heights = []
  for x, y in samples:
    hit = ground.footingBelow(mathutils.Vector((float(x), float(y), top + 1)), top - bottom + 2)
    if hit is None:
      raise ValueError(f"No ground players stand on under [{x:.1f}, {y:.1f}] on the wall's path")
    heights.append(hit.z)
  heights = numpy.array(heights)
  count = len(samples)
  positions = [(x, y, z - wallDepth) for (x, y), z in zip(samples, heights)] + [(x, y, z + height) for (x, y), z in zip(samples, heights)]
  faces = [(index, count + index, count + (index + 1) % count, (index + 1) % count) for index in range(count if closed else count - 1)]
  boundary = linkBoundary(name, existing, positions, faces, {"kind": "wall", "path": pathArray.tolist(), "height": height})
  return describeBoundary(boundary) | {"replaced": existing is not None, "samples": count, "ground": [round(float(heights.min()), 2), round(float(heights.max()), 2)]}


def placeBoundaryPlane(name, kind, outline, height):
  """A flat boundary over a closed outline at a height: a lid (facing down: over a gap players must not climb or fly out of) or a floor
  (facing up: under a fall players must not drop out through). Placing it again by name redraws it."""
  if kind not in planeKinds:
    raise ValueError(f"kind is one of {list(planeKinds)}, got {kind!r}")
  existing = requireBoundaryName(name)
  if isinstance(height, bool) or not isinstance(height, (int, float)) or not math.isfinite(height):
    raise ValueError(f"height must be a number, got {height!r}")
  outlineArray = bridgeObjects.requireSimpleOutline(outline)
  positions = [(float(x), float(y), float(height)) for x, y in outlineArray]
  triangles = mathutils.geometry.tessellate_polygon([[mathutils.Vector(point) for point in positions]])
  facing = 1.0 if kind == "floor" else -1.0
  faces = []
  for triangle in triangles:
    a, b, c = (numpy.array(positions[index]) for index in triangle)
    faces.append(tuple(triangle) if numpy.cross(b - a, c - a)[2] * facing > 0 else tuple(reversed(triangle)))
  boundary = linkBoundary(name, existing, positions, faces, {"kind": kind, "outline": outlineArray.tolist(), "height": height})
  return describeBoundary(boundary) | {"replaced": existing is not None}


def worldBounds(sceneObject):
  corners = numpy.array([list(sceneObject.matrix_world @ mathutils.Vector(corner)) for corner in sceneObject.bound_box])
  return [round(float(value), 2) for value in corners.min(0)], [round(float(value), 2) for value in corners.max(0)]


def describeBoundary(boundary):
  spec = readSpec(boundary, bridgeMeshAccess.boundaryProperty)
  minimum, maximum = worldBounds(boundary)
  described = {"name": boundary.name, "kind": spec["kind"], "triangles": bridgeMeshAccess.triangleCount(boundary) if boundary.type == "MESH" else 0, "minimum": minimum, "maximum": maximum}
  if spec["kind"] == "wall":
    path = numpy.array(spec["path"])
    described |= {"path": spec["path"], "height": spec["height"], "length": round(float(numpy.linalg.norm(numpy.diff(path, axis=0), axis=1).sum()), 1)}
  elif spec["kind"] in planeKinds:
    outline = numpy.array(spec["outline"])
    ends = numpy.roll(outline, -1, axis=0)
    described |= {"outline": spec["outline"], "height": spec["height"], "area": round(float(abs((outline[:, 0] * ends[:, 1] - ends[:, 0] * outline[:, 1]).sum()) / 2), 1)}
  if not isAuthored(boundary):
    described["clientContent"] = boundary[bridgeMeshAccess.clientContentProperty]
  return described


def markPassable(objects, passable):
  """Mark objects players pass through (art with its own collision shell, hanging moss, a curtain), or take the mark off; export flags
  every triangle of a marked object 0x1, as it does liquid surfaces and cutout cards."""
  if not isinstance(passable, bool):
    raise ValueError(f"passable must be true or false, got {passable!r}")
  if not objects:
    raise ValueError("objects names at least one mesh or collection instance")
  marked = []
  for name in objects:
    sceneObject = bridgeMeshAccess.requireObject(name)
    if sceneObject.type != "MESH" and not bridgeMeshAccess.isCollectionInstance(sceneObject):
      raise ValueError(f"'{name}' is a {sceneObject.type}; only meshes and collection instances are passed through")
    if bridgeMeshAccess.boundaryProperty in sceneObject:
      raise ValueError(f"'{name}' is a boundary, which exists to block players")
    if bridgeMeshAccess.isDesignAid(sceneObject) or bridgeMeshAccess.regionIntentProperty in sceneObject or not isAuthored(sceneObject):
      raise ValueError(f"'{name}' is not the zone's own geometry (a guide, plot border, region, or placed client content)")
    marked.append(sceneObject)
  for sceneObject in marked:
    if passable:
      sceneObject[bridgeMeshAccess.passableProperty] = True
    elif bridgeMeshAccess.passableProperty in sceneObject:
      del sceneObject[bridgeMeshAccess.passableProperty]
  return {"objects": [describePassable(sceneObject) for sceneObject in marked]}


def describePassable(sceneObject):
  triangles = sum(bridgeMeshAccess.triangleCount(part) for part, _ in bridgeMeshAccess.objectParts(sceneObject))
  return {"object": sceneObject.name, "passable": bridgeMeshAccess.passableProperty in sceneObject, "triangles": triangles}


def requireCorners(minimum, maximum):
  for corner in (minimum, maximum):
    if not isinstance(corner, list) or len(corner) != 3 or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in corner):
      raise ValueError(f"minimum and maximum are [x, y, z] corners, got {minimum!r} and {maximum!r}")
  if any(high <= low for low, high in zip(minimum, maximum)):
    raise ValueError(f"Every maximum must lie above its minimum, got {minimum} and {maximum}")


def requireTarget(target):
  """A zone line's target: the zone's short name, and x, y, z (zone file axes, as /loc prints them) and headingDegrees (0 = +Y,
  clockwise), each a number or "keep" (the player's own, written 999999 and 999 in the server's row)."""
  if not isinstance(target, dict) or set(target) != set(targetKeys):
    raise ValueError(f"target is {{{', '.join(targetKeys)}}}, each coordinate and the heading a number or \"keep\"; got {target!r}")
  if not isinstance(target["zone"], str) or not target["zone"]:
    raise ValueError(f"target zone is the short name of the zone the line leads to, got {target['zone']!r}")
  for key in targetCoordinates + ("headingDegrees",):
    value = target[key]
    if value == keep:
      continue
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
      raise ValueError(f"target {key} is a number or \"keep\", got {value!r}")
  heading = target["headingDegrees"]
  if heading != keep and not 0 <= heading < 360:
    raise ValueError(f"target headingDegrees runs from 0 up to 360, got {heading}")


def parsedName(name):
  """A zone line's number and label as the client reads its name: atoi on the digits after ATP_ (0 without any), the rest its label."""
  match = leadingDigits.match(name)
  return (int(match.group(1)) if match and match.group(1) else 0), (match.group(2) if match else "")


def boxBounds(box):
  location, rotation, scale = box.matrix_world.decompose()
  return [round(float(value), 4) for value in location], [round(float(value), 4) for value in scale]


def boxCorners(box):
  """A box empty's eight world corners, x changing fastest, then y, then z: the first four its bottom."""
  return [list(box.matrix_world @ mathutils.Vector((x, y, z))) for z in (-1, 1) for y in (-1, 1) for x in (-1, 1)]


def boxHeading(box):
  """How far a box is turned about Z, in radians."""
  return box.matrix_world.to_euler().z


def guideCorners():
  """The world corners of the boundaries' meshes and of the zone lines' boxes, which views draw as guides."""
  corners = [list(found.matrix_world @ mathutils.Vector(corner)) for found in boundaryObjects() if found.type == "MESH" for corner in found.bound_box]
  return corners + [corner for line in zoneLineObjects() for corner in boxCorners(line)]


def placeZoneLine(number, label, minimum, maximum, target):
  """A zone line: an axis-aligned box named ATP_<number>_<label>, the .zon region the client zones players through, with where it
  leads. A line placed with a number already in use replaces that line."""
  if isinstance(number, bool) or not isinstance(number, int) or number < 1:
    raise ValueError(f"number is a whole number from 1 (the client reads 0 as none), got {number!r}")
  if not isinstance(label, str) or not labelPattern.match(label):
    raise ValueError(f"label is letters, digits, and underscores, got {label!r}")
  requireCorners(minimum, maximum)
  requireTarget(target)
  name = f"ATP_{number}_{label}"
  replaced = [line for line in zoneLineObjects() if isAuthored(line) and parsedName(line.name)[0] == number]
  taken = bpy.data.objects.get(name)
  if taken is not None and taken not in replaced:
    raise ValueError(f"An object named '{name}' already exists and is not this zone's zone line {number}")
  replacedNames = [line.name for line in replaced]
  if name.lower() in {sceneObject.name.lower() for sceneObject in bpy.data.objects if sceneObject not in replaced}:
    raise ValueError(f"An object named '{name}' already exists once lowercased; .zon region names must differ whatever their case")
  for line in replaced:
    bpy.data.objects.remove(line)
  box = bpy.data.objects.new(name, None)
  box.empty_display_type = "CUBE"
  box.empty_display_size = 1.0
  box.location = [(low + high) / 2 for low, high in zip(minimum, maximum)]
  box.scale = [(high - low) / 2 for low, high in zip(minimum, maximum)]
  box[bridgeMeshAccess.zoneLineProperty] = json.dumps(target)
  bridgeObjects.targetCollection(zoneLineCollectionName).objects.link(box)
  bpy.context.view_layer.update()
  return describeZoneLine(box) | {"replaced": replacedNames}


def describeZoneLine(box):
  """A zone line's name, number, label, the bounds of its box, its turn about Z where it has one, and its target."""
  number, label = parsedName(box.name)
  corners = boxCorners(box)
  heading = boxHeading(box)
  described = {
    "name": box.name, "number": number, "label": label, "minimum": [round(min(corner[axis] for corner in corners), 2) for axis in range(3)],
    "maximum": [round(max(corner[axis] for corner in corners), 2) for axis in range(3)], "target": readSpec(box, bridgeMeshAccess.zoneLineProperty),
  }
  if abs(heading) > turnTolerance:
    described["headingDegrees"] = round(math.degrees(heading), 2)
  if not isAuthored(box):
    described["clientContent"] = box[bridgeMeshAccess.clientContentProperty]
  return described


def zoneLineErrors():
  """What no zone file can hold among the zone's own zone lines, each with its object and where it stands: a name the client cannot
  read a number from, a turned box (export writes rotation 0), a box without size, and region names that clash once lowercased, swim
  volumes included."""
  errors = []

  def add(box, message):
    errors.append({"object": box.name, "at": boxBounds(box)[0], "message": message})

  lines = [line for line in zoneLineObjects() if isAuthored(line)]
  for line in lines:
    match = zoneLinePattern.match(line.name)
    if match is None or int(match.group(1)) < 1:
      add(line, f"'{line.name}' is not named ATP_<number>_<label> with a number from 1; placeZoneLine names it")
    if any(abs(angle) > turnTolerance for angle in line.matrix_world.to_euler()):
      add(line, f"'{line.name}' is turned; zone lines stay square to the axes")
    if min(line.matrix_world.to_scale()) <= 0:
      add(line, f"'{line.name}' has a half extent of 0 or less: {[round(value, 3) for value in line.matrix_world.to_scale()]}")
  seen = {}
  for box in lines + bridgeSwim.swimBoxes():
    seen.setdefault(box.name.lower(), []).append(box)
  for boxes in seen.values():
    if len(boxes) > 1 and any(box in lines for box in boxes):
      for box in boxes:
        add(box, f".zon regions {[shared.name for shared in boxes]} share a name once lowercased")
  return errors


def zoneLineGaps():
  """What a game export needs of the zone's own zone lines: a target for each, and a number used once."""
  gaps = []
  numbers = {}
  for line in zoneLineObjects():
    if not isAuthored(line):
      continue
    if readSpec(line, bridgeMeshAccess.zoneLineProperty) is None:
      gaps.append({"gap": "zone line without a target", "zoneLine": line.name, "at": boxBounds(line)[0]})
    numbers.setdefault(parsedName(line.name)[0], []).append(line.name)
  gaps += [{"gap": "zone line number used twice", "number": number, "zoneLines": names} for number, names in sorted(numbers.items()) if len(names) > 1]
  return gaps


def zoneLineRegions():
  """The .zon regions export writes for the zone's own zone lines, from the boxes as they stand."""
  regions = []
  for line in zoneLineObjects():
    if isAuthored(line):
      center, halfExtents = boxBounds(line)
      regions.append({"name": line.name, "center": center, "halfExtents": halfExtents})
  return regions


def boundaryErrors():
  """What export cannot merge into the terrain, each with its object and where it stands: a boundary that is not a mesh, or has no
  faces."""
  errors = []
  for boundary in boundaryObjects():
    if not isAuthored(boundary):
      continue
    at = [round(float(value), 2) for value in boundary.matrix_world.translation]
    if boundary.type != "MESH":
      errors.append({"object": boundary.name, "at": at, "message": f"'{boundary.name}' is a boundary but a {boundary.type}; boundaries are meshes"})
    elif not len(boundary.data.polygons):
      errors.append({"object": boundary.name, "at": at, "message": f"Boundary '{boundary.name}' has no faces"})
  return errors


def boundaryArrays(boundary, depsgraph):
  """A boundary's evaluated triangles in world space as the terrain takes them: no material, no texture coordinates, not passable; a
  wall's twice, facing each way."""
  positions, triangles, _, _, _, _ = evaluatedTriangles(boundary, depsgraph)
  if readSpec(boundary, bridgeMeshAccess.boundaryProperty)["kind"] == "wall":
    # The client's own walls are single triangles facing the play area (Highpass Hold's, and over 99% of the vertical ones in its EQG
    # zones), and whether its collision lets a player through a wall met from behind is untraced. A wall here faces left of its path,
    # which on an open path can be away from the play area, so it goes in facing both ways and blocks from either side.
    triangles = numpy.concatenate([triangles, triangles[:, ::-1] + len(positions)])
    positions = numpy.concatenate([positions, positions])
  matrix = numpy.array(boundary.matrix_world)
  world = positions @ matrix[:3, :3].T + matrix[:3, 3]
  corners = world[triangles]
  normals = numpy.zeros_like(world)
  for corner in range(3):
    numpy.add.at(normals, triangles[:, corner], numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]))
  normals /= numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  return {
    "positions": world, "normals": normals, "uvs": numpy.zeros((len(world), 2)), "triangles": triangles,
    "materials": [None] * len(triangles), "passable": numpy.zeros(len(triangles), dtype=bool),
  }


def getBoundaries():
  """Every boundary (walls, lids, floors, and those an imported zone file brought), every object marked passable, and what export
  cannot merge."""
  bpy.context.view_layer.update()
  marked = [sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.passableProperty in sceneObject]
  return {
    "boundaries": [describeBoundary(boundary) for boundary in boundaryObjects()],
    "passable": [describePassable(sceneObject) for sceneObject in sorted(marked, key=lambda found: found.name)],
    "errors": [error["message"] for error in boundaryErrors()],
  }


def getZoneLines():
  bpy.context.view_layer.update()
  return {"zoneLines": [describeZoneLine(line) for line in zoneLineObjects()], "errors": [error["message"] for error in zoneLineErrors()], "gaps": zoneLineGaps()}


def placeImportedBoundaries(zone, walls):
  """An imported zone archive's terrain's material -1 triangles as one boundary, for reference."""
  name = f"{zone} boundaries"
  bridgeObjects.requireNewName(name)
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata(walls["positions"], [], walls["triangles"])
  mesh.update()
  boundary = bpy.data.objects.new(name, mesh)
  boundary.hide_render = True
  boundary[bridgeMeshAccess.boundaryProperty] = json.dumps({"kind": "imported"})
  boundary[bridgeMeshAccess.clientContentProperty] = "zoneFile"
  bridgeObjects.targetCollection(name).objects.link(boundary)
  bpy.context.view_layer.update()
  return {"boundary": describeBoundary(boundary)}


def placeZoneLineGuides(zoneLines, collection, clientContent):
  """An imported zone's zone lines, for reference: boxes as placeZoneLine makes them, [{name, center, halfExtents, headingDegrees}],
  turned about Z as the zone file turns them, with no target, which a zone file never holds (the server's zone points do). A name
  already taken is numbered, as Blender numbers a zone's lights."""
  if clientContent not in bridgeMeshAccess.clientContentKinds:
    raise ValueError(f"clientContent is one of {list(bridgeMeshAccess.clientContentKinds)}, got {clientContent!r}")
  destination = bridgeObjects.targetCollection(collection)
  boxes = []
  for line in zoneLines:
    if min(line["halfExtents"]) <= 0:
      raise ValueError(f"Zone line '{line['name']}' has a half extent of 0 or less: {list(line['halfExtents'])}")
    box = bpy.data.objects.new(line["name"], None)
    box.empty_display_type = "CUBE"
    box.empty_display_size = 1.0
    box.location = line["center"]
    box.rotation_euler = (0.0, 0.0, math.radians(line["headingDegrees"]))
    box.scale = line["halfExtents"]
    box[bridgeMeshAccess.zoneLineProperty] = json.dumps(None)
    box[bridgeMeshAccess.clientContentProperty] = clientContent
    destination.objects.link(box)
    boxes.append(box)
  bpy.context.view_layer.update()
  return {"zoneLines": [describeZoneLine(box) for box in boxes]}


commands = {
  "placeBoundaryWall": (placeBoundaryWall, True),
  "placeBoundaryPlane": (placeBoundaryPlane, True),
  "markPassable": (markPassable, True),
  "getBoundaries": (getBoundaries, False),
  "placeZoneLine": (placeZoneLine, True),
  "getZoneLines": (getZoneLines, False),
  "placeImportedBoundaries": (placeImportedBoundaries, True),
  "placeZoneLineGuides": (placeZoneLineGuides, True),
}
