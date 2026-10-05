"""What a terrain's caves (bridgeCaves) leave in its mesh, and the guards every other tool keeps to around them: which vertices are the
plug (terrain vertices whose faces a cut took out, kept to put them back), the ring (where a cave meets the ground, following its plug
triangles in every shaping pass), and the lining (the cave's own surface, holding no offset in any pass); which faces a cave made; the
cave selector; and the refusals of topology changes. Reads only the mesh and its object, so every module can import it. Runs under
Blender's Python."""
import json

import numpy

caveProperty = "zonewrightCaves"
caveLayerStart = "zonewrightCave"
vertexTagPrefix = "zonewrightCaveVertex:"
faceTagPrefix = "zonewrightCaveFace:"
sourcePrefix = "zonewrightCaveSource:"
weightsPrefix = "zonewrightCaveWeights:"
groundPrefix = "zonewrightCaveGround:"
ringTag = -1
liningTag = -2
liningFaceTag = -1


def caves(sceneObject):
  """A mesh's caves by name: each its definition, its plug faces, and the lining strokes replayed on every cut."""
  return json.loads(sceneObject[caveProperty]) if caveProperty in sceneObject else {}


def holdsCaves(sceneObject):
  return caveProperty in sceneObject


def writeCaves(sceneObject, known):
  if known:
    sceneObject[caveProperty] = json.dumps(known)
  elif caveProperty in sceneObject:
    del sceneObject[caveProperty]


def requireNoCaves(sceneObject, action):
  names = sorted(caves(sceneObject))
  if names:
    raise ValueError(
      f"'{sceneObject.name}' holds caves {names}, whose take-back depends on the faces around them staying as they were cut; to {action},"
      " take each back (removeCave, which returns its definition), make the change, then cut it again (cutCave with that definition)"
    )


def attributeValues(mesh, name, width=1):
  """An int attribute's values, or a vector attribute's (width 3) as float64; read in the attribute's own type, which Blender copies
  without converting each value."""
  attribute = mesh.attributes[name]
  values = numpy.empty(len(attribute.data) * width, dtype=numpy.float32 if width == 3 else numpy.int32)
  attribute.data.foreach_get("vector" if width == 3 else "value", values)
  return values.reshape(-1, 3).astype(numpy.float64) if width == 3 else values


def reachOf(sceneObject, name):
  """The vertices of the ground within a cave's reach as it was cut, whose moving makes the cave stale."""
  return ~numpy.isnan(attributeValues(sceneObject.data, groundPrefix + name, 3)).any(axis=1)


def plugIndices(tags):
  """Each plug id's vertex, -1 for an id no vertex carries."""
  rows = numpy.flatnonzero(tags > 0)
  byIdentifier = numpy.full(int(tags.max(initial=0)) + 1, -1, dtype=numpy.int64)
  byIdentifier[tags[rows]] = rows
  return byIdentifier


def ringData(sceneObject, name, record, tags):
  """A cave's ring vertices, the vertices at the corners of each one's plug triangle, and its weights over them."""
  mesh = sceneObject.data
  ringRows = numpy.flatnonzero(tags == ringTag)
  if not len(ringRows):
    return ringRows, numpy.zeros((0, 3), dtype=numpy.int64), numpy.zeros((0, 3))
  triangles = numpy.array([plugFace["vertices"] for plugFace in record["plug"]], dtype=numpy.int64).reshape(-1, 3)
  sources = attributeValues(mesh, sourcePrefix + name)[ringRows]
  if sources.min() < 0 or sources.max() >= len(triangles):
    raise ValueError(f"Cave '{name}' of '{sceneObject.name}' has ring vertices whose plug triangle is not recorded; restore a checkpoint")
  identifiers = triangles[sources]
  byIdentifier = plugIndices(tags)
  if identifiers.max() >= len(byIdentifier) or (byIdentifier[identifiers] < 0).any():
    raise ValueError(f"Cave '{name}' of '{sceneObject.name}' has lost plug vertices its ring follows; restore a checkpoint")
  return ringRows, byIdentifier[identifiers], attributeValues(mesh, weightsPrefix + name, 3)[ringRows]


