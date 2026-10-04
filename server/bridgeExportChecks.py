"""Checks before a zone export, writing nothing and changing nothing. Failures stop an export: what no zone file can hold, and for a
game export what it must have decided (blockout, swimming, the zone row's values, zone line targets, containment). Findings are for an
artist to look at: texture coverage (the base material showing where no surfacing layer covers a face, ground borders without a
transition strip, stretched or collapsed texture coordinates, faces wound against the rest of their surface), and for a test export
what a game export would still refuse. Each names the object, material, image, and face count, with where the faces lie. The coverage
view draws the same face statuses. Runs under Blender's Python."""
import os

import bpy
import mathutils
import numpy

import bridgeAuthoring
import bridgeBoundaries
import bridgeCaves
import bridgeCommands
import bridgeExport
import bridgeHousing
import bridgeMeshAccess
import bridgeSurfacing
import bridgeSwim
import bridgeViews
from bridgeState import state
from playerScale import walkableNormalZ

exportPurposes = ("test", "game")
# A face's texture is stretched where a texel lies more than this many times longer one way than the other in the world, and stretched
# or squeezed where its world units per repeat differ from its material's usual (the median over the material's exported area) by more
# than this factor. Ratios are compared at stretchDigits, so a mapping laid at exactly this factor (a transition strip half as wide as
# its repeat) does not flicker over it with float noise.
stretchFactor = 2.0
stretchDigits = 3
zeroTextureArea = 1e-12
degenerateArea = 1e-9
locationsShown = 8
gameViewKeys = ("fogOn", "minClip", "maxClip", "sky")
gameFogKeys = ("fogStart", "fogEnd", "fogDensity")
gamePlayerKeys = ("safePoint", "underworld")
textureNodeNames = (bridgeSurfacing.diffuseNodeName, bridgeSurfacing.normalNodeName, bridgeSurfacing.environmentNodeName, bridgeSurfacing.secondDiffuseNodeName)
# Coverage statuses, most pressing first: a face draws in the first that holds for it.
coverageColors = {
  "error": (0.02, 0.02, 0.02), "zeroTexture": (0.9, 0.05, 0.05), "blockout": (0.45, 0.28, 0.12), "stretch": (1.0, 0.88, 0.0),
  "base": (1.0, 0.5, 0.0), "border": (0.95, 0.1, 0.85), "ok": (0.62, 0.62, 0.6),
}
coverageLegend = {
  "error": "black: cannot export (no material, a material export cannot read, a bad image, no texture coordinates)",
  "zeroTexture": "red: zero texture area", "blockout": "brown: blockout material", "stretch": "yellow: texture stretched or squeezed",
  "base": "orange: base material showing", "border": "magenta: ground border without a transition strip", "ok": "grey: ok",
  "back": "blue: a face seen from its back in this view",
}
statusNames = tuple(coverageColors)
backFaceColor = (0.15, 0.4, 1.0)
# Coverage light keeps half its strength facing away from the layout light, so a shaded grey never reads as an error's black.
coverageAmbient = 0.5
coverageAttributeName = "zonewrightCoverage"


def requirePurpose(purpose):
  if purpose not in exportPurposes:
    raise ValueError(f"purpose is one of {list(exportPurposes)}, got {purpose!r}")


def isPowerOfTwo(value):
  return value > 0 and value & (value - 1) == 0


def isZonewrightMaterial(material):
  return material is not None and (bridgeMeshAccess.cutoutProperty in material or bridgeMeshAccess.liquidProperty in material)


def isBlockout(material):
  return isZonewrightMaterial(material) and bool(material.get(bridgeSurfacing.blockoutPropertyName))


def isGround(material):
  """A material ground borders run between: made by createMaterial, opaque, and neither blockout nor a transition."""
  return (
    material is not None and bridgeMeshAccess.cutoutProperty in material and not material[bridgeMeshAccess.cutoutProperty]
    and not material.get(bridgeSurfacing.blockoutPropertyName) and not material.get(bridgeSurfacing.transitionPropertyName)
  )


