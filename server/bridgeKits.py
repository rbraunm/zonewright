"""Kit pieces as the client's kits are made and placed: modeled to exact size (or marked by hand), openings cut through walls, roofs
over footprints, and pieces placed and snapped end to end by their sockets as linked instances sharing one model. Runs under Blender's
Python."""
import math
import numbers

import bmesh
import bpy
import mathutils
import numpy

import bridgeArrangement
import bridgeExport
import bridgeKitData
import bridgeKitGeometry
import bridgeMeshAccess
import bridgeObjects
import bridgeShaping
import bridgeStructureData
import bridgeSurfacing
from playerScale import stepHeight

openingKinds = ("door", "window")
openingPieceKinds = ("wall", "custom")
frameKeys = {"width", "depth", "material", "worldUnitsPerRepeat"}
snapKeys = {"object", "socket", "pieceSocket"}
# An opening leaves at least this much wall at each end and above it (and below a window).
openingMargin = 1.0
# How far an opening's prism runs past the piece, so its cut faces never lie on the piece's own.
throughMargin = 1.0
sourceAttribute = "zonewrightOpeningSource"
capAttribute = "zonewrightOpeningCap"
frameSource, openingSource = 1, 2
pitchRange = (5.0, 60.0)
# Placements a roof goes over stand turned alike within this many degrees.
frameTolerance = 0.01
ridgeDirections = ("x", "y")
defaultPlacementCollection = "structures"


def isNumber(value):
  return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value)


def requirePoint(label, point, sizes=(3,)):
  if not isinstance(point, (list, tuple)) or len(point) not in sizes or not all(isNumber(value) for value in point):
    shapes = " or ".join("[" + ", ".join("xyz"[:size]) + "]" for size in sizes)
    raise ValueError(f"{label} is {shapes}, got {point!r}")
  return [float(value) for value in point]


def requirePositive(label, value):
  if not isNumber(value) or value <= 0:
    raise ValueError(f"{label} must be a positive number, got {value!r}")
  return float(value)


def requireNewPieceName(name):
  bridgeKitData.requireStemmed(name, "A kit piece's name")
  for kind, found in (("an object", bpy.data.objects.get(name)), ("a collection", bpy.data.collections.get(name)), ("a mesh", bpy.data.meshes.get(name))):
    if found is not None:
      raise ValueError(f"'{name}' is already the name of {kind} in this file")


def isMadeMaterial(material):
  """A material createMaterial made (diffuse, optional normal, optional cutout), the only kind a kit piece takes."""
  return material is not None and bridgeSurfacing.cutoutPropertyName in material and bridgeSurfacing.liquidPropertyName not in material


def requireMaterial(name):
  material = next((found for found in bpy.data.materials if found.name == name and found.library is None), None)
  if not isMadeMaterial(material):
    raise ValueError(f"'{name}' is not a material createMaterial made in this file")
  return material


def requireRoles(kind, roles, materials, repeats):
  """Each of a kind's roles with its material and repeat, given exactly."""
  for label, given in (("materials", materials), ("worldUnitsPerRepeat", repeats)):
    if not isinstance(given, dict) or set(given) != set(roles):
      got = sorted(given) if isinstance(given, dict) else given
      raise ValueError(f"A {kind} takes {label} for exactly its roles {list(roles)}; got {got!r}")
  roleMaterials = {role: requireMaterial(materials[role]) for role in roles}
  roleRepeats = {role: requirePositive(f"worldUnitsPerRepeat {role}", repeats[role]) for role in roles}
  return roleMaterials, roleRepeats


def makePiece(name, kind, shape, roleMaterials, roleRepeats, location, passable):
  """A collection holding one mesh object built from a shape, its base at location (its instance_offset), marked as an asset, its
  record written."""
  mesh = bridgeKitGeometry.buildMesh(name, shape, roleMaterials, roleRepeats)
  meshObject = bpy.data.objects.new(name, mesh)
  meshObject.location = location
  collection = bpy.data.collections.new(name)
  bpy.context.scene.collection.children.link(collection)
  collection.objects.link(meshObject)
  collection.instance_offset = location
  collection.asset_mark()
  passable = bridgeKitData.kinds[kind]["passable"] if passable is None else passable
  if passable:
    meshObject[bridgeMeshAccess.passableProperty] = True
  positions = numpy.array(shape.positions)
  bridgeKitData.writeRecord(collection, {
    "kind": kind, "passable": passable, "sockets": bridgeKitData.defaultSockets(kind, positions.min(0), positions.max(0)), "openings": [],
  })
  bpy.context.view_layer.update()
  return collection, mesh


def roleSummary(mesh, shape, roleMaterials):
  measured = bridgeKitGeometry.roleRepeats(mesh, shape.roles)
  return {role: {"material": roleMaterials[role].name, "worldUnitsPerRepeat": measured[role]} for role in measured}


def withRoles(described, shape, roleMaterials):
  for seam in described["seams"]:
    seam["roles"] = [role for role in dict.fromkeys(shape.roles) if roleMaterials[role].name_full == seam["material"]]
  return described


def requirePassable(passable):
  if passable is not None and not isinstance(passable, bool):
    raise ValueError(f"passable is true, false, or null (the kind's), got {passable!r}")


