"""Kit piece geometry: the shapes createKitPiece and addRoof model, each role's texture mapping in the piece frame, a piece measured
(size, module, triangles, each material's repeat, texture seams), the rigid turn and move that places a piece or meets a socket, and
the fitting that lays pieces into one mesh: rigid, stretched along its axes, swept along a polyline, or sheared, each face's texture
carried at its world scale by its own affine mapping. Runs under Blender's Python."""
import math

import bmesh
import bpy
import mathutils
import numpy

import bridgeKitData
import bridgeMeshAccess
import bridgeSurfacing

boxCorners = numpy.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=numpy.float64)
boxFaces = {"front": (2, 3, 7, 6), "back": (0, 1, 5, 4), "right": (1, 2, 6, 5), "left": (3, 0, 4, 7), "top": (4, 5, 6, 7), "bottom": (0, 3, 2, 1)}
# A wall, post, or cap has no bottom: it stands on something, and an unseen face is a wasted triangle pair.
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


def gableEnds(shape, half, outline):
  """A roof's end walls over its plate at x = -half and half, each an [y, z] outline: one face looking out and one, on corners of its
  own, looking in, so a walk-in building's gable is seen from inside as the client draws one-sided faces."""
  for x in (-half, half):
    for facing in (1, -1):
      corners = [shape.point((x, y, z)) for y, z in outline]
      shape.face(corners, "gable", "box", (facing * math.copysign(1, x), 0, 0))


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
    gableEnds(shape, half, ((-halfDepth, 0), (halfDepth, 0), (0, ridge)))
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
    gableEnds(shape, half, ((halfDepth, 0), (-halfDepth, 0), (-halfDepth, depth * rise)))
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


# Vertices this close to a cut along a swept piece lie on it, and take the miter between the segments meeting there.
onCut = 1e-6


def faceOfLoop(geometry):
  return numpy.repeat(numpy.arange(len(geometry["loopTotals"])), geometry["loopTotals"])


def faceGradients(geometry):
  """Each face's texture mapping as an affine map of position, its gradient (2 x 3) fitted over its corners by least squares."""
  totals = geometry["loopTotals"]
  starts = numpy.cumsum(totals) - totals
  corners = geometry["positions"][geometry["loopVertices"]]
  gradients = numpy.zeros((len(totals), 2, 3))
  for face, (start, total) in enumerate(zip(starts, totals)):
    points = corners[start:start + total]
    design = numpy.column_stack([points - points.mean(0), numpy.ones(total)])
    fit, _, _, _ = numpy.linalg.lstsq(design, geometry["uvs"][start:start + total], rcond=None)
    gradients[face] = fit[:3].T
  return gradients


def pieceData(collection):
  """A piece's geometry in its frame (bridgeKitData.pieceGeometry) with each face's texture gradient, its bounds, and its record."""
  geometry = bridgeKitData.pieceGeometry(collection)
  geometry["gradients"] = faceGradients(geometry)
  geometry["low"], geometry["high"] = geometry["positions"].min(0), geometry["positions"].max(0)
  geometry["record"] = bridgeKitData.readRecord(collection)
  geometry["piece"] = collection.name
  return geometry


def moved(geometry, positions):
  """The geometry with its vertices at new places, each face's texture carried at its world scale (uv + gradient x move)."""
  shift = positions[geometry["loopVertices"]] - geometry["positions"][geometry["loopVertices"]]
  carried = geometry["uvs"] + numpy.einsum("lij,lj->li", geometry["gradients"][faceOfLoop(geometry)], shift)
  return geometry | {"positions": positions, "uvs": carried}


def stretched(geometry, scales):
  """Scaled along the piece's axes about its base center, texture carried."""
  return moved(geometry, geometry["positions"] * numpy.asarray(scales, dtype=numpy.float64))


def sheared(geometry, rise, length):
  """Each vertex raised rise x (its x / length): a wall section following the ground's fall about its middle, verticals vertical, its
  texture shearing with it."""
  positions = geometry["positions"].copy()
  positions[:, 2] += rise * positions[:, 0] / length
  return geometry | {"positions": positions}


def frameMatrix(origin, xAxis, yAxis):
  """A placement whose piece X runs along xAxis and Y along yAxis (both unit, square to each other), its Z their cross, at origin."""
  xAxis, yAxis = numpy.asarray(xAxis, dtype=numpy.float64), numpy.asarray(yAxis, dtype=numpy.float64)
  matrix = numpy.identity(4)
  matrix[:3, 0], matrix[:3, 1], matrix[:3, 2] = xAxis, yAxis, numpy.cross(xAxis, yAxis)
  matrix[:3, 3] = origin
  return matrix