class ImageChecks:
  """What stops each image going into a zone archive, and the DDS name the archive stores it under."""

  def __init__(self):
    self.blendDrive = os.path.splitdrive(bpy.data.filepath)[0].lower() if bpy.data.filepath else None
    self.records = {}

  def record(self, image):
    if image.name not in self.records:
      self.records[image.name] = self.inspect(image)
    return self.records[image.name]

  def inspect(self, image):
    named = bridgeExport.imagePath(image) if image.filepath else image.name
    if image.packed_file is not None:
      return {"path": named, "problem": "image packed into the .blend", "ddsName": None}
    if image.source != "FILE":
      return {"path": named, "problem": f"image not a file on disk (its source is {image.source.lower()})", "ddsName": None}
    path = bridgeExport.imagePath(image)
    record = {"path": path, "problem": None, "ddsName": None}
    if not os.path.isfile(path):
      return record | {"problem": "image missing"}
    if self.blendDrive is not None and image.library is None and os.path.splitdrive(path)[0].lower() != self.blendDrive:
      return record | {"problem": "image on another drive than the .blend"}
    with open(path, "rb") as source:
      isDDS = source.read(4) == b"DDS "
    name = os.path.basename(path).lower()
    if isDDS and not name.endswith(".dds"):
      return record | {"problem": "DDS data under another extension"}
    width, height = image.size
    if not isDDS and not (isPowerOfTwo(width) and isPowerOfTwo(height)):
      return record | {"problem": "image sides not powers of two", "size": [width, height]}
    return record | {"ddsName": name if isDDS else os.path.splitext(name)[0] + ".dds"}

  def clashing(self):
    """The DDS names more than one recorded image file would be stored under, each with those files."""
    stored = {}
    for record in self.records.values():
      if record["ddsName"] is not None:
        stored.setdefault(record["ddsName"], set()).add(record["path"])
    return {name: sorted(paths) for name, paths in stored.items() if len(paths) > 1}


def materialImages(material):
  """The images of a zonewright material's texture nodes, None for a node without one."""
  nodes = material.node_tree.nodes
  return [nodes[name].image for name in textureNodeNames if nodes.get(name) is not None]


def materialProblems(material, images, clashing):
  """What stops a material exporting: [(problem, fields naming the image)]."""
  if not isZonewrightMaterial(material):
    return [("material not made by createMaterial or createLiquidMaterial", {})]
  nodes = material.node_tree.nodes
  problems = [] if nodes.get(bridgeSurfacing.diffuseNodeName) is not None else [("no diffuse texture", {})]
  for image in materialImages(material):
    if image is None:
      problems.append(("texture node without an image", {}))
      continue
    record = images.record(image)
    fields = {"image": record["path"]} | ({"size": record["size"]} if "size" in record else {})
    if record["problem"] is not None:
      problems.append((record["problem"], fields))
    elif record["ddsName"] in clashing:
      problems.append(("images share a DDS name", fields | {"ddsName": record["ddsName"], "images": clashing[record["ddsName"]]}))
  if material.get(bridgeMeshAccess.cutoutProperty) and nodes.get(bridgeSurfacing.normalNodeName) is not None:
    problems.append(("cutout with a normal map", {}))
  return problems


def usedMaterials(data):
  """The materials an exported mesh's faces use, None for faces without one."""
  return [(data["materials"] + [None])[slot] for slot in numpy.unique(numpy.minimum(data["materialIndices"], len(data["materials"])))]


def readArray(collection, attribute, width=1, dtype=numpy.int64):
  values = numpy.empty(len(collection) * width, dtype=dtype)
  collection.foreach_get(attribute, values)
  return values.reshape(-1, width) if width > 1 else values


def readPart(part, depsgraph):
  """An exported mesh's evaluated faces in its own space, with which faces share each edge."""
  evaluated = part.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    data = {
      "positions": readArray(mesh.vertices, "co", 3, numpy.float64), "loopVertices": readArray(mesh.loops, "vertex_index"),
      "loopEdges": readArray(mesh.loops, "edge_index"), "loopStarts": readArray(mesh.polygons, "loop_start"),
      "loopTotals": readArray(mesh.polygons, "loop_total"), "materialIndices": readArray(mesh.polygons, "material_index"),
      "edges": readArray(mesh.edges, "vertices", 2), "triangleLoops": readArray(mesh.loop_triangles, "loops", 3),
      "trianglePolygons": readArray(mesh.loop_triangles, "polygon_index"),
      "uvs": readArray(mesh.uv_layers.active.data, "uv", 2, numpy.float64) if mesh.uv_layers.active is not None else None,
      "cornerNormals": readArray(mesh.corner_normals, "vector", 3, numpy.float64),
      "materials": [slot.material for slot in evaluated.material_slots],
    }
  finally:
    evaluated.to_mesh_clear()
  faceCount = len(data["loopStarts"])
  loopFaces = numpy.repeat(numpy.arange(faceCount), data["loopTotals"])
  edgeUses = numpy.bincount(data["loopEdges"], minlength=len(data["edges"]))
  order = numpy.argsort(data["loopEdges"], kind="stable")
  firstUses = numpy.cumsum(edgeUses) - edgeUses
  shared = numpy.flatnonzero(edgeUses == 2)
  loopA, loopB = order[firstUses[shared]], order[firstUses[shared] + 1]
  forward = data["loopVertices"] == data["edges"][data["loopEdges"], 0]
  return data | {
    "sharedEdges": shared, "faceA": loopFaces[loopA], "faceB": loopFaces[loopB], "consistent": forward[loopA] != forward[loopB],
    "openFaces": numpy.bincount(loopFaces, weights=(edgeUses[data["loopEdges"]] != 2), minlength=faceCount) > 0,
  }