def createKitPiece(name, kind, size, materials, worldUnitsPerRepeat, location, passable):
  requireNewPieceName(name)
  if kind not in bridgeKitData.modeledKinds:
    raise ValueError(f"kind must be one of {list(bridgeKitData.modeledKinds)}, got {kind!r}; a roof comes from addRoof, and any other shape is modeled and marked (markKitPiece, kind custom)")
  if not isinstance(size, list) or len(size) != 3 or not all(isNumber(value) for value in size):
    raise ValueError(f"size is [length, depth, height], three numbers, got {size!r}")
  if kind == "ropeRail" and (size[1] != 0 or size[0] <= 0 or size[2] <= 0):
    raise ValueError(f"A ropeRail is a ribbon: size [length, 0, height] with its depth exactly 0 and the others positive, got {size!r}")
  if kind != "ropeRail" and min(size) <= 0:
    raise ValueError(f"size [length, depth, height] must be three positive numbers, got {size!r}")
  roleMaterials, roleRepeats = requireRoles(kind, bridgeKitData.kinds[kind]["roles"], materials, worldUnitsPerRepeat)
  if kind == "ropeRail" and not roleMaterials["rope"][bridgeSurfacing.cutoutPropertyName]:
    raise ValueError(f"A ropeRail's rope material must be cutout (createMaterial cutout), as the client's alpha-tested rope rails are; '{materials['rope']}' is opaque")
  location = requirePoint("location", location)
  requirePassable(passable)
  shape = bridgeKitGeometry.pieceShape(kind, [float(value) for value in size])
  collection, mesh = makePiece(name, kind, shape, roleMaterials, roleRepeats, location, passable)
  return withRoles(bridgeKitGeometry.describePiece(collection), shape, roleMaterials) | {"roles": roleSummary(mesh, shape, roleMaterials)}


def prefabOwner(collection):
  """The prefab a collection is or belongs to, or None."""
  if bridgeKitData.prefabProperty in collection:
    return collection
  return next((parent for parent in bpy.data.collections if bridgeKitData.prefabProperty in parent and collection.name in parent.children), None)


def requireSockets(sockets, low, high):
  if not isinstance(sockets, list):
    raise ValueError(f"sockets is a list of {{name, at, direction}}, got {sockets!r}")
  checked, names = [], set()
  for socket in sockets:
    if not isinstance(socket, dict) or set(socket) != {"name", "at", "direction"} or not isinstance(socket["name"], str) or not socket["name"]:
      raise ValueError(f"A socket is {{name, at [x, y, z], direction [x, y, z]}} in the piece frame, got {socket!r}")
    name = socket["name"]
    if name in names:
      raise ValueError(f"Socket name '{name}' is given twice; each socket of a piece has its own name")
    names.add(name)
    at = numpy.array(requirePoint(f"socket '{name}' at", socket["at"]))
    direction = numpy.array(requirePoint(f"socket '{name}' direction", socket["direction"]))
    if abs(numpy.linalg.norm(direction) - 1) > bridgeKitData.directionTolerance:
      raise ValueError(f"Socket '{name}''s direction {direction.tolist()} is not of unit length")
    if abs(direction[2]) > bridgeKitData.directionTolerance and numpy.abs(direction[:2]).max() > bridgeKitData.directionTolerance:
      raise ValueError(f"Socket '{name}''s direction {direction.tolist()} is neither level nor straight up or down; pieces stand upright and turn only about the vertical")
    outside = numpy.maximum(low - at, at - high).max()
    if outside > bridgeKitData.socketReach:
      raise ValueError(f"Socket '{name}' at {at.tolist()} lies {outside:.2f} outside the piece's bounds {bridgeKitData.roundVector(low)} to {bridgeKitData.roundVector(high)}; it may lie at most {bridgeKitData.socketReach:g} outside")
    checked.append({"name": name, "at": [bridgeKitData.plain(value) for value in at], "direction": [bridgeKitData.plain(value) for value in direction]})
  return checked


def requireStraight(kind, sockets):
  byName = {socket["name"]: socket for socket in sockets}
  if "start" not in byName or "end" not in byName:
    raise ValueError(f"A {kind} meets the next one end to end, so it needs sockets 'start' and 'end'; it has {sorted(byName)}")
  if numpy.dot(byName["start"]["direction"], byName["end"]["direction"]) >= bridgeKitData.jointFacing:
    raise ValueError(f"A {kind}'s start and end must face apart, as a straight piece's do; they face {byName['start']['direction']} and {byName['end']['direction']} (a corner is a custom piece)")


def unmappableFaces(member):
  """Faces whose texture coordinates are not an affine map of position within 1 percent of their extent: a span that stretches or
  bends the piece cannot carry their texture."""
  mesh = member.data
  totals, loopVertices = bridgeMeshAccess.faceLoops(member)
  positions, _ = bridgeMeshAccess.readVertexArrays(member)
  uvs = numpy.empty(len(mesh.loops) * 2)
  mesh.uv_layers.active.data.foreach_get("uv", uvs)
  uvs = uvs.reshape(-1, 2)
  starts = numpy.cumsum(totals) - totals
  count = 0
  for start, total in zip(starts, totals):
    if total <= 3:
      continue
    corners = positions[loopVertices[start:start + total]]
    coordinates = uvs[start:start + total]
    design = numpy.column_stack([corners - corners.mean(0), numpy.ones(total)])
    fit, _, _, _ = numpy.linalg.lstsq(design, coordinates, rcond=None)
    extent = numpy.ptp(coordinates, axis=0).max()
    if extent > 0 and numpy.abs(design @ fit - coordinates).max() > 0.01 * extent:
      count += 1
  return count