def placed(geometry, matrix):
  """Turned and moved rigidly into the world (texture unchanged)."""
  return geometry | {"positions": geometry["positions"] @ matrix[:3, :3].T + matrix[:3, 3]}


def toEditor(geometry):
  """A bmesh of the geometry, each face tagged with the face it came from and carrying its corners' texture coordinates."""
  meshEditor = bmesh.new()
  uvLayer = meshEditor.loops.layers.uv.new(bridgeSurfacing.uvLayerName)
  sourceLayer = meshEditor.faces.layers.int.new("source")
  vertices = [meshEditor.verts.new(position) for position in geometry["positions"].tolist()]
  start = 0
  for face, total in enumerate(geometry["loopTotals"]):
    corners = geometry["loopVertices"][start:start + total]
    made = meshEditor.faces.new([vertices[index] for index in corners])
    made[sourceLayer] = face
    for loop, uv in zip(made.loops, geometry["uvs"][start:start + total]):
      loop[uvLayer].uv = (float(uv[0]), float(uv[1]))
    start += total
  return meshEditor


def fromEditor(meshEditor, geometry):
  """Geometry read back from a bmesh made by toEditor: each face keeps the material, passability, and gradient of the face it came from."""
  uvLayer = meshEditor.loops.layers.uv[bridgeSurfacing.uvLayerName]
  sourceLayer = meshEditor.faces.layers.int["source"]
  meshEditor.verts.index_update()
  positions = numpy.array([list(vertex.co) for vertex in meshEditor.verts], dtype=numpy.float64).reshape(-1, 3)
  totals, corners, uvs, sources = [], [], [], []
  for face in meshEditor.faces:
    totals.append(len(face.loops))
    corners.extend(loop.vert.index for loop in face.loops)
    uvs.extend(list(loop[uvLayer].uv) for loop in face.loops)
    sources.append(face[sourceLayer])
  sources = numpy.array(sources, dtype=numpy.int64)
  return geometry | {
    "positions": positions, "loopTotals": numpy.array(totals, dtype=numpy.int64), "loopVertices": numpy.array(corners, dtype=numpy.int64),
    "uvs": numpy.array(uvs, dtype=numpy.float64).reshape(-1, 2), "materials": [geometry["materials"][source] for source in sources],
    "passable": geometry["passable"][sources], "gradients": geometry["gradients"][sources],
  }


def faceNormals(geometry):
  """Each face's unit normal from its corners' winding (Newell's)."""
  corners = geometry["positions"][geometry["loopVertices"]]
  normals, start = [], 0
  for total in geometry["loopTotals"]:
    points = corners[start:start + total] - corners[start:start + total].mean(0)
    normal = numpy.cross(points, numpy.roll(points, -1, axis=0)).sum(0)
    normals.append(normal / max(float(numpy.linalg.norm(normal)), 1e-12))
    start += total
  return numpy.array(normals).reshape(-1, 3)


def bisected(geometry, planes, clearOuter=False):
  """Cut along planes (point, normal); with clearOuter what lies in front of each plane is taken off and the opening closed by a face
  taking the material and mapping of the piece's face that faced the way the plane does (a plank's end, where a deck's end is cut
  square), so its texture lies on it at that face's scale."""
  meshEditor = toEditor(geometry)
  uvLayer = meshEditor.loops.layers.uv[bridgeSurfacing.uvLayerName]
  sourceLayer = meshEditor.faces.layers.int["source"]
  normals = faceNormals(geometry) if clearOuter else None
  starts = numpy.cumsum(geometry["loopTotals"]) - geometry["loopTotals"]
  for point, normal in planes:
    geom = list(meshEditor.verts) + list(meshEditor.edges) + list(meshEditor.faces)
    result = bmesh.ops.bisect_plane(meshEditor, geom=geom, dist=onCut, plane_co=point, plane_no=normal, clear_outer=clearOuter)
    if not clearOuter:
      continue
    cutEdges = [element for element in result["geom_cut"] if isinstance(element, bmesh.types.BMEdge) and element.is_valid and element.is_boundary]
    if not cutEdges:
      continue
    source = int(numpy.argmax(normals @ (numpy.asarray(normal, dtype=numpy.float64) / numpy.linalg.norm(normal))))
    referencePoint = geometry["positions"][geometry["loopVertices"][starts[source]]]
    referenceUV = geometry["uvs"][starts[source]]
    gradient = geometry["gradients"][source]
    for face in bmesh.ops.holes_fill(meshEditor, edges=cutEdges, sides=0)["faces"]:
      face[sourceLayer] = source
      for loop in face.loops:
        uv = referenceUV + gradient @ (numpy.array(loop.vert.co) - referencePoint)
        loop[uvLayer].uv = (float(uv[0]), float(uv[1]))
  if clearOuter:
    bmesh.ops.recalc_face_normals(meshEditor, faces=list(meshEditor.faces))
  cut = fromEditor(meshEditor, geometry)
  meshEditor.free()
  return cut