def textureScales(data, corners, texels):
  """Per triangle: world units per texture repeat (the square root of the world area one repeat covers), how many times longer a texel
  lies in the world one way than the other (texels: each triangle's texture width and height in pixels), and the texture area."""
  textured = data["uvs"][data["triangleLoops"]]
  firstUV, secondUV = textured[:, 1] - textured[:, 0], textured[:, 2] - textured[:, 0]
  firstEdge, secondEdge = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
  determinant = firstUV[:, 0] * secondUV[:, 1] - firstUV[:, 1] * secondUV[:, 0]
  safe = numpy.where(determinant == 0, 1.0, determinant)[:, None]
  alongU = (firstEdge * secondUV[:, 1:2] - secondEdge * firstUV[:, 1:2]) / safe / texels[:, 0:1]
  alongV = (secondEdge * firstUV[:, 0:1] - firstEdge * secondUV[:, 0:1]) / safe / texels[:, 1:2]
  uu, uv, vv = (alongU * alongU).sum(1), (alongU * alongV).sum(1), (alongV * alongV).sum(1)
  half = (uu + vv) / 2
  spread = numpy.sqrt(numpy.maximum(half * half - (uu * vv - uv * uv), 0))
  elongation = numpy.sqrt((half + spread) / numpy.maximum(half - spread, 1e-30))
  repeat = numpy.sqrt(numpy.linalg.norm(numpy.cross(alongU, alongV), axis=1) * texels[:, 0] * texels[:, 1])
  return repeat, elongation, numpy.abs(determinant) / 2


def texturePixels(material):
  """A material's diffuse texture's width and height in pixels, or None when it has no readable one."""
  node = material.node_tree.nodes.get(bridgeSurfacing.diffuseNodeName) if isZonewrightMaterial(material) else None
  if node is None or node.image is None or min(node.image.size) <= 0:
    return None
  return tuple(node.image.size)


def weightedMedian(values, weights):
  order = numpy.argsort(values)
  cumulative = numpy.cumsum(weights[order])
  return float(values[order][numpy.searchsorted(cumulative, cumulative[-1] / 2)])


def windingAgainst(data, corners, crosses, isTerrain):
  """Faces wound against the rest of their connected surface, which shows its back where they show their front: a closed surface
  faces out, a terrain sheet up, and any other sheet the way most of its area faces."""
  faceCount = len(data["loopStarts"])
  first = numpy.concatenate([data["faceA"], data["faceB"]])
  second = numpy.concatenate([data["faceB"], data["faceA"]])
  signs = numpy.where(numpy.concatenate([data["consistent"], data["consistent"]]), 1, -1)
  order = numpy.argsort(first, kind="stable")
  starts = numpy.concatenate([[0], numpy.cumsum(numpy.bincount(first, minlength=faceCount))]).tolist()
  neighbours, steps = second[order].tolist(), signs[order].tolist()
  orientation, piece, pieces = [0] * faceCount, [0] * faceCount, 0
  for seed in range(faceCount):
    if orientation[seed]:
      continue
    orientation[seed], piece[seed], stack = 1, pieces, [seed]
    while stack:
      face = stack.pop()
      for index in range(starts[face], starts[face + 1]):
        other = neighbours[index]
        if not orientation[other]:
          orientation[other], piece[other] = orientation[face] * steps[index], pieces
          stack.append(other)
    pieces += 1
  orientation, piece = numpy.array(orientation, dtype=numpy.float64), numpy.array(piece)
  polygons = data["trianglePolygons"]
  faceVolume = numpy.bincount(polygons, weights=(corners[:, 0] * crosses).sum(1) / 6, minlength=faceCount)
  faceUp = numpy.bincount(polygons, weights=crosses[:, 2], minlength=faceCount)
  faceArea = numpy.bincount(polygons, weights=numpy.linalg.norm(crosses, axis=1), minlength=faceCount)
  closed = numpy.bincount(piece, weights=data["openFaces"].astype(numpy.float64), minlength=pieces) == 0
  volume = numpy.bincount(piece, weights=orientation * faceVolume, minlength=pieces)
  sheet = numpy.bincount(piece, weights=orientation * (faceUp if isTerrain else faceArea), minlength=pieces)
  measure = numpy.where(closed & (volume != 0), volume, sheet)
  return orientation * numpy.where(measure < 0, -1, 1)[piece] < 0