def markKitPiece(collectionName, kind, sockets, origin, passable):
  collection = next((found for found in bpy.data.collections if found.name == collectionName and found.library is None), None)
  if collection is None:
    raise ValueError(f"No collection named '{collectionName}' in this file")
  bridgeKitData.requireStemmed(collectionName, "A kit piece's name")
  if prefabOwner(collection) is not None:
    raise ValueError(f"'{collectionName}' is a prefab or a prefab's part, not a piece")
  if kind not in bridgeKitData.kinds:
    raise ValueError(f"kind must be one of {list(bridgeKitData.kinds)}, got {kind!r}")
  members = sorted(collection.all_objects, key=lambda member: member.name)
  others = [member.name for member in members if member.type != "MESH"]
  if others:
    raise ValueError(f"'{collectionName}' holds {others}, which are not meshes; a kit piece is meshes (a building of placed pieces is a prefab)")
  if not members:
    raise ValueError(f"'{collectionName}' holds no mesh")
  for member in members:
    slots = [slot.material for slot in member.material_slots]
    indices = numpy.empty(len(member.data.polygons), dtype=numpy.int64)
    member.data.polygons.foreach_get("material_index", indices)
    bare = int(sum(1 for index in indices if index >= len(slots) or not isMadeMaterial(slots[index])))
    if bare:
      raise ValueError(f"Mesh '{member.name}' has {bare} faces without a material createMaterial made (assignMaterial)")
    if not member.data.uv_layers:
      raise ValueError(f"Mesh '{member.name}' has no texture coordinates on its {len(member.data.polygons)} faces (projectUVs)")
    if kind == "ropeRail" and any(not slots[index][bridgeSurfacing.cutoutPropertyName] for index in indices):
      raise ValueError(f"A ropeRail's faces must all be cutout; mesh '{member.name}' has opaque faces")
  requirePassable(passable)
  corners = numpy.array([list(corner) for member in members for corner in bridgeMeshAccess.worldBoundsCorners(member, bpy.context.evaluated_depsgraph_get())])
  low, high = corners.min(0), corners.max(0)
  origin = numpy.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]]) if origin is None else numpy.array(requirePoint("origin", origin))
  low, high = low - origin, high - origin
  sockets = bridgeKitData.defaultSockets(kind, low, high) if sockets is None else requireSockets(sockets, low, high)
  if kind in bridgeKitData.straightKinds:
    requireStraight(kind, sockets)
  existing = bridgeKitData.readRecord(collection)
  if passable is None:
    passable = existing["passable"] if existing is not None and existing["kind"] == kind else bridgeKitData.kinds[kind]["passable"]
  collection.instance_offset = origin.tolist()
  if collection.asset_data is None:
    collection.asset_mark()
  for member in members:
    if passable:
      member[bridgeMeshAccess.passableProperty] = True
    elif bridgeMeshAccess.passableProperty in member:
      del member[bridgeMeshAccess.passableProperty]
  bridgeKitData.writeRecord(collection, {"kind": kind, "passable": passable, "sockets": sockets, "openings": existing["openings"] if existing is not None else []})
  bpy.context.view_layer.update()
  return bridgeKitGeometry.describePiece(collection) | {"unmappable": [{"mesh": member.name, "faces": faces} for member in members if (faces := unmappableFaces(member))]}


def openingOutline(kind, along, width, height, sill, archRise, archSegments, base):
  """An opening's outline [x, z] in the piece frame, counterclockwise seen from the front: a door's runs on below the base."""
  half = width / 2
  bottom = base + sill if kind == "window" else base - throughMargin
  jambTop = base + sill + height
  points = [(along - half, bottom), (along + half, bottom), (along + half, jambTop)]
  if archRise is not None:
    radius = (half * half + archRise * archRise) / (2 * archRise)
    centerZ = jambTop + archRise - radius
    start = math.atan2(jambTop - centerZ, half)
    for step in range(1, archSegments):
      angle = start + (math.pi - 2 * start) * step / archSegments
      points.append((along + radius * math.cos(angle), centerZ + radius * math.sin(angle)))
  points.append((along - half, jambTop))
  return numpy.array(points)


def offsetOutline(outline, distance):
  """A convex counterclockwise outline with each side moved out by distance, its corners mitered."""
  sides = numpy.roll(outline, -1, axis=0) - outline
  normals = numpy.stack([sides[:, 1], -sides[:, 0]], axis=1) / numpy.linalg.norm(sides, axis=1)[:, None]
  return numpy.array([outline[index] + numpy.linalg.solve(numpy.array([normals[index - 1], normals[index]]), [distance, distance]) for index in range(len(outline))])


def frameOutline(kind, outline, frameWidth, base):
  framed = offsetOutline(outline, frameWidth)
  if kind == "door":
    framed[:2, 1] = base
  return framed


def openingExtent(opening, base):
  """An opening with its frame as [left, right, bottom, top] in the piece frame."""
  outline = openingOutline(opening["kind"], opening["along"], opening["width"], opening["height"], opening["sill"], opening["archRise"], opening["archSegments"], base)
  if opening["frame"] is not None:
    outline = frameOutline(opening["kind"], outline, opening["frame"]["width"], base)
  bottom = base if opening["kind"] == "door" else outline[:, 1].min()
  return outline[:, 0].min(), outline[:, 0].max(), bottom, outline[:, 1].max()