class CaveVertices:
  """Which vertices of a mesh its caves hold (plug, ring, lining) and which cave owns each, with every ring vertex's plug triangle and
  its weights there."""

  def __init__(self, sceneObject):
    mesh = sceneObject.data
    count = len(mesh.vertices)
    self.names = sorted(caves(sceneObject))
    self.plug, self.ring, self.lining = (numpy.zeros(count, dtype=bool) for _ in range(3))
    self.owner = numpy.full(count, -1)
    rows, corners, weights = [], [], []
    for position, (name, record) in enumerate(sorted(caves(sceneObject).items())):
      tags = attributeValues(mesh, vertexTagPrefix + name)
      self.owner[tags != 0] = position
      self.plug |= tags > 0
      self.ring |= tags == ringTag
      self.lining |= tags == liningTag
      for collected, part in zip((rows, corners, weights), ringData(sceneObject, name, record, tags)):
        collected.append(part)
    self.ringRows = numpy.concatenate(rows) if rows else numpy.zeros(0, dtype=numpy.int64)
    self.ringCorners = numpy.concatenate(corners) if corners else numpy.zeros((0, 3), dtype=numpy.int64)
    self.ringWeights = numpy.concatenate(weights) if weights else numpy.zeros((0, 3))
    self.tagged = self.plug | self.ring | self.lining

  def settled(self, coordinates):
    """Coordinates with each ring vertex put back on its plug triangle as the triangle's corners stand in them."""
    coordinates = coordinates.copy()
    coordinates[self.ringRows] = numpy.einsum("rk,rkj->rj", self.ringWeights, coordinates[self.ringCorners])
    return coordinates

  def namesOf(self, mask):
    return sorted({self.names[owner] for owner in self.owner[mask] if owner >= 0})


def guardedKey(sceneObject, current, requested):
  """A pass's (or the mesh's) new coordinates as a write may set them: the lining left where it is and each ring vertex on its plug
  triangle; with how many lining vertices the write would have moved, or None for a mesh without caves."""
  if not holdsCaves(sceneObject):
    return requested, None
  caveVertices = CaveVertices(sceneObject)
  moved = caveVertices.lining & (numpy.abs(requested - current).max(axis=1) > 1e-9)
  guarded = requested.copy()
  guarded[caveVertices.lining] = current[caveVertices.lining]
  return caveVertices.settled(guarded), int(moved.sum())


def guardedOffsets(sceneObject, offsets):
  """Offsets a pass may hold: none on the lining, each ring vertex's from its plug triangle's; with how many lining vertices had one,
  or None for a mesh without caves."""
  if not holdsCaves(sceneObject):
    return offsets, None
  caveVertices = CaveVertices(sceneObject)
  held = caveVertices.lining & (numpy.abs(offsets).max(axis=1) > 1e-9)
  guarded = offsets.copy()
  guarded[caveVertices.lining] = 0.0
  return caveVertices.settled(guarded), int(held.sum())


def fixedInPlan(sceneObject):
  """The vertices no stroke slides sideways: every cave's plug, ring, and lining."""
  if not holdsCaves(sceneObject):
    return numpy.zeros(len(sceneObject.data.vertices), dtype=bool)
  return CaveVertices(sceneObject).tagged


def liningVertices(sceneObject):
  if not holdsCaves(sceneObject):
    return numpy.zeros(len(sceneObject.data.vertices), dtype=bool)
  return CaveVertices(sceneObject).lining


def caveMadeVertices(sceneObject):
  """The vertices caves made: their rings and linings, none of them the ground's own."""
  if not holdsCaves(sceneObject):
    return numpy.zeros(len(sceneObject.data.vertices), dtype=bool)
  caveVertices = CaveVertices(sceneObject)
  return caveVertices.ring | caveVertices.lining