def pieces(data, mask, centers, areas):
  """The connected pieces of the masked faces, largest first, each its center and face count; and how many there are."""
  faces = numpy.flatnonzero(mask)
  if not len(faces):
    return [], 0
  joined = mask[data["faceA"]] & mask[data["faceB"]]
  component = bridgeAuthoring.faceComponents(data["faceA"][joined], data["faceB"][joined], len(mask))
  labels, inverse, counts = numpy.unique(component[faces], return_inverse=True, return_counts=True)
  weights = areas[faces] + 1e-9
  totals = numpy.bincount(inverse, weights=weights)
  center = numpy.stack([numpy.bincount(inverse, weights=centers[faces, axis] * weights) for axis in range(3)], axis=1) / totals[:, None]
  order = numpy.argsort(-counts, kind="stable")[:locationsShown]
  return [{"center": [round(float(value), 1) for value in center[index]], "faces": int(counts[index])} for index in order], len(labels)


class Entries:
  """Failures or findings gathered over every placed mesh, merged by what they are and where: face counts add up, locations gather."""

  def __init__(self):
    self.entries = {}

  def add(self, label, key, owner, part, fields, faces=0, at=(), pieceCount=0, worst=None, stretches=(), length=0.0):
    entryKey = (label, part) + tuple(key)
    entry = self.entries.setdefault(entryKey, {"label": label, "object": part, "fields": fields, "owners": set(), "faces": 0, "at": [], "pieces": 0, "worst": None, "stretches": [], "length": 0.0})
    entry["owners"].add(owner)
    entry["faces"] += faces
    entry["at"] += list(at)
    entry["pieces"] += pieceCount
    entry["stretches"] += list(stretches)
    entry["length"] += length
    if worst is not None:
      entry["worst"] = worst if entry["worst"] is None else max(entry["worst"], worst)

  def listed(self, kind):
    listed = []
    for entry in sorted(self.entries.values(), key=lambda entry: (entry["label"], entry["object"], -entry["faces"])):
      record = {kind: entry["label"], "object": entry["object"]} | entry["fields"]
      others = sorted(entry["owners"] - {entry["object"]})
      if others:
        record["placedBy"] = others[:locationsShown]
        record["placements"] = len(entry["owners"])
      if entry["faces"]:
        record["faces"] = entry["faces"]
        record["at"] = sorted(entry["at"], key=lambda place: -place["faces"])[:locationsShown]
        record["pieces"] = entry["pieces"]
      if entry["worst"] is not None:
        record["worst"] = round(entry["worst"], 2)
      if entry["stretches"]:
        record["length"] = round(entry["length"], 1)
        record["stretches"] = sorted(entry["stretches"], key=lambda stretch: -stretch["length"])[:locationsShown]
        record["stretchCount"] = len(entry["stretches"])
      listed.append(record)
    return listed


def placedParts(shipped):
  """Every exported mesh as placed: (shipped object, mesh, world matrix, role)."""
  return [(sceneObject, part, matrix, role) for sceneObject, role in shipped if role in ("terrain", "mesh", "instance") for part, matrix in bridgeMeshAccess.objectParts(sceneObject)]