def requireOpening(kind, width, height, sill, archRise, archSegments, frame):
  if kind not in openingKinds:
    raise ValueError(f"kind must be one of {list(openingKinds)}, got {kind!r}")
  if kind == "door" and sill != 0:
    raise ValueError(f"A door's sill is 0 (it opens from the base); got {sill!r}; an opening above the base is a window")
  if kind == "window" and not (isNumber(sill) and sill > 0):
    raise ValueError(f"A window stands on a sill above the base; sill must be positive, got {sill!r}")
  requirePositive("width", width)
  requirePositive("height", height)
  if not isinstance(archSegments, int) or isinstance(archSegments, bool) or archSegments < 2:
    raise ValueError(f"archSegments must be a whole number of at least 2, got {archSegments!r}")
  if archRise is not None and not (isNumber(archRise) and 0 < archRise <= width / 2):
    raise ValueError(f"archRise must be above 0 and at most half the width ({width / 2:g}, a semicircle), got {archRise!r}")
  if frame is not None:
    if not isinstance(frame, dict) or set(frame) != frameKeys:
      raise ValueError(f"frame is {{width, depth, material, worldUnitsPerRepeat}}, got {frame!r}")
    requirePositive("frame width", frame["width"])
    requirePositive("frame depth", frame["depth"])
    requirePositive("frame worldUnitsPerRepeat", frame["worldUnitsPerRepeat"])
    requireMaterial(frame["material"])


def prismObject(name, outline, yLow, yHigh, offset, source):
  """A closed prism through the piece along Y over an [x, z] outline, in the file's coordinates, its faces tagged with their source."""
  meshEditor = bmesh.new()
  front = [meshEditor.verts.new((x + offset[0], yHigh + offset[1], z + offset[2])) for x, z in outline]
  back = [meshEditor.verts.new((x + offset[0], yLow + offset[1], z + offset[2])) for x, z in outline]
  meshEditor.faces.new(front)
  meshEditor.faces.new(list(reversed(back)))
  for index in range(len(outline)):
    following = (index + 1) % len(outline)
    meshEditor.faces.new([front[index], back[index], back[following], front[following]])
  bmesh.ops.recalc_face_normals(meshEditor, faces=list(meshEditor.faces))
  mesh = bpy.data.meshes.new(name)
  meshEditor.to_mesh(mesh)
  meshEditor.free()
  mesh.attributes.new(sourceAttribute, "INT", "FACE").data.foreach_set("value", numpy.full(len(mesh.polygons), source, dtype=numpy.int32))
  prism = bpy.data.objects.new(name, mesh)
  bpy.context.scene.collection.objects.link(prism)
  return prism


def applyBoolean(target, cutter, operation):
  modifier = target.modifiers.new("zonewrightOpening", "BOOLEAN")
  modifier.object = cutter
  modifier.operation = operation
  modifier.solver = "EXACT"
  bpy.context.view_layer.update()
  bridgeShaping.applyModifier(target, modifier)


def faceValues(mesh, name):
  values = numpy.zeros(len(mesh.polygons), dtype=numpy.int32)
  attribute = mesh.attributes.get(name)
  if attribute is not None:
    attribute.data.foreach_get("value", values)
  return values


def capOpenings(member):
  """Close the piece's open edges (a wall has no bottom) so a boolean has a solid to cut, tagging the faces that close it."""
  meshEditor = bridgeMeshAccess.loadBMesh(member)
  layer = meshEditor.faces.layers.int.new(capAttribute)
  sourceLayer = meshEditor.faces.layers.int.new(sourceAttribute)
  boundary = [edge for edge in meshEditor.edges if edge.is_boundary]
  made = bmesh.ops.holes_fill(meshEditor, edges=boundary, sides=0)["faces"]
  for face in meshEditor.faces:
    face[layer] = 1 if face in made else 0
    face[sourceLayer] = 0
  bridgeMeshAccess.storeBMesh(meshEditor, member)


