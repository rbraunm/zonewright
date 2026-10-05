"""Kit piece geometry: the shapes createKitPiece and addRoof model, each role's texture mapping in the piece frame, a piece measured
(size, module, triangles, each material's repeat, texture seams), and the rigid turn and move that places a piece or meets a socket.
Runs under Blender's Python."""
import math

import bpy
import mathutils
import numpy

import bridgeKitData
import bridgeMeshAccess
import bridgeSurfacing

# A box's corners as multiples of half its length, half its depth, and its height, and its faces wound to face out.
boxCorners = numpy.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=numpy.float64)
boxFaces = {"front": (2, 3, 7, 6), "back": (0, 1, 5, 4), "right": (1, 2, 6, 5), "left": (3, 0, 4, 7), "top": (4, 5, 6, 7), "bottom": (0, 3, 2, 1)}
# Which faces of its box each kind keeps, and the role each plays; a wall, post, or cap has no bottom, as it stands on something.
kindFaces = {
  "wall": {"front": "face", "back": "face", "left": "edge", "right": "edge", "top": "edge"},
  "floor": {"top": "top", "front": "edge", "back": "edge", "left": "edge", "right": "edge", "bottom": "under"},
  "post": {"front": "side", "back": "side", "left": "side", "right": "side", "top": "top"},
  "beam": {"front": "side", "back": "side", "top": "side", "bottom": "side", "left": "end", "right": "end"},
  "plank": {"top": "top", "bottom": "top", "front": "edge", "back": "edge", "left": "edge", "right": "edge"},
  "rail": {"front": "side", "back": "side", "top": "side", "bottom": "side", "left": "end", "right": "end"},
  "cap": {"front": "side", "back": "side", "left": "side", "right": "side", "top": "top"},
}
# Bars whose texture's grain (its v, as the client's wood textures run it) follows their length along X rather than up.
grainKinds = ("beam", "plank", "rail")
roofKinds = ("gable", "hip", "shed")
roofRoles = {"gable": ("roof", "under", "gable", "edge"), "hip": ("roof", "under", "edge"), "shed": ("roof", "under", "gable", "edge")}
# A repeat count this close to whole tiles without a seam.
wholeRepeats = 1e-3
axisNames = ("x", "y", "z")


class Shape:
  """Faces to build a piece from: positions in the piece frame, each face's corners (wound to face out), role, and mapping (box, or
  slope: in the face's plane, level along it and up it)."""

  def __init__(self):
    self.positions, self.faces, self.roles, self.mappings = [], [], [], []

  def point(self, position):
    self.positions.append([float(value) for value in position])
    return len(self.positions) - 1

  def face(self, corners, role, mapping, outward):
    points = numpy.array([self.positions[index] for index in corners])
    if newellNormal(points) @ numpy.array(outward, dtype=numpy.float64) < 0:
      corners = tuple(reversed(corners))
    self.faces.append(tuple(corners))
    self.roles.append(role)
    self.mappings.append(mapping)

  def transformed(self, turn):
    self.positions = [list(numpy.array(turn) @ numpy.array(position)) for position in self.positions]
    return self


def newellNormal(points):
  following = numpy.roll(points, -1, axis=0)
  normal = numpy.cross(points, following).sum(0)
  length = numpy.linalg.norm(normal)
  return normal / length if length > 0 else normal


def pieceShape(kind, size):
  length, depth, height = size
  shape = Shape()
  if kind == "ropeRail":
    half = length / 2
    front = [shape.point(point) for point in ((half, 0, 0), (-half, 0, 0), (-half, 0, height), (half, 0, height))]
    back = [shape.point(point) for point in ((half, 0, 0), (-half, 0, 0), (-half, 0, height), (half, 0, height))]
    shape.face(front, "rope", "box", (0, 1, 0))
    shape.face(back, "rope", "box", (0, -1, 0))
    return shape
  for corner in boxCorners * numpy.array([length / 2, depth / 2, height]):
    shape.point(corner)
  outward = {"front": (0, 1, 0), "back": (0, -1, 0), "right": (1, 0, 0), "left": (-1, 0, 0), "top": (0, 0, 1), "bottom": (0, 0, -1)}
  for side, role in kindFaces[kind].items():
    shape.face(boxFaces[side], role, "grain" if kind in grainKinds else "box", outward[side])
  return shape