def surveyFaces(shipped):
  """Every exported face's coverage status, with the failures and findings behind them: face failures, coverage findings, and blockout."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  readParts, windings = {}, {}
  failures, findings, blockouts = Entries(), Entries(), Entries()
  placed = []
  for sceneObject, part, matrix, role in placedParts(shipped):
    if part.name not in readParts:
      readParts[part.name] = readPart(part, depsgraph)
    data = readParts[part.name]
    if not len(data["loopStarts"]):
      failures.add("no faces", (), sceneObject.name, part.name, {})
      continue
    matrix = numpy.array(matrix)
    world = data["positions"] @ matrix[:3, :3].T + matrix[:3, 3]
    corners = world[data["loopVertices"][data["triangleLoops"]]]
    crosses = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    faceCount = len(data["loopStarts"])
    areas = numpy.bincount(data["trianglePolygons"], weights=numpy.linalg.norm(crosses, axis=1) / 2, minlength=faceCount)
    centers = numpy.add.reduceat(world[data["loopVertices"]], data["loopStarts"]) / data["loopTotals"][:, None]
    windingKey = (part.name, numpy.linalg.det(matrix[:3, :3]) < 0)
    if windingKey not in windings:
      windings[windingKey] = windingAgainst(data, corners, crosses, role == "terrain")
    placed.append({"owner": sceneObject, "part": part, "matrix": matrix, "role": role, "data": data, "corners": corners, "crosses": crosses, "areas": areas, "centers": centers, "back": windings[windingKey]})
  images = ImageChecks()
  materials = {material.name: material for entry in placed for material in usedMaterials(entry["data"]) if material is not None}
  for material in materials.values():
    if isZonewrightMaterial(material):
      for image in materialImages(material):
        if image is not None:
          images.record(image)
  clashing = images.clashing()
  problemsOf = {name: materialProblems(material, images, clashing) for name, material in materials.items()}
  repeats = {}
  for entry in placed:
    data = entry["data"]
    if data["uvs"] is None:
      continue
    pixels = [texturePixels(material) if material is not None else None for material in data["materials"]] + [None]
    slots = numpy.minimum(data["materialIndices"], len(data["materials"]))[data["trianglePolygons"]]
    texels = numpy.array([pixels[slot] or (1, 1) for slot in range(len(pixels))], dtype=numpy.float64)[slots]
    repeat, elongation, textureArea = textureScales(data, entry["corners"], texels)
    triangleArea = numpy.linalg.norm(entry["crosses"], axis=1) / 2
    measured = (triangleArea > degenerateArea) & (textureArea > zeroTextureArea) & numpy.array([pixels[slot] is not None for slot in range(len(pixels))])[slots]
    entry["scales"] = (repeat, elongation, textureArea, triangleArea, measured)
    for slot in numpy.unique(slots[measured]):
      pick = measured & (slots == slot)
      repeats.setdefault(data["materials"][slot].name, []).append((repeat[pick], triangleArea[pick]))
  usual = {name: weightedMedian(numpy.concatenate([part[0] for part in parts]), numpy.concatenate([part[1] for part in parts])) for name, parts in repeats.items()}
  counts = dict.fromkeys(statusNames, 0)
  for entry in placed:
    entry["statuses"] = placedStatuses(entry, problemsOf, usual, failures, findings, blockouts)
    for index, name in enumerate(statusNames):
      counts[name] += int((entry["statuses"] == index).sum())
    back = entry["back"]
    if back.any():
      at, pieceCount = pieces(entry["data"], back, entry["centers"], entry["areas"])
      findings.add("back faces", (), entry["owner"].name, entry["part"].name, {}, int(back.sum()), at, pieceCount)
  counts["back"] = int(sum(entry["back"].sum() for entry in placed))
  return placed, failures.listed("failure"), findings.listed("finding"), blockouts, counts


def placedStatuses(entry, problemsOf, usual, failures, findings, blockouts):
  """Each face's coverage status (an index into statusNames) for one placed mesh, recording why in the failures and findings."""
  data, owner, part = entry["data"], entry["owner"].name, entry["part"].name
  faceCount = len(data["loopStarts"])
  centers, areas = entry["centers"], entry["areas"]
  materials = data["materials"]
  slotOf = numpy.minimum(data["materialIndices"], len(materials))
  slotMaterials = materials + [None]

  def record(entries, label, mask, fields, **extra):
    at, pieceCount = pieces(data, mask, centers, areas)
    entries.add(label, tuple(str(value) for value in fields.values()), owner, part, fields, int(mask.sum()), at, pieceCount, **extra)

  error = numpy.zeros(faceCount, dtype=bool)
  blockout = numpy.zeros(faceCount, dtype=bool)
  isWater = bridgeMeshAccess.waterProperty in entry["part"]
  for slot in numpy.unique(slotOf):
    mask = slotOf == slot
    material = slotMaterials[slot]
    if material is None:
      record(failures, "faces without a material", mask, {})
      error |= mask
      continue
    for problem, fields in problemsOf[material.name]:
      record(failures, problem, mask, {"material": material.name} | fields)
      error |= mask
    if bridgeSurfacing.liquidOf(material) is not None and not isWater:
      record(failures, "liquid material off a water body", mask, {"material": material.name})
      error |= mask
    if isBlockout(material):
      record(blockouts, "blockout", mask, {"material": material.name})
      blockout |= mask
  zero = numpy.zeros(faceCount, dtype=bool)
  stretch = numpy.zeros(faceCount, dtype=bool)
  if data["uvs"] is None:
    record(failures, "no texture coordinates", numpy.ones(faceCount, dtype=bool), {})
    error[:] = True
  else:
    repeat, elongation, textureArea, triangleArea, measured = entry["scales"]
    polygons = data["trianglePolygons"]
    slots = slotOf[polygons]
    flat = (triangleArea > degenerateArea) & (textureArea <= zeroTextureArea)
    zero[polygons[flat]] = True
    worst = numpy.zeros(faceCount)
    for slot in numpy.unique(slots[measured]):
      material = slotMaterials[slot]
      pick = measured & (slots == slot)
      ratio = numpy.maximum.reduce([elongation[pick], repeat[pick] / usual[material.name], usual[material.name] / repeat[pick]])
      numpy.maximum.at(worst, polygons[pick], ratio)
      faces = numpy.zeros(faceCount, dtype=bool)
      faces[polygons[pick][ratio.round(stretchDigits) > stretchFactor]] = True
      if faces.any():
        record(findings, "texture stretched or squeezed", faces, {"material": material.name, "usualRepeat": round(usual[material.name], 2)}, worst=float(worst[faces].max()))
      stretch |= faces
    for slot in numpy.unique(slotOf[zero]):
      material = slotMaterials[slot]
      record(findings, "zero texture area", zero & (slotOf == slot), {"material": None if material is None else material.name})
  base = numpy.zeros(faceCount, dtype=bool)
  deciders = bridgeAuthoring.shownSurface(entry["part"])[1] if bridgeMeshAccess.surfaceLayers(entry["part"]) else None
  if deciders is not None and len(deciders) != faceCount:
    failures.add("surfacing layers unlike the exported faces", (), owner, part, {"message": f"'{part}' has modifiers that change its faces, so its surfacing layers cannot be checked against what it exports; apply or remove them"})
  elif deciders is not None:
    base = deciders == -1
    for slot in numpy.unique(slotOf[base]):
      material = slotMaterials[slot]
      record(findings, "base material showing", base & (slotOf == slot), {"material": None if material is None else material.name})
  border = numpy.zeros(faceCount, dtype=bool)
  if entry["role"] == "terrain":
    border = groundBorders(entry, slotOf, slotMaterials, base, findings)
  statuses = numpy.full(faceCount, statusNames.index("ok"))
  for name, mask in (("border", border), ("base", base), ("stretch", stretch), ("blockout", blockout), ("zeroTexture", zero), ("error", error)):
    statuses[mask] = statusNames.index(name)
  return statuses