def cutOpening(piece, kind, along, width, height, sill, archRise, archSegments, frame):
  collection = bridgeKitData.requireLocalPiece(piece)
  record = bridgeKitData.readRecord(collection)
  if record["kind"] not in openingPieceKinds:
    raise ValueError(f"'{piece}' is a {record['kind']}; openings are cut through wall pieces and custom pieces (a curved wall section)")
  members = bridgeKitData.pieceMembers(collection)
  if len(members) != 1:
    raise ValueError(f"Piece '{piece}' is {len(members)} meshes; an opening is cut through a wall of one mesh (joinObjects joins them)")
  member = members[0]
  if not isNumber(along):
    raise ValueError(f"along is a number (from the wall's middle along X), got {along!r}")
  requireOpening(kind, width, height, sill, archRise, archSegments, frame)
  bridgeMeshAccess.requireNoShapingPasses(member, "cut an opening")
  bridgeShaping.requireNoModifiers(member)
  offset = numpy.array(collection.instance_offset)
  low, high = bridgeKitGeometry.pieceBounds(collection)
  base = low[2]
  opening = {
    "kind": kind, "along": float(along), "width": float(width), "height": float(height), "sill": float(sill),
    "archRise": None if archRise is None else float(archRise), "archSegments": archSegments,
    "frame": None if frame is None else {"width": float(frame["width"]), "depth": float(frame["depth"]), "material": frame["material"], "worldUnitsPerRepeat": float(frame["worldUnitsPerRepeat"])},
  }
  left, right, bottom, top = openingExtent(opening, base)
  what = f"The {kind}" + (" with its frame" if frame is not None else "")
  leftovers = [("at its -X end", "its -X end", left - low[0]), ("at its +X end", "its +X end", high[0] - right), ("above it", "its top", high[2] - top)]
  if kind == "window":
    leftovers.append(("below it", "its base", bottom - base))
  for where, edge, leftover in leftovers:
    if leftover < openingMargin - 1e-9:
      found = f"reaches {-leftover:.2f} past the wall's {edge.split(' ', 1)[1]}" if leftover < 0 else f"leaves only {leftover:.2f} of wall {where}"
      raise ValueError(f"{what} {found}; it needs at least {openingMargin:g} of wall {where}, so it is {openingMargin - leftover:.2f} too wide, tall, or near the edge")
  for index, earlier in enumerate(record["openings"]):
    earlierLeft, earlierRight, earlierBottom, earlierTop = openingExtent(earlier, base)
    if left < earlierRight and earlierLeft < right and bottom < earlierTop and earlierBottom < top:
      raise ValueError(f"{what} ({left:.2f} to {right:.2f} along, {bottom:.2f} to {top:.2f} up) overlaps opening {index} (a {earlier['kind']} {earlierLeft:.2f} to {earlierRight:.2f} along, {earlierBottom:.2f} to {earlierTop:.2f} up)")
  frameMaterial = None if frame is None else requireMaterial(frame["material"])
  outline = openingOutline(kind, along, width, height, sill, archRise, archSegments, base)
  depth = 0.0 if frame is None else float(frame["depth"])
  trianglesBefore = bridgeMeshAccess.triangleCount(member)
  densities = bridgeShaping.materialDensities(member)
  slotCount = len(member.data.materials)
  originalSlots = numpy.empty(len(member.data.polygons), dtype=numpy.int32)
  member.data.polygons.foreach_get("material_index", originalSlots)
  cutters = []
  original = member.data.copy()
  try:
    openingPrism = prismObject(f"{piece}OpeningCutter", outline, low[1] - depth - throughMargin, high[1] + depth + throughMargin, offset, openingSource)
    cutters.append(openingPrism)
    crossed = sorted({first for first, _ in bridgeShaping.worldFaceTree(member, range(len(member.data.polygons))).overlap(bridgeShaping.worldFaceTree(openingPrism, range(len(openingPrism.data.polygons))))})
    if not crossed:
      raise ValueError(f"The {kind} at {along:g} along misses the faces of '{piece}'")
    crossedTree = bridgeShaping.worldFaceTree(member, crossed)
    # A frame stands proud of the faces the opening goes through, not of the piece's bounds, which earlier frames have widened.
    crossedPoints = numpy.array([list(member.matrix_world @ member.data.vertices[vertex].co) for index in crossed for vertex in member.data.polygons[index].vertices]) - offset
    faceLow, faceHigh = float(crossedPoints[:, 1].min()), float(crossedPoints[:, 1].max())
    capOpenings(member)
    if frame is not None:
      framePrism = prismObject(f"{piece}FrameCutter", frameOutline(kind, outline, frame["width"], base), faceLow - depth, faceHigh + depth, offset, frameSource)
      cutters.append(framePrism)
      applyBoolean(member, framePrism, "UNION")
    applyBoolean(member, openingPrism, "DIFFERENCE")
  except Exception:
    cut, name = member.data, member.data.name
    member.data = original
    bpy.data.meshes.remove(cut)
    original.name = name
    raise
  else:
    bpy.data.meshes.remove(original)
  finally:
    for cutter in cutters:
      cutterMesh = cutter.data
      bpy.data.objects.remove(cutter)
      bpy.data.meshes.remove(cutterMesh)
  mesh = member.data
  while len(mesh.materials) > slotCount:
    mesh.materials.pop()
  meshEditor = bridgeMeshAccess.loadBMesh(member)
  capLayer = meshEditor.faces.layers.int.get(capAttribute)
  sourceLayer = meshEditor.faces.layers.int.get(sourceAttribute)
  meshEditor.normal_update()
  matrix = member.matrix_world
  turn = matrix.to_3x3()

  def onBase(face):
    return (turn @ face.normal).normalized().z < -0.999 and abs((matrix @ face.calc_center_median()).z - offset[2] - base) < 1e-5

  underneath = [face for face in meshEditor.faces if face[capLayer] or (face[sourceLayer] == frameSource and onBase(face))]
  bmesh.ops.delete(meshEditor, geom=underneath, context="FACES")
  if frameMaterial is not None and frameMaterial.name not in [material.name for material in mesh.materials if material is not None]:
    mesh.materials.append(frameMaterial)
  frameSlot = None if frameMaterial is None else next(index for index, material in enumerate(mesh.materials) if material == frameMaterial)
  uvLayer = meshEditor.loops.layers.uv.active
  revealPoints, made = [], 0
  for face in meshEditor.faces:
    source = face[sourceLayer]
    if source == 0:
      continue
    made += 1
    points = numpy.array([list(matrix @ loop.vert.co) for loop in face.loops]) - offset
    if source == openingSource:
      revealPoints.extend(points.tolist())
    if frameMaterial is not None:
      face.material_index, repeat = frameSlot, frame["worldUnitsPerRepeat"]
    else:
      center = matrix @ face.calc_center_median()
      face.material_index = int(originalSlots[crossed[crossedTree.find_nearest(center)[2]]])
      repeat = densities[face.material_index]
    for loop, uv in zip(face.loops, bridgeKitGeometry.mapFace(points, "box", low, repeat)):
      loop[uvLayer].uv = (float(uv[0]), float(uv[1]))
  bridgeMeshAccess.storeBMesh(meshEditor, member)
  for name in (capAttribute, sourceAttribute):
    mesh.attributes.remove(mesh.attributes[name])
  record["openings"].append(opening)
  bridgeKitData.writeRecord(collection, record)
  bpy.context.view_layer.update()
  reveal = numpy.array(revealPoints)
  clearBottom = base if kind == "door" else reveal[:, 2].min()
  shown = outline.copy()
  if kind == "door":
    shown[:2, 1] = base
  return {
    "piece": piece, "opening": opening, "corners": [[round(float(x), 4), round(float(z), 4)] for x, z in shown],
    "clearWidth": round(float(numpy.ptp(reveal[:, 0])), 4), "clearHeight": round(float(reveal[:, 2].max() - clearBottom), 4),
    "madeFaces": made, "triangles": {"before": trianglesBefore, "after": bridgeMeshAccess.triangleCount(member)},
  }