def liningReport(left):
  """What a result says of the lining vertices a write left alone: nothing for a mesh without caves."""
  return {} if left is None else {"caveLiningLeft": left}


def faceTags(sceneObject, name):
  return attributeValues(sceneObject.data, faceTagPrefix + name)


def caveFaceMask(sceneObject):
  """The faces any cave made: its lining and the pieces of ground its cut left at the mouth."""
  mask = numpy.zeros(len(sceneObject.data.polygons), dtype=bool)
  for name in caves(sceneObject):
    mask |= faceTags(sceneObject, name) != 0
  return mask


def requireCaveNames(sceneObject, value):
  known = caves(sceneObject)
  if value is True:
    if not known:
      raise ValueError(f"'{sceneObject.name}' has no caves")
    return sorted(known)
  if not isinstance(value, str) or value not in known:
    raise ValueError(f"The cave selector is {{\"cave\": name}} or {{\"cave\": true}}; '{sceneObject.name}' has caves {sorted(known)}, got {value!r}")
  return [value]


def liningSelection(sceneObject, value, elementKind):
  """The lining of the named cave (or every cave, for true): its vertices or faces."""
  mesh = sceneObject.data
  if elementKind == "vertices":
    mask = numpy.zeros(len(mesh.vertices), dtype=bool)
    for name in requireCaveNames(sceneObject, value):
      mask |= attributeValues(mesh, vertexTagPrefix + name) == liningTag
    return mask
  mask = numpy.zeros(len(mesh.polygons), dtype=bool)
  for name in requireCaveNames(sceneObject, value):
    mask |= faceTags(sceneObject, name) == liningFaceTag
  return mask


def namedCaves(selector):
  """The caves a selector names anywhere in it (True for every cave)."""
  if not isinstance(selector, dict):
    return set()
  named = set()
  for key, value in selector.items():
    if key == "cave":
      named.add(value)
    elif key in ("and", "or") and isinstance(value, list):
      for part in value:
        named |= namedCaves(part)
    elif key == "not":
      named |= namedCaves(value)
  return named


def surfaceMask(sceneObject, selector, mask):
  """A surface tool's faces: the lining of every cave the selector does not name left out."""
  if not holdsCaves(sceneObject):
    return mask
  named = namedCaves(selector)
  if True in named:
    return mask
  kept = mask.copy()
  for name in caves(sceneObject):
    if name not in named:
      kept &= faceTags(sceneObject, name) != liningFaceTag
  return kept


def requireSurfaceSelection(sceneObject, selector, mask):
  """surfaceMask, refusing a selection the lining took up all of."""
  kept = surfaceMask(sceneObject, selector, mask)
  if mask.any() and not kept.any():
    raise ValueError(f"Selector {selector!r} picks only the lining of caves of '{sceneObject.name}', which surface tools leave out unless the selector names the cave ({{\"cave\": name}})")
  return kept


def refuseLiningMapping(sceneObject, selector, action):
  """Refuse a mapping or material set on a cave's lining: its definition sets them, and every cut sets them again."""
  named = namedCaves(selector)
  if holdsCaves(sceneObject) and named:
    raise ValueError(f"A cave's lining takes its materials and mapping from its definition (wallMaterial, floorMaterial, worldUnitsPerRepeat), set again on every cut; change them with editCave rather than {action} on {sorted(map(str, named))}")


def keepStroke(sceneObject, selector, mask, tool, arguments):
  """Keep a surface stroke that reached a cave's lining with that cave, so every later cut of it paints its lining again."""
  if not holdsCaves(sceneObject):
    return
  named = namedCaves(selector)
  known = caves(sceneObject)
  names = sorted(known) if True in named else sorted(name for name in named if name in known)
  touched = [name for name in names if (mask & (faceTags(sceneObject, name) == liningFaceTag)).any()]
  for name in touched:
    known[name]["paint"].append({"tool": tool, "arguments": arguments})
  if touched:
    writeCaves(sceneObject, known)