def roundedPoint(point):
  return [round(float(value), 1) for value in point]


def groundBorders(entry, slotOf, slotMaterials, base, findings):
  """Where two ground materials meet on a terrain mesh, with ground a player can walk on at least on one side and no transition strip
  between them, by pair of materials: the border's length and its stretches, each its center, bounds, and length; returns the faces
  along them."""
  data = entry["data"]
  names = [material.name if isGround(material) else None for material in slotMaterials]
  faceNames = numpy.array([names[slot] or "" for slot in slotOf], dtype=object)
  ground = (faceNames != "") & ~base
  faceCross = numpy.stack([numpy.bincount(data["trianglePolygons"], weights=entry["crosses"][:, axis], minlength=len(slotOf)) for axis in range(3)], axis=1)
  walkable = faceCross[:, 2] >= walkableNormalZ * numpy.linalg.norm(faceCross, axis=1)
  faceA, faceB = data["faceA"], data["faceB"]
  meets = ground[faceA] & ground[faceB] & (faceNames[faceA] != faceNames[faceB]) & (walkable[faceA] | walkable[faceB])
  faces = numpy.zeros(len(slotOf), dtype=bool)
  if not meets.any():
    return faces
  faces[faceA[meets]] = True
  faces[faceB[meets]] = True
  matrix = entry["matrix"]
  world = data["positions"] @ matrix[:3, :3].T + matrix[:3, 3]
  edges = data["edges"][data["sharedEdges"][meets]]
  pairs = numpy.array([" | ".join(sorted(pair)) for pair in zip(faceNames[faceA[meets]], faceNames[faceB[meets]])], dtype=object)
  for pair in sorted(set(pairs)):
    stretches = []
    for vertices, _, closed in bridgeMeshAccess.tracedChains(edges[pairs == pair], {}):
      points = world[vertices + vertices[:1] if closed else vertices]
      length = float(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1).sum())
      stretches.append({
        "center": roundedPoint(points.mean(axis=0)), "minimum": roundedPoint(points.min(axis=0)), "maximum": roundedPoint(points.max(axis=0)),
        "length": round(length, 1),
      })
    findings.add("border without a transition", (pair,), entry["owner"].name, entry["part"].name, {"materials": pair.split(" | ")}, stretches=stretches, length=sum(stretch["length"] for stretch in stretches))
  return faces


def bodyCenter(body):
  corners = numpy.array([list(body.matrix_world @ mathutils.Vector(corner)) for corner in body.bound_box])
  return [round(float(value), 1) for value in (corners.min(0) + corners.max(0)) / 2]


def objectLocation(sceneObject):
  return [round(float(value), 1) for value in sceneObject.matrix_world.translation]


def placementFailures(shipped):
  failures = []
  stems = {}
  for sceneObject, role in shipped:
    if role not in ("mesh", "instance"):
      continue
    if not bridgeExport.isOnePositiveScale(sceneObject):
      scale = tuple(round(component, 6) for component in sceneObject.matrix_world.to_scale())
      failures.append({"failure": "scale", "object": sceneObject.name, "at": objectLocation(sceneObject), "message": f"'{sceneObject.name}' is scaled {scale}; a placed model takes one positive scale"})
    if role == "instance":
      others = [member.name for member in bridgeExport.collectionMembers(sceneObject.instance_collection) if member.type != "MESH"]
      if others:
        failures.append({"failure": "collection holds more than meshes", "object": sceneObject.name, "at": objectLocation(sceneObject), "collection": sceneObject.instance_collection.name, "members": others})
    key = bridgeExport.modelKey(sceneObject, role)
    stems.setdefault(bridgeExport.modelStem(key), set()).add(key[1])
  for stem, names in sorted(stems.items()):
    if len(names) > 1 or not stem:
      failures.append({"failure": "model names collide", "models": sorted(names), "message": f"Models {sorted(names)} are named '{stem}' once lowercased to letters, digits, and underscores; give them distinct names"})
  return failures