def bodyCorners(sceneObject, depsgraph):
  """An object's box in the world: a placed piece's own body (a wall's faces, not the frames standing proud of them), else its bounds."""
  if not bridgeKitData.isPlacedPiece(sceneObject):
    return numpy.array([list(corner) for corner in bridgeMeshAccess.worldBoundsCorners(sceneObject, depsgraph)])
  collection = sceneObject.instance_collection
  low, high = bridgeKitGeometry.pieceBounds(collection)
  proud = bridgeKitData.frameProud(bridgeKitData.readRecord(collection))
  low, high = low + [0.0, proud, 0.0], high - [0.0, proud, 0.0]
  box = numpy.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
  matrix = bridgeMeshAccess.matrixArray(sceneObject.matrix_world)
  return box @ matrix[:3, :3].T + matrix[:3, 3]


def overFrame(names):
  """What a roof goes over, measured square to the named meshes or placements of this file: they stand turned alike (or a quarter turn
  apart), and their bodies are measured along that turn, so a roof placed at the returned facing sits on them. Returns the facing, the
  plan size [along X, along Y] of that frame, the world point under its middle at the top's height."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  objects = []
  for name in names:
    sceneObject = bridgeMeshAccess.requireObject(name)
    if sceneObject.type != "MESH" and not bridgeMeshAccess.isCollectionInstance(sceneObject):
      raise ValueError(f"'{name}' is a {sceneObject.type}; a roof goes over meshes and placed pieces")
    xAxis = sceneObject.matrix_world.col[0].xyz
    objects.append((sceneObject, (math.degrees(math.atan2(xAxis.y, xAxis.x)) + 45.0) % 90.0 - 45.0))
  first, turn = objects[0]
  for sceneObject, other in objects[1:]:
    apart = (other - turn + 45.0) % 90.0 - 45.0
    if abs(apart) > frameTolerance:
      raise ValueError(
        f"'{sceneObject.name}' stands turned {apart:+.2f} degrees against '{first.name}' (beyond a quarter turn); a roof goes over pieces turned alike,"
        " or a quarter turn apart, and is measured square to them"
      )
  radians = math.radians(turn)
  along, across = numpy.array([math.cos(radians), math.sin(radians)]), numpy.array([-math.sin(radians), math.cos(radians)])
  corners = numpy.vstack([bodyCorners(sceneObject, depsgraph) for sceneObject, _ in objects])
  measured = numpy.column_stack([corners[:, :2] @ along, corners[:, :2] @ across])
  low, high = measured.min(0), measured.max(0)
  middle = along * (low[0] + high[0]) / 2 + across * (low[1] + high[1]) / 2
  facing = (-turn) % 360.0
  # Placements' single-precision turns leave their measure a few millionths off whole; a footprint is held to four places.
  footprint = bridgeKitData.roundVector(high - low)
  return 0.0 if abs(facing - 360.0) < 1e-9 else facing, footprint, [float(middle[0]), float(middle[1]), float(corners[:, 2].max())]


def addRoof(name, kind, pitchDegrees, overhang, thickness, materials, worldUnitsPerRepeat, location, footprint, over, ridgeAlong):
  requireNewPieceName(name)
  if kind not in bridgeKitGeometry.roofKinds:
    raise ValueError(f"kind must be one of {list(bridgeKitGeometry.roofKinds)}, got {kind!r}")
  roleMaterials, roleRepeats = requireRoles(kind, bridgeKitGeometry.roofRoles[kind], materials, worldUnitsPerRepeat)
  if not isNumber(pitchDegrees) or not pitchRange[0] <= pitchDegrees <= pitchRange[1]:
    raise ValueError(f"pitchDegrees runs from {pitchRange[0]:g} to {pitchRange[1]:g}, got {pitchDegrees!r}")
  if not isNumber(overhang) or overhang < 0:
    raise ValueError(f"overhang must be 0 or more, got {overhang!r}")
  requirePositive("thickness", thickness)
  if ridgeAlong not in ridgeDirections:
    raise ValueError(f"ridgeAlong is \"x\" or \"y\", got {ridgeAlong!r}")
  if kind == "shed" and ridgeAlong != "x":
    raise ValueError("A shed rises toward the back (-Y), its high edge along X, so ridgeAlong is \"x\"; turn the roof where it is placed")
  location = requirePoint("location", location)
  if (footprint is None) == (over is None):
    raise ValueError("Give footprint [length X, depth Y] or over [names of meshes or placed pieces], one of them")
  plateOver = facingOver = None
  if over is not None:
    if not isinstance(over, list) or not over:
      raise ValueError(f"over names meshes or placed pieces of this file, got {over!r}")
    facingOver, footprint, plate = overFrame(over)
    plateOver = bridgeKitData.roundVector(plate)
  elif not isinstance(footprint, list) or len(footprint) != 2 or not all(isNumber(value) and value > 0 for value in footprint):
    raise ValueError(f"footprint is [length X, depth Y], two positive numbers, got {footprint!r}")
  if kind == "hip" and footprint[0 if ridgeAlong == "x" else 1] < footprint[1 if ridgeAlong == "x" else 0]:
    raise ValueError(f"A hip roof's ridge runs along the longer side; footprint {footprint} is longer along {'y' if ridgeAlong == 'x' else 'x'}, so ridgeAlong is \"{'y' if ridgeAlong == 'x' else 'x'}\"")
  shape, ridgeHeight, eaveHeight = bridgeKitGeometry.roofShape(kind, [float(value) for value in footprint], float(pitchDegrees), float(overhang), float(thickness), ridgeAlong)
  collection, mesh = makePiece(name, "roof", shape, roleMaterials, roleRepeats, location, False)
  described = bridgeKitGeometry.describePiece(collection) | {
    "roof": kind, "footprint": bridgeKitData.roundVector(footprint), "ridgeHeight": round(ridgeHeight, 4), "eaveHeight": round(eaveHeight, 4),
    "roles": roleSummary(mesh, shape, roleMaterials),
  }
  return described | ({"plateOver": plateOver, "facingOver": round(facingOver, 4)} if plateOver is not None else {})


def requireSnap(snapTo):
  if not isinstance(snapTo, dict) or set(snapTo) != snapKeys or not all(isinstance(value, str) for value in snapTo.values()):
    raise ValueError(f"snapTo is {{object, socket, pieceSocket}}: a placed piece, its socket, and the new piece's socket to meet it; got {snapTo!r}")


def socketNamed(sockets, name, owner):
  found = next((socket for socket in sockets if socket[0] == name), None)
  if found is None:
    raise ValueError(f"{owner} has no socket '{name}'; its sockets: {[socket[0] for socket in sockets]}")
  return found


def placeKitPiece(name, kitPath, piece, location, facingDegrees, snapTo, depth, collection):
  if collection == bridgeExport.terrainCollectionName:
    raise ValueError(f"A placed piece is not ground; place it in another collection than '{bridgeExport.terrainCollectionName}' (by default '{defaultPlacementCollection}')")
  bridgeObjects.requireNewName(name)
  if (location is None) == (snapTo is None):
    raise ValueError("Give location (with facingDegrees) or snapTo, one of them")
  if location is not None:
    location = requirePoint("location", location, (2, 3))
    if facingDegrees is None:
      raise ValueError("A piece placed at a location needs facingDegrees: the way its front faces, 0 = +Y, clockwise")
  if facingDegrees is not None and not isNumber(facingDegrees):
    raise ValueError(f"facingDegrees is a number, got {facingDegrees!r}")
  if not isNumber(depth):
    raise ValueError(f"depth is a number, got {depth!r}")
  settling = location is not None and len(location) == 2
  if depth != 0 and not settling:
    raise ValueError("depth sinks a settled piece; it applies only to a location given as [x, y], which settles onto the ground")
  if snapTo is not None:
    requireSnap(snapTo)
  with bridgeKitData.linkingUndone():
    pieceCollection = bridgeKitData.requirePiece(kitPath, piece)
    record = bridgeKitData.readRecord(pieceCollection)
    if snapTo is not None:
      target = bridgeMeshAccess.requireObject(snapTo["object"])
      if not bridgeKitData.isPlacedPiece(target):
        raise ValueError(f"'{snapTo['object']}' is not a placed kit piece; pieces snap to placed pieces' sockets")
      bridgeKitData.requireUpright(target, "snapping to it as it stands")
      _, targetAt, targetDirection = socketNamed(bridgeKitData.worldSockets(target), snapTo["socket"], f"'{target.name}'")
      partner = bridgeKitData.partnerOf(target, targetAt, targetDirection, bridgeKitData.placedPieces())
      if partner is not None:
        raise ValueError(f"'{target.name}''s socket '{snapTo['socket']}' is already joined to '{partner['object']}''s '{partner['socket']}'")
      socket = next((found for found in record["sockets"] if found["name"] == snapTo["pieceSocket"]), None)
      if socket is None:
        raise ValueError(f"Piece '{piece}' has no socket '{snapTo['pieceSocket']}'; its sockets: {[found['name'] for found in record['sockets']]}")
      pieceDirection = mathutils.Vector(socket["direction"])
      pieceLevel, targetLevel = bridgeKitData.isLevel(pieceDirection), bridgeKitData.isLevel(targetDirection)
      if pieceLevel != targetLevel:
        raise ValueError(f"Socket '{socket['name']}' faces {'level' if pieceLevel else 'up or down'} and '{snapTo['socket']}' {'level' if targetLevel else 'up or down'}: a level socket meets only a level one")
      if not pieceLevel and pieceDirection.z * targetDirection.z > 0:
        raise ValueError(f"Sockets '{socket['name']}' and '{snapTo['socket']}' both face {'up' if pieceDirection.z > 0 else 'down'}; a vertical pair meets down onto up")
      if pieceLevel and facingDegrees is not None:
        raise ValueError("A level snap turns the piece to meet the socket, so it takes no facingDegrees")
      placedAt, facingDegrees = bridgeKitGeometry.snappedPlacement(targetAt, targetDirection, socket, bridgeKitData.facingOf(target) if facingDegrees is None else facingDegrees)
      location = list(placedAt)
    elif settling:
      found = bridgeMeshAccess.overGroundOn(bridgeMeshAccess.PlayerSurfaces(), location[0], location[1], bridgeMeshAccess.sceneTopHeight() + bridgeArrangement.settleLift)
      if found is not None:
        raise ValueError(f"At [{location[0]:g}, {location[1]:g}] {bridgeMeshAccess.describeRockOverGround(location, found[0], found[1])}, so which ground is a choice: give z")
    instance = bpy.data.objects.new(name, None)
    instance.instance_type = "COLLECTION"
    instance.instance_collection = pieceCollection
    instance.location = location if len(location) == 3 else location + [0.0]
    instance.rotation_euler = (0.0, 0.0, math.radians(-facingDegrees))
    bridgeObjects.targetCollection(collection).objects.link(instance)
    bpy.context.view_layer.update()
    settled = bridgeArrangement.settleObjects([name], depth, 0.0, None)["settled"][0] if settling else None
  return bridgeKitGeometry.describePlacement(instance) | ({"settled": settled} if settled is not None else {"footing": footing(instance)})


def footing(instance):
  """What lies under a piece placed by its socket or at a given height: the lowest and highest surface players stand on under its
  footprint (found from a step over its base), and how far its base floats over the lowest, so a run snapped out over a drop says so."""
  bottom, _, samples = bridgeArrangement.footprint(instance)
  others = bridgeMeshAccess.playerSolidObjects({instance.name})
  surfaces = bridgeMeshAccess.PlayerSurfaces(objects=others) if others else None
  heights = [] if surfaces is None else [
    found.z for x, y in samples if (found := surfaces.footingBelow(mathutils.Vector((x, y, bottom + stepHeight)), bridgeMeshAccess.waterReach)) is not None
  ]
  if not heights:
    return {"base": round(bottom, 3), "under": None, "floats": None}
  gap = bottom - min(heights)
  return {"base": round(bottom, 3), "under": [round(min(heights), 3), round(max(heights), 3)], "floats": round(gap, 3) if gap > stepHeight else None}


def swapKitPiece(names, piece):
  if not isinstance(names, list) or not names:
    raise ValueError(f"names lists at least one placed kit piece, got {names!r}")
  placements = [bridgeKitData.requirePlacedPiece(name) for name in names]
  for placement in placements:
    bridgeStructureData.requireNotStructurePart(placement, "swapKitPiece")
  kits = sorted({bridgeKitData.kitPathOf(placement.instance_collection) or "" for placement in placements})
  if len(kits) > 1:
    raise ValueError(f"{names} come from different kits ({[kit or 'this file' for kit in kits]}); a piece is swapped for another of its own kit")
  kitPath = kits[0] or None
  with bridgeKitData.linkingUndone():
    replacement = bridgeKitData.requirePiece(kitPath, piece)
    newSockets = bridgeKitData.socketsByName(bridgeKitData.readRecord(replacement))
    for placement in placements:
      oldSockets = bridgeKitData.socketsByName(bridgeKitData.readRecord(placement.instance_collection))
      differences = [f"'{name}' only on {placement.instance_collection.name}" for name in sorted(set(oldSockets) - set(newSockets))]
      differences += [f"'{name}' only on {piece}" for name in sorted(set(newSockets) - set(oldSockets))]
      for name in sorted(set(oldSockets) & set(newSockets)):
        old, new = oldSockets[name], newSockets[name]
        if numpy.linalg.norm(numpy.subtract(old["at"], new["at"])) > bridgeKitData.jointDistance:
          differences.append(f"'{name}' at {old['at']} on {placement.instance_collection.name} but {new['at']} on {piece}")
        if numpy.dot(old["direction"], new["direction"]) < 1 - bridgeKitData.directionTolerance:
          differences.append(f"'{name}' faces {old['direction']} on {placement.instance_collection.name} but {new['direction']} on {piece}")
      if differences:
        raise ValueError(f"'{piece}' cannot stand in for '{placement.name}' ({placement.instance_collection.name}) without breaking its joints; its sockets differ: {'; '.join(differences)}")
    for placement in placements:
      placement.instance_collection = replacement
  bpy.context.view_layer.update()
  return {"placements": [bridgeKitGeometry.describePlacement(placement) for placement in placements]}


commands = {
  "createKitPiece": (createKitPiece, True),
  "markKitPiece": (markKitPiece, True),
  "cutOpening": (cutOpening, True),
  "addRoof": (addRoof, True),
  "placeKitPiece": (placeKitPiece, True),
}
