"""Kit pieces as records on collections: the kinds, the stem rule, finding or linking a piece, its geometry in its own frame, its
fingerprint, and its sockets where a placement puts them. Light on imports (no tool module), so every tool module can use it. Runs
under Blender's Python."""
import contextlib
import hashlib
import json
import math
import os

import bpy
import mathutils
import numpy

import bridgeMeshAccess

pieceProperty = "zonewrightKitPiece"
prefabProperty = "zonewrightPrefab"
kinds = {
  "wall": {"roles": ("face", "edge"), "sockets": ("start", "end", "top"), "passable": False},
  "floor": {"roles": ("top", "edge", "under"), "sockets": ("start", "end", "front", "back"), "passable": False},
  "post": {"roles": ("side", "top"), "sockets": ("base", "top"), "passable": False},
  "beam": {"roles": ("side", "end"), "sockets": ("start", "end"), "passable": False},
  "plank": {"roles": ("top", "edge"), "sockets": ("start", "end"), "passable": False},
  "rail": {"roles": ("side", "end"), "sockets": ("start", "end"), "passable": True},
  "ropeRail": {"roles": ("rope",), "sockets": ("start", "end"), "passable": True},
  "cap": {"roles": ("side", "top"), "sockets": ("base",), "passable": False},
  "roof": {"roles": ("roof", "under", "gable", "edge"), "sockets": ("plate",), "passable": False},
  "custom": {"roles": (), "sockets": (), "passable": False},
}
modeledKinds = ("wall", "floor", "post", "beam", "plank", "rail", "ropeRail", "cap")
straightKinds = ("wall", "beam", "plank", "rail", "ropeRail")
# Two placed sockets meet when this close, facing each other at least this squarely.
jointDistance = 0.01
jointFacing = -0.999
directionTolerance = 1e-6
# A placement's float32 matrix holds its unit axes to about a millionth.
uprightTolerance = 1e-4
socketReach = 1.0
# What a link brings into a file, taken back when the step that linked it is refused.
linkedKinds = ("libraries", "collections", "objects", "meshes", "materials", "images", "node_groups")


def plain(value):
  """A float as JSON keeps it, without a negative zero."""
  return float(value) + 0.0


def fileStem():
  if not bpy.data.filepath:
    raise ValueError("The open file has never been saved: a kit file's name begins every piece's name, so save it first (saveFile)")
  return os.path.splitext(os.path.basename(bpy.data.filepath))[0]


def requireStemmed(name, what):
  stem = fileStem()
  if not isinstance(name, str) or not name.startswith(stem):
    raise ValueError(f"{what} '{name}' must start with the kit file's stem '{stem}', as every piece of {stem}.blend does (for example '{stem}Wall25')")


def readRecord(collection):
  return json.loads(collection[pieceProperty]) if pieceProperty in collection else None


def writeRecord(collection, record):
  collection[pieceProperty] = json.dumps(record)


def isPlacedPiece(sceneObject):
  return bridgeMeshAccess.isCollectionInstance(sceneObject) and pieceProperty in sceneObject.instance_collection


def requirePlacedPiece(name):
  sceneObject = bridgeMeshAccess.requireObject(name)
  if not isPlacedPiece(sceneObject):
    raise ValueError(f"'{name}' is not a placed kit piece (placeKitPiece places them)")
  return sceneObject


def kitPathOf(collection):
  """The absolute path of the kit file a linked collection comes from, or None for the open file's own."""
  if collection.library is None:
    return None
  return os.path.normpath(bpy.path.abspath(collection.library.filepath))


def localPieces():
  return sorted(collection.name for collection in bpy.data.collections if collection.library is None and pieceProperty in collection)


def requireLocalPiece(name):
  collection = next((found for found in bpy.data.collections if found.name == name and found.library is None), None)
  if collection is None or pieceProperty not in collection:
    raise ValueError(f"'{name}' is not a kit piece of this file; its pieces: {localPieces()}")
  return collection


def requirePiece(kitPath, piece):
  """A piece's collection: the open file's own (kitPath None), or linked from the kit at kitPath (once; linking again finds it)."""
  if kitPath is None:
    return requireLocalPiece(piece)
  if not os.path.isabs(kitPath) or not os.path.isfile(kitPath):
    raise FileNotFoundError(f"Kit '{kitPath}' is not an existing absolute path to a .blend")
  with bpy.data.libraries.load(kitPath, link=True, assets_only=True) as (dataFrom, dataTo):
    if piece not in dataFrom.collections:
      raise ValueError(f"The kit {kitPath} holds no piece '{piece}'; its pieces and other assets: {sorted(dataFrom.collections)}")
    dataTo.collections = [piece]
  collection = dataTo.collections[0]
  if collection is None or pieceProperty not in collection:
    raise ValueError(f"'{piece}' in {kitPath} is an asset but not a kit piece (createKitPiece, markKitPiece, and addRoof make them)")
  return collection