def checkZoneExport(purpose):
  """Every failure that stops an export for its purpose and every finding to look at, each with where it is; plus what the export leaves
  out, the decisions it leaves to confirm, and the zone's faces by coverage status."""
  requirePurpose(purpose)
  bpy.context.view_layer.update()
  failures, findings = [], []
  if not bpy.data.filepath:
    failures.append({"failure": "never saved", "message": "Save the file (saveFile with a path): an export writes the zone as saved"})
  elif state.unsavedChanges:
    failures.append({"failure": "unsaved changes", "message": "Save the file (saveFile): an export writes the zone as saved"})
  shipped, excluded, classified = bridgeExport.classifyObjects()
  failures += classified + placementFailures(shipped)
  failures += [
    {"failure": "caves broken", "object": sceneObject.name, "message": problem}
    for sceneObject, role in shipped if sceneObject.type == "MESH" for problem in bridgeCaves.integrityProblems(sceneObject)
  ]
  _, faceFailures, faceFindings, blockouts, coverage = surveyFaces(shipped)
  failures += faceFailures
  findings += faceFindings
  for label, errors in (("swim volume", bridgeSwim.structuralErrors()), ("boundary", bridgeBoundaries.boundaryErrors()), ("zone line", bridgeBoundaries.zoneLineErrors())):
    failures += [{"failure": label} | error for error in errors]
  decisions = bridgeSwim.swimDecisions()
  try:
    bridgeHousing.collectHousing()
  except ValueError as error:
    failures.append({"failure": "housing", "message": str(error)})
  gapKey = "failure" if purpose == "game" else "finding"
  gaps = blockouts.listed(gapKey) + [{gapKey: f"swim {swimState}", "body": body.name, "at": bodyCenter(body)} for swimState, bodies in decisions.items() for body in bodies]
  gaps += [{gapKey: gap["gap"]} | {key: value for key, value in gap.items() if key != "gap"} for gap in zoneRowGaps() + bridgeBoundaries.zoneLineGaps()]
  toConfirm = bridgeExport.decisionsToConfirm(shipped)
  if purpose == "game":
    failures += [
      {
        "failure": "stale", "object": decision["object"], "staleCaves": decision["staleCaves"], "staleDefinedPasses": decision["staleDefinedPasses"],
        "message": f"The ground under caves {decision['staleCaves']} and defined passes {decision['staleDefinedPasses']} of '{decision['object']}' moved since they were made; regradeTerrain (or editCave) fits them to it",
      }
      for decision in toConfirm if decision["staleCaves"] or decision["staleDefinedPasses"]
    ]
    failures += gaps + [{"failure": "containment not checked", "message": "A game export must prove players cannot leave the play area except through zone lines; that needs reach mapping, which is not built yet, so no game export can be made"}]
  else:
    findings += gaps
  return {
    "purpose": purpose, "failures": failures, "findings": findings, "coverage": coverage, "excluded": excluded,
    "toConfirm": toConfirm, "swim": {swimState: [body.name for body in bodies] for swimState, bodies in decisions.items()},
    "boundaries": sorted(sceneObject.name for sceneObject, role in shipped if role == "boundary"), "zoneLines": [region["name"] for region in bridgeBoundaries.zoneLineRegions()],
  }


def zoneRowGaps():
  """What the zone row a game export writes still lacks: its view values, the safe point and underworld, and ground under the safe
  point above the underworld, where players arrive."""
  zone = bridgeCommands.readZoneProperties(bpy.context.scene)
  gaps = []
  missing = [key for key in gameViewKeys if key not in zone] + ([key for key in gameFogKeys if key not in zone] if zone.get("fogOn") else [])
  if missing:
    stating = "; sky \"none\" states that the zone draws none" if "sky" in missing else ""
    gaps.append({"gap": "view values missing", "missing": missing, "message": f"A game export writes the zone row's view values; set {missing} with setZoneProperties{stating}"})
  missing = [key for key in gamePlayerKeys if key not in zone]
  if missing:
    gaps.append({"gap": "safe point or underworld missing", "missing": missing, "message": f"A game export writes where players arrive and how far they may fall; set {missing} with setZoneProperties"})
  if "safePoint" in zone:
    x, y, z = zone["safePoint"][:3]
    floor = zone["underworld"] if "underworld" in zone else bridgeBoundaries.sceneHeightSpan()[0]
    if bridgeBoundaries.collisionSurfaces(boundaries=False).footingBelow(mathutils.Vector((x, y, z + 1)), z + 1 - floor) is None:
      gaps.append({"gap": "safe point over no ground", "at": [x, y, z], "message": f"No ground players stand on lies under the safe point {[x, y, z]} above {floor}; players arriving there would fall"})
  return gaps