def roofShape(kind, footprint, pitchDegrees, overhang, thickness, ridgeAlong):
  """A roof over a footprint [length X, depth Y] whose slopes' undersides pass through its edges at the plate (z 0), and its ridge
  and eave heights. Built with its ridge along X, then turned when it runs along Y."""
  length, depth = footprint if ridgeAlong == "x" else footprint[::-1]
  pitch = math.radians(pitchDegrees)
  rise, lift = math.tan(pitch), thickness / math.cos(pitch)
  sine, cosine = math.sin(pitch), math.cos(pitch)
  half, halfDepth = length / 2, depth / 2
  shape = Shape()
  if kind == "gable":
    ridge = halfDepth * rise
    ends = (-(half + overhang), half + overhang)
    ridgeUnder = [shape.point((x, 0, ridge)) for x in ends]
    ridgeTop = [shape.point((x, 0, ridge + lift)) for x in ends]
    for side in (1, -1):
      eaveUnder = [shape.point((x, side * (halfDepth + overhang), -overhang * rise)) for x in ends]
      eaveTop = [shape.point((x, side * (halfDepth + overhang), -overhang * rise + lift)) for x in ends]
      shape.face((eaveTop[0], eaveTop[1], ridgeTop[1], ridgeTop[0]), "roof", "slope", (0, side * sine, cosine))
      shape.face((eaveUnder[0], eaveUnder[1], ridgeUnder[1], ridgeUnder[0]), "under", "slope", (0, -side * sine, -cosine))
      shape.face((eaveUnder[0], eaveUnder[1], eaveTop[1], eaveTop[0]), "edge", "box", (0, side, 0))
      for end, x in enumerate(ends):
        shape.face((eaveUnder[end], ridgeUnder[end], ridgeTop[end], eaveTop[end]), "edge", "box", (math.copysign(1, x), 0, 0))
    for x in (-half, half):
      corners = [shape.point(point) for point in ((x, -halfDepth, 0), (x, halfDepth, 0), (x, 0, ridge))]
      shape.face(corners, "gable", "box", (math.copysign(1, x), 0, 0))
    ridgeHeight, eaveHeight = ridge + lift, -overhang * rise
  elif kind == "hip":
    outerX, outerY, ridgeHalf, ridge = half + overhang, halfDepth + overhang, half - halfDepth, halfDepth * rise
    layers = {}
    for name, raise_ in (("under", 0.0), ("top", lift)):
      corners = [shape.point((x, y, -overhang * rise + raise_)) for x, y in ((outerX, outerY), (-outerX, outerY), (-outerX, -outerY), (outerX, -outerY))]
      if ridgeHalf > 0:
        peaks = [shape.point((ridgeHalf, 0, ridge + raise_)), shape.point((-ridgeHalf, 0, ridge + raise_))]
      else:
        peaks = [shape.point((0, 0, ridge + raise_))] * 2
      layers[name] = (corners, peaks)
    for name, sign in (("top", 1), ("under", -1)):
      (c1, c2, c3, c4), (p1, p2) = layers[name]
      role = "roof" if name == "top" else "under"
      shape.face((c2, c1, p1, p2) if p1 != p2 else (c2, c1, p1), role, "slope", (0, sign * sine, sign * cosine))
      shape.face((c4, c3, p2, p1) if p1 != p2 else (c4, c3, p1), role, "slope", (0, -sign * sine, sign * cosine))
      shape.face((c1, c4, p1), role, "slope", (sign * sine, 0, sign * cosine))
      shape.face((c3, c2, p2), role, "slope", (-sign * sine, 0, sign * cosine))
    under, top = layers["under"][0], layers["top"][0]
    for first, second, outward in ((1, 0, (0, 1, 0)), (0, 3, (1, 0, 0)), (3, 2, (0, -1, 0)), (2, 1, (-1, 0, 0))):
      shape.face((under[first], under[second], top[second], top[first]), "edge", "box", outward)
    ridgeHeight, eaveHeight = ridge + lift, -overhang * rise
  else:
    ends = (-(half + overhang), half + overhang)
    lowY, highY = halfDepth + overhang, -(halfDepth + overhang)
    lowZ, highZ = -overhang * rise, (depth + overhang) * rise
    lowUnder = [shape.point((x, lowY, lowZ)) for x in ends]
    lowTop = [shape.point((x, lowY, lowZ + lift)) for x in ends]
    highUnder = [shape.point((x, highY, highZ)) for x in ends]
    highTop = [shape.point((x, highY, highZ + lift)) for x in ends]
    shape.face((lowTop[0], lowTop[1], highTop[1], highTop[0]), "roof", "slope", (0, sine, cosine))
    shape.face((lowUnder[0], lowUnder[1], highUnder[1], highUnder[0]), "under", "slope", (0, -sine, -cosine))
    shape.face((lowUnder[0], lowUnder[1], lowTop[1], lowTop[0]), "edge", "box", (0, 1, 0))
    shape.face((highUnder[0], highUnder[1], highTop[1], highTop[0]), "edge", "box", (0, -1, 0))
    for end, x in enumerate(ends):
      shape.face((lowUnder[end], highUnder[end], highTop[end], lowTop[end]), "edge", "box", (math.copysign(1, x), 0, 0))
    for x in (-half, half):
      corners = [shape.point(point) for point in ((x, halfDepth, 0), (x, -halfDepth, 0), (x, -halfDepth, depth * rise))]
      shape.face(corners, "gable", "box", (math.copysign(1, x), 0, 0))
    ridgeHeight, eaveHeight = highZ + lift, lowZ
  if ridgeAlong == "y":
    shape.transformed([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
  return shape, ridgeHeight, eaveHeight


boxAxes = {"box": {0: (1, 2), 1: (0, 2), 2: (0, 1)}, "grain": {0: (1, 2), 1: (2, 0), 2: (1, 0)}}


def boxMapping(points, normal, corner, repeat, mapping="box"):
  """Texture coordinates projected along the face's main axis, counted from the piece's corner: u along X (Y on an end) and v up;
  with grain, v along X on every face but the ends."""
  across = boxAxes[mapping][int(numpy.abs(normal).argmax())]
  return (points[:, across] - corner[list(across)]) / repeat


def slopeMapping(points, normal, corner, repeat):
  """Texture coordinates in the face's own plane: u level along it, v up it, so rows of shingles run level."""
  level = numpy.array([-normal[1], normal[0], 0.0])
  level /= numpy.linalg.norm(level)
  upSlope = numpy.array([0.0, 0.0, 1.0]) - normal * normal[2]
  upSlope /= numpy.linalg.norm(upSlope)
  relative = points - corner
  return numpy.stack([relative @ level, relative @ upSlope], axis=1) / repeat


def mapFace(points, mapping, corner, repeat):
  normal = newellNormal(points)
  if mapping == "slope":
    return slopeMapping(points, normal, corner, repeat)
  return boxMapping(points, normal, corner, repeat, mapping)


def buildMesh(name, shape, roleMaterials, roleRepeats):
  """A mesh of the shape's faces, each in its role's material and mapped at its role's repeat from the shape's lowest corner."""
  positions = numpy.array(shape.positions)
  corner = positions.min(0)
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata(positions.tolist(), [], [list(face) for face in shape.faces])
  mesh.update()
  slots = []
  for role in shape.roles:
    if roleMaterials[role] not in slots:
      slots.append(roleMaterials[role])
  for material in slots:
    mesh.materials.append(material)
  mesh.polygons.foreach_set("material_index", numpy.array([slots.index(roleMaterials[role]) for role in shape.roles], dtype=numpy.int32))
  uvs = numpy.concatenate([mapFace(positions[list(face)], mapping, corner, roleRepeats[role]) for face, role, mapping in zip(shape.faces, shape.roles, shape.mappings)])
  mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName).data.foreach_set("uv", uvs.astype(numpy.float32).ravel())
  mesh.update()
  return mesh


def polygonUVAreas(mesh):
  totals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
  mesh.polygons.foreach_get("loop_total", totals)
  uvs = numpy.empty(len(mesh.loops) * 2)
  mesh.uv_layers.active.data.foreach_get("uv", uvs)
  uvs = uvs.reshape(-1, 2)
  starts = numpy.cumsum(totals) - totals
  areas = numpy.empty(len(totals))
  for face, (start, total) in enumerate(zip(starts, totals)):
    corners = uvs[start:start + total]
    following = numpy.roll(corners, -1, axis=0)
    areas[face] = abs((corners[:, 0] * following[:, 1] - following[:, 0] * corners[:, 1]).sum()) / 2
  return areas


def roleRepeats(mesh, roles):
  """Each role's measured world units per texture repeat over its faces."""
  worldAreas = numpy.empty(len(mesh.polygons))
  mesh.polygons.foreach_get("area", worldAreas)
  uvAreas = polygonUVAreas(mesh)
  measured = {}
  for role in dict.fromkeys(roles):
    faces = numpy.array([face for face, faceRole in enumerate(roles) if faceRole == role])
    repeat = bridgeMeshAccess.worldUnitsPerRepeat(worldAreas[faces].sum(), uvAreas[faces].sum())
    measured[role] = None if repeat is None else round(repeat, 4)
  return measured


def pieceBounds(collection):
  positions = bridgeKitData.pieceGeometry(collection)["positions"]
  return positions.min(0), positions.max(0)


def diffuseTexture(material):
  node = material.node_tree.nodes.get(bridgeSurfacing.diffuseNodeName) if material is not None and material.node_tree is not None else None
  return None if node is None or node.image is None else node.image.name


def jointAxes(record):
  """The axes along which a piece meets more of its kind (sockets facing apart, as start and end), with its extent along each."""
  axes = {}
  sockets = record["sockets"]
  for index, first in enumerate(sockets):
    for second in sockets[index + 1:]:
      if numpy.dot(first["direction"], second["direction"]) < bridgeKitData.jointFacing:
        axis = int(numpy.abs(first["direction"]).argmax())
        axes[axis] = abs(first["at"][axis] - second["at"][axis])
  return axes


def measurePiece(collection):
  """What a piece's record leaves to be derived: size, module, triangles, each material's texture and measured repeat, and seams
  (materials whose repeat does not fit the piece's extent along an axis it joins along a whole number of times, so a texture seam
  shows where two such pieces meet)."""
  record = bridgeKitData.readRecord(collection)
  low, high = pieceBounds(collection)
  triangles = 0
  areas, runs, textures = {}, {}, {}
  for member in bridgeKitData.pieceMembers(collection):
    mesh = member.data
    triangles += bridgeMeshAccess.triangleCount(member)
    textureAreas = bridgeMeshAccess.textureAreas(member)
    if textureAreas is None:
      raise ValueError(f"Mesh '{member.name}' of kit piece '{collection.name}' has no texture coordinates")
    triangleVertices = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangleVertices)
    positions, _ = bridgeMeshAccess.readVertexArrays(member)
    extents = numpy.ptp(positions[triangleVertices.reshape(-1, 3)], axis=1)
    slotMaterials = [slot.material for slot in member.material_slots]
    for slot in numpy.unique(textureAreas.materials):
      material = slotMaterials[slot] if slot < len(slotMaterials) else None
      key = None if material is None else material.name_full
      chosen = textureAreas.materials == slot
      world, uv = areas.get(key, (0.0, 0.0))
      areas[key] = (world + textureAreas.world[chosen].sum(), uv + textureAreas.uv[chosen].sum())
      runs[key] = runs.get(key, numpy.zeros(3, dtype=bool)) | (extents[chosen] > 1e-6).any(axis=0)
      textures[key] = diffuseTexture(material)
  materials, seams = [], []
  for key, (world, uv) in areas.items():
    repeat = bridgeMeshAccess.worldUnitsPerRepeat(world, uv)
    materials.append({"material": key, "texture": textures[key], "worldUnitsPerRepeat": None if repeat is None else round(repeat, 4)})
    if repeat is None:
      continue
    for axis, extent in jointAxes(record).items():
      count = extent / repeat
      if runs[key][axis] and abs(count - round(count)) > wholeRepeats:
        seams.append({"material": key, "along": axisNames[axis], "extent": round(extent, 4), "worldUnitsPerRepeat": round(repeat, 4), "repeats": round(count, 4)})
  return {
    "size": bridgeKitData.roundVector(high - low), "bounds": [bridgeKitData.roundVector(low), bridgeKitData.roundVector(high)],
    "module": bridgeKitData.moduleOf(record), "triangles": triangles, "materials": materials, "seams": seams,
  }