@contextlib.contextmanager
def linkingUndone(always=False):
  """Whatever a refused step linked or made in the file is taken out again, so a refusal changes nothing; with always, also after a
  step that only read (a kit linked to be looked at)."""
  before = {kind: set(getattr(bpy.data, kind)) for kind in linkedKinds}

  def takeBack():
    added = [item for kind in linkedKinds for item in set(getattr(bpy.data, kind)) - before[kind]]
    if added:
      bpy.data.batch_remove(added)

  try:
    yield
  except Exception:
    takeBack()
    raise
  if always:
    takeBack()


def pieceMembers(collection):
  return sorted((member for member in collection.all_objects if member.type == "MESH"), key=lambda member: member.name)


def pieceOfMesh(sceneObject):
  """The kit piece whose collection holds a mesh object, or None."""
  return next((collection for collection in sceneObject.users_collection if pieceProperty in collection), None)


def fileMatrix(member):
  """Where a member stands in its kit file, from its own transform and its parents': a linked member drawn in no scene holds no world
  matrix of its own."""
  local = member.matrix_parent_inverse @ member.matrix_basis if member.parent is not None else member.matrix_basis
  return fileMatrix(member.parent) @ local if member.parent is not None else local


def pieceGeometry(collection):
  """A piece's member meshes flattened in the piece frame (origin at its instance_offset): positions, each face's corner count, the
  corners' vertices and texture coordinates, each face's material, and which faces players pass through."""
  record = readRecord(collection)
  offset = numpy.array(collection.instance_offset)
  positions, totals, corners, uvs, materials, passable = [], [], [], [], [], []
  count = 0
  for member in pieceMembers(collection):
    mesh = member.data
    coordinates = numpy.empty(len(mesh.vertices) * 3).reshape(-1, 3)
    mesh.vertices.foreach_get("co", coordinates.ravel())
    matrix = numpy.array(fileMatrix(member))
    positions.append(coordinates @ matrix[:3, :3].T + matrix[:3, 3] - offset)
    loopTotals, loopVertices = bridgeMeshAccess.faceLoops(member)
    totals.append(loopTotals)
    corners.append(loopVertices.astype(numpy.int64) + count)
    count += len(mesh.vertices)
    if mesh.uv_layers.active is None:
      raise ValueError(f"Mesh '{member.name}' of kit piece '{collection.name}' has no texture coordinates (projectUVs)")
    loopUVs = numpy.empty(len(mesh.loops) * 2)
    mesh.uv_layers.active.data.foreach_get("uv", loopUVs)
    uvs.append(loopUVs.reshape(-1, 2))
    slots = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("material_index", slots)
    slotMaterials = [slot.material for slot in member.material_slots]
    faceMaterials = [slotMaterials[slot] if slot < len(slotMaterials) else None for slot in slots]
    flagged = numpy.zeros(len(mesh.polygons), dtype=bool)
    attribute = mesh.attributes.get(bridgeMeshAccess.passableAttribute)
    if attribute is not None and attribute.domain == "FACE":
      attribute.data.foreach_get("value", flagged)
    wholly = record["passable"] or bridgeMeshAccess.passableProperty in member
    passable.append(flagged | wholly | numpy.array([bridgeMeshAccess.isPassableMaterial(material) for material in faceMaterials], dtype=bool))
    materials.extend(faceMaterials)
  if not positions:
    raise ValueError(f"Kit piece '{collection.name}' holds no mesh")
  return {
    "positions": numpy.concatenate(positions), "loopTotals": numpy.concatenate(totals), "loopVertices": numpy.concatenate(corners),
    "uvs": numpy.concatenate(uvs), "materials": materials, "passable": numpy.concatenate(passable),
  }