def collectZoneExport(outputFolder, zoneName, purpose):
  """The checks, then, only when nothing fails, what the export writes (bridgeExport.collectZoneExport)."""
  report = checkZoneExport(purpose)
  return {"report": report, "collected": None if report["failures"] else bridgeExport.collectZoneExport(outputFolder, zoneName)}


def meshFromData(data, name):
  """A mesh of the faces readPart read, in the part's own space, shaded with the part's own normals."""
  mesh = bpy.data.meshes.new(name)
  mesh.vertices.add(len(data["positions"]))
  mesh.vertices.foreach_set("co", data["positions"].ravel())
  mesh.loops.add(len(data["loopVertices"]))
  mesh.loops.foreach_set("vertex_index", data["loopVertices"].astype(numpy.int32))
  mesh.polygons.add(len(data["loopStarts"]))
  mesh.polygons.foreach_set("loop_start", data["loopStarts"].astype(numpy.int32))
  mesh.update(calc_edges=True)
  if len(data["loopStarts"]):
    mesh.normals_split_custom_set(data["cornerNormals"])
  return mesh


def coverageMesh(data, statuses):
  """A copy of an exported mesh's evaluated faces, each colored by its coverage status."""
  mesh = meshFromData(data, bridgeViews.previewName + "Coverage")
  palette = numpy.array([(*coverageColors[name], 1.0) for name in statusNames], dtype=numpy.float32)
  mesh.attributes.new(coverageAttributeName, "FLOAT_COLOR", "FACE").data.foreach_set("color", palette[statuses].ravel())
  return mesh


def coverageMaterial():
  """Each face's status color lit from the layout light's direction, and blue where the face is seen from its back."""
  material = bpy.data.materials.new(bridgeViews.previewName + "Coverage")
  material.use_nodes = True
  tree = material.node_tree
  nodes, links = tree.nodes, tree.links
  nodes.clear()
  status = nodes.new("ShaderNodeAttribute")
  status.attribute_type = "GEOMETRY"
  status.attribute_name = coverageAttributeName
  geometry = nodes.new("ShaderNodeNewGeometry")
  color = bridgeSurfacing.mixColors(tree, status.outputs["Color"], (*backFaceColor, 1.0), geometry.outputs["Backfacing"])
  facing = nodes.new("ShaderNodeVectorMath")
  facing.operation = "DOT_PRODUCT"
  links.new(geometry.outputs["Normal"], facing.inputs[0])
  facing.inputs[1].default_value = bridgeViews.layoutLightDirection
  lit = nodes.new("ShaderNodeMath")
  lit.operation = "MAXIMUM"
  links.new(facing.outputs["Value"], lit.inputs[0])
  lit.inputs[1].default_value = 0.0
  shade = nodes.new("ShaderNodeMath")
  shade.operation = "MULTIPLY_ADD"
  links.new(lit.outputs["Value"], shade.inputs[0])
  shade.inputs[1].default_value = 1 - coverageAmbient
  shade.inputs[2].default_value = coverageAmbient
  shaded = nodes.new("ShaderNodeVectorMath")
  shaded.operation = "SCALE"
  links.new(color, shaded.inputs[0])
  links.new(shade.outputs["Value"], shaded.inputs["Scale"])
  emission = nodes.new("ShaderNodeEmission")
  links.new(shaded.outputs["Vector"], emission.inputs["Color"])
  output = nodes.new("ShaderNodeOutputMaterial")
  links.new(emission.outputs["Emission"], output.inputs["Surface"])
  return material


def drawCoverage(preview):
  """Replace the preview's linked objects with the exported faces in their coverage colors (what the preview made itself, such as the
  scale figure, stays); returns the zone's faces by status and the legend."""
  shipped, _, _ = bridgeExport.classifyObjects()
  placed, _, _, _, counts = surveyFaces(shipped)
  for linked in list(preview.scene.collection.objects):
    if linked not in preview.createdObjects:
      preview.scene.collection.objects.unlink(linked)
  material = coverageMaterial()
  preview.createdMaterials.append(material)
  for entry in placed:
    mesh = coverageMesh(entry["data"], entry["statuses"])
    mesh.materials.append(material)
    copy = preview.addObject(bpy.data.objects.new(bridgeViews.previewName + "Coverage", mesh))
    copy.matrix_world = mathutils.Matrix(entry["matrix"].tolist())
  return {"faces": counts, "legend": coverageLegend}


commands = {
  "checkZoneExport": (checkZoneExport, False),
  "collectZoneExport": (collectZoneExport, False),
}