def describePiece(collection):
  """A piece: its kit (null for the open file's own), record, what is derived from it, and its fingerprint."""
  return {"piece": collection.name, "kit": bridgeKitData.kitPathOf(collection)} | bridgeKitData.readRecord(collection) | measurePiece(collection) | {
    "fingerprint": bridgeKitData.fingerprint(collection),
  }


def describePlacement(sceneObject):
  """A placed piece: where it stands and faces, its piece, and its sockets in the world, each joined to another's or free."""
  collection = sceneObject.instance_collection
  record = bridgeKitData.readRecord(collection)
  measured = measurePiece(collection)
  return {
    "name": sceneObject.name, "location": bridgeKitData.roundVector(sceneObject.matrix_world.translation), "facingDegrees": bridgeKitData.facingOf(sceneObject),
    "piece": {
      "kit": bridgeKitData.kitPathOf(collection), "piece": collection.name, "kind": record["kind"], "size": measured["size"], "module": measured["module"],
      "triangles": measured["triangles"], "passable": record["passable"], "fingerprint": bridgeKitData.fingerprint(collection),
    },
    "sockets": bridgeKitData.describeSockets(sceneObject),
  }


def turnAbout(facingDegrees):
  """The turn about the vertical that faces a piece's front (+Y) facingDegrees clockwise from +Y."""
  return mathutils.Matrix.Rotation(math.radians(-facingDegrees), 3, "Z")


def snappedPlacement(targetAt, targetDirection, socket, facingDegrees):
  """Where a piece stands, and how it faces, so its socket meets a placed socket face to face: a level pair turns the piece until
  its socket faces back along the target's; a vertical pair keeps facingDegrees."""
  direction = mathutils.Vector(socket["direction"])
  if bridgeKitData.isLevel(direction):
    turn = math.atan2(-targetDirection.y, -targetDirection.x) - math.atan2(direction.y, direction.x)
    facingDegrees = (-math.degrees(turn)) % 360.0
  location = targetAt - turnAbout(facingDegrees) @ mathutils.Vector(socket["at"])
  return location, facingDegrees