def fingerprint(collection):
  """Twelve hex digits of a sha1 over the piece as a placement draws it: each member mesh in the piece frame (positions to 1e-4, faces,
  corner texture coordinates to 1e-5, each face's material and passability), its instance_offset, and its record."""
  digest = hashlib.sha1()
  geometry = pieceGeometry(collection)
  digest.update((numpy.round(geometry["positions"], 4) + 0.0).tobytes())
  digest.update(geometry["loopTotals"].astype(numpy.int64).tobytes())
  digest.update(geometry["loopVertices"].astype(numpy.int64).tobytes())
  digest.update((numpy.round(geometry["uvs"], 5) + 0.0).tobytes())
  digest.update(json.dumps([None if material is None else material.name_full for material in geometry["materials"]]).encode())
  digest.update(geometry["passable"].tobytes())
  digest.update((numpy.round(numpy.array(collection.instance_offset), 4) + 0.0).tobytes())
  digest.update(json.dumps(readRecord(collection), sort_keys=True).encode())
  return digest.hexdigest()[:12]


def defaultSockets(kind, low, high):
  """A kind's sockets from the piece's bounds in its frame: ends out along X at the base, a top up, a base down, a floor's front and
  back, a roof's plate at its origin."""
  middle = (numpy.array(low) + numpy.array(high)) / 2
  places = {
    "start": ([low[0], middle[1], low[2]], [-1, 0, 0]), "end": ([high[0], middle[1], low[2]], [1, 0, 0]),
    "front": ([middle[0], high[1], low[2]], [0, 1, 0]), "back": ([middle[0], low[1], low[2]], [0, -1, 0]),
    "base": ([middle[0], middle[1], low[2]], [0, 0, -1]), "top": ([middle[0], middle[1], high[2]], [0, 0, 1]),
    "plate": ([0, 0, 0], [0, 0, -1]),
  }
  return [{"name": name, "at": [plain(value) for value in places[name][0]], "direction": [plain(value) for value in places[name][1]]} for name in kinds[kind]["sockets"]]


def socketsByName(record):
  return {socket["name"]: socket for socket in record["sockets"]}


def moduleOf(record):
  """The plan distance between a piece's start and end sockets where they face apart, as a straight piece's do; None without both, or
  for a corner or curve, whose sockets turn."""
  sockets = socketsByName(record)
  if "start" not in sockets or "end" not in sockets or numpy.dot(sockets["start"]["direction"], sockets["end"]["direction"]) >= jointFacing:
    return None
  start, end = sockets["start"]["at"], sockets["end"]["at"]
  return round(math.hypot(end[0] - start[0], end[1] - start[1]), 4)


def frameProud(record):
  """How far the frames of a piece's openings stand proud of its faces: 0 without framed openings."""
  return max((opening["frame"]["depth"] for opening in record["openings"] if opening["frame"] is not None), default=0.0)


def isUpright(sceneObject):
  """Whether a placement stands as placements do: at scale 1, turned only about the vertical."""
  matrix = sceneObject.matrix_world
  axes = [matrix.col[axis].xyz for axis in range(3)]
  return all(abs(axis.length - 1) <= uprightTolerance for axis in axes) and abs(axes[2].z - 1) <= uprightTolerance


def requireUpright(sceneObject, action):
  if not isUpright(sceneObject):
    raise ValueError(
      f"'{sceneObject.name}' is a placed kit piece, which stands upright at scale 1 and turns only about the vertical so its sockets meet the"
      f" next piece's; {action} would tilt or scale it"
    )


def isLevel(direction):
  return abs(direction[2]) <= directionTolerance


def facingOf(sceneObject):
  """The way a placement's front (the piece's +Y) faces, degrees clockwise from +Y."""
  facing = round((-math.degrees(sceneObject.matrix_world.to_euler("XYZ").z)) % 360.0, 4)
  return 0.0 if facing == 360.0 else plain(facing)


def worldSockets(sceneObject):
  """A placed piece's sockets where it stands: name, world point, world direction."""
  matrix = sceneObject.matrix_world
  turn = matrix.to_3x3().normalized()
  return [
    (socket["name"], matrix @ mathutils.Vector(socket["at"]), (turn @ mathutils.Vector(socket["direction"])).normalized())
    for socket in readRecord(sceneObject.instance_collection)["sockets"]
  ]


def placedPieces():
  bpy.context.view_layer.update()
  return [sceneObject for sceneObject in bpy.context.scene.objects if isPlacedPiece(sceneObject)]


def partnerOf(sceneObject, at, direction, others):
  """The placed piece socket {object, socket} meeting a world socket face to face, or None."""
  for other in others:
    if other == sceneObject:
      continue
    for name, otherAt, otherDirection in worldSockets(other):
      if (otherAt - at).length <= jointDistance and otherDirection.dot(direction) < jointFacing:
        return {"object": other.name, "socket": name}
  return None


def describeSockets(sceneObject):
  others = placedPieces()
  return [
    {"name": name, "at": roundVector(at), "direction": roundVector(direction), "joinedTo": partnerOf(sceneObject, at, direction, others)}
    for name, at, direction in worldSockets(sceneObject)
  ]