def polylineFrames(points, plumb=False):
  """Each segment's tangent, level side (left of travel), and up: tangent x side, or straight up when plumb."""
  tangents = numpy.diff(points, axis=0)
  tangents /= numpy.linalg.norm(tangents, axis=1, keepdims=True)
  sides = numpy.column_stack([-tangents[:, 1], tangents[:, 0], numpy.zeros(len(tangents))])
  sides /= numpy.linalg.norm(sides, axis=1, keepdims=True)
  ups = numpy.tile([0.0, 0.0, 1.0], (len(tangents), 1)) if plumb else numpy.cross(tangents, sides)
  return tangents, sides, ups


def swept(geometry, points, acrossScale=1.0, plumb=False):
  """Bent along a polyline: X stretched to its length (texture carried) and cut where it bends, so X runs along it; Y and Z offsets
  taken in each segment's frame (tangent, level side, up, or straight up when plumb, so a hanging card's uprights hang plumb on a
  slope); vertices on a cut lie on the miter between the segments meeting there."""
  points = numpy.asarray(points, dtype=numpy.float64)
  arcs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1))])
  low, high = geometry["low"][0], geometry["high"][0]
  straight = geometry["positions"].copy()
  straight[:, 0] = (straight[:, 0] - low) * arcs[-1] / (high - low)
  straight[:, 1] *= acrossScale
  laid = moved(geometry, straight)
  if len(points) > 2:
    laid = bisected(laid, [((arc, 0.0, 0.0), (1.0, 0.0, 0.0)) for arc in arcs[1:-1]])
  tangents, sides, ups = polylineFrames(points, plumb)
  along = laid["positions"][:, 0]
  segments = numpy.clip(numpy.searchsorted(arcs, along, side="right") - 1, 0, len(points) - 2)
  offsets = laid["positions"][:, 1:2] * sides[segments] + laid["positions"][:, 2:3] * ups[segments]
  world = points[segments] + tangents[segments] * (along - arcs[segments])[:, None] + offsets
  for vertex in numpy.nonzero((segments > 0) & (numpy.abs(along - arcs[segments]) <= onCut))[0]:
    joint = segments[vertex]
    miter = tangents[joint - 1] + tangents[joint]
    if plumb:
      # A plumb card's segments meet on the vertical through the joint whatever their slopes: its miter stands upright.
      miter[2] = 0.0
    miter /= numpy.linalg.norm(miter)
    meeting = []
    for segment in (joint - 1, joint):
      offset = laid["positions"][vertex, 1] * sides[segment] + laid["positions"][vertex, 2] * ups[segment]
      meeting.append(points[joint] + offset - tangents[segment] * (offset @ miter) / (tangents[segment] @ miter))
    world[vertex] = (meeting[0] + meeting[1]) / 2
  return laid | {"positions": world}


class Bake:
  """Pieces gathered into one mesh, each an island of its own (nothing merged), one material slot per material, the texture
  coordinates in the UV layer, and the faces players pass through flagged by the passable face attribute."""

  def __init__(self):
    self.parts = []

  def add(self, geometry):
    self.parts.append(geometry)

  def triangles(self):
    return int(sum((part["loopTotals"] - 2).sum() for part in self.parts))

  def mesh(self, name, inverse):
    """The gathered pieces as a new mesh, in the space whose inverse world matrix is given."""
    positions, faces, uvs, materials, passable, offset = [], [], [], [], [], 0
    for part in self.parts:
      positions.append(part["positions"] @ inverse[:3, :3].T + inverse[:3, 3])
      start = 0
      for total in part["loopTotals"]:
        faces.append((part["loopVertices"][start:start + total] + offset).tolist())
        start += total
      uvs.append(part["uvs"])
      materials.extend(part["materials"])
      passable.append(part["passable"])
      offset += len(part["positions"])
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(numpy.concatenate(positions).tolist(), [], faces)
    slots = {}
    for material in materials:
      if material.as_pointer() not in slots:
        slots[material.as_pointer()] = len(slots)
        mesh.materials.append(material)
    mesh.polygons.foreach_set("material_index", numpy.array([slots[material.as_pointer()] for material in materials], dtype=numpy.int32))
    mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName).data.foreach_set("uv", numpy.concatenate(uvs).astype(numpy.float32).ravel())
    flags = numpy.concatenate(passable)
    if flags.any():
      mesh.attributes.new(bridgeMeshAccess.passableAttribute, "BOOLEAN", "FACE").data.foreach_set("value", flags)
    mesh.update()
    return mesh