def roundVector(vector, digits=4):
  return [plain(round(float(component), digits)) for component in vector]


def readPrefab(collection):
  return json.loads(collection[prefabProperty]) if prefabProperty in collection else None


def writePrefab(collection, record):
  collection[prefabProperty] = json.dumps(record)


def localPrefabs():
  return sorted(collection.name for collection in bpy.data.collections if collection.library is None and prefabProperty in collection)


def partCollectionName(prefab, part):
  return prefab + part[0].upper() + part[1:]


def requirePrefab(kitPath, prefab):
  """A prefab's collection: the open file's own (kitPath None), or linked from the kit at kitPath (once; linking again finds it)."""
  if kitPath is None:
    collection = next((found for found in bpy.data.collections if found.name == prefab and found.library is None), None)
    if collection is None or prefabProperty not in collection:
      raise ValueError(f"'{prefab}' is not a prefab of this file; its prefabs: {localPrefabs()}")
    return collection
  if not os.path.isabs(kitPath) or not os.path.isfile(kitPath):
    raise FileNotFoundError(f"Kit '{kitPath}' is not an existing absolute path to a .blend")
  with bpy.data.libraries.load(kitPath, link=True, assets_only=True) as (dataFrom, dataTo):
    if prefab not in dataFrom.collections:
      raise ValueError(f"The kit {kitPath} holds no prefab '{prefab}'; its pieces and prefabs: {sorted(dataFrom.collections)}")
    dataTo.collections = [prefab]
  collection = dataTo.collections[0]
  if collection is None or prefabProperty not in collection:
    raise ValueError(f"'{prefab}' in {kitPath} is an asset but not a prefab (assemblePrefab makes them)")
  return collection


def prefabFingerprint(collection):
  """Twelve hex digits of a sha1 over a prefab's record (parts, footprint, entrances) and what each part gathers (each placed piece's
  name, piece, and place in the kit): its parts are instances, so placements follow its pieces' changes, and only a changed footprint or
  entrance, which seating and plinths were laid from, or a piece swapped, moved, added, or taken out of a part, changes this."""
  record = readPrefab(collection)
  digest = hashlib.sha1(json.dumps(record, sort_keys=True).encode())
  for part in record["parts"]:
    partCollection = collection.children.get(partCollectionName(collection.name, part))
    members = [] if partCollection is None else sorted(partCollection.objects, key=lambda member: member.name)
    gathered = [[member.name, None if member.instance_collection is None else member.instance_collection.name, (numpy.round(numpy.array(fileMatrix(member)), 4) + 0.0).tolist()] for member in members]
    digest.update(json.dumps([part, gathered]).encode())
  return digest.hexdigest()[:12]


def sourceFingerprint(collection):
  """The fingerprint of a piece or prefab a structure was laid from."""
  return prefabFingerprint(collection) if prefabProperty in collection else fingerprint(collection)


def requireSource(kitPath, name):
  """A kit piece's or prefab's collection a structure was laid from, linked as requirePiece and requirePrefab link them."""
  if kitPath is None:
    collection = next((found for found in bpy.data.collections if found.name == name and found.library is None), None)
    if collection is None or (pieceProperty not in collection and prefabProperty not in collection):
      raise ValueError(f"'{name}' is not a kit piece or prefab of this file")
    return collection
  if not os.path.isabs(kitPath) or not os.path.isfile(kitPath):
    raise FileNotFoundError(f"Kit '{kitPath}' is not an existing absolute path to a .blend")
  with bpy.data.libraries.load(kitPath, link=True, assets_only=True) as (dataFrom, dataTo):
    if name not in dataFrom.collections:
      raise ValueError(f"The kit {kitPath} holds no piece or prefab '{name}'")
    dataTo.collections = [name]
  collection = dataTo.collections[0]
  if collection is None or (pieceProperty not in collection and prefabProperty not in collection):
    raise ValueError(f"'{name}' in {kitPath} is not a kit piece or prefab")
  return collection


def prefabPartOf(sceneObject):
  """The prefab and part an object of this file is gathered into ({prefab, part}), or None."""
  for holder in sceneObject.users_collection:
    for prefab in bpy.data.collections:
      if prefabProperty in prefab and holder.name in prefab.children:
        part = next((name for name in readPrefab(prefab)["parts"] if partCollectionName(prefab.name, name) == holder.name), None)
        if part is not None:
          return {"prefab": prefab.name, "part": part}
  return None
