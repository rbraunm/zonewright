"""Authoring a zone the way an environment artist does: regions that say what an area is to become, surfacing layers painted by intent
and edited at their boundaries, and broad strokes that take an area back to start it again. Runs under Blender's Python."""
import json
import math

import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeEnvironment
import bridgeExport
import bridgeMeshAccess
import bridgeNoise
import bridgeObjects
import bridgePasses
import bridgeShaping
import bridgeSurfacing

regionCollectionName = "regions"
layerOrderProperty = bridgeMeshAccess.surfaceLayersProperty
baseAttributeName = "zonewrightSurfaceBase"
layerAttributePrefix = "zonewrightSurface:"
# Once a layer maps a transition its own way, the mesh's own UVs are kept here per face corner and the UV map is composed from them
# and the transition mappings, as materials are from the layers; each layer's transition mapping is kept under the prefix, not a
# number where the layer maps nothing of its own. Three-component vectors, so Blender does not take them for UV maps.
baseMappingName = "zonewrightSurfaceBaseUV"
transitionMappingPrefix = "zonewrightTransitionUV:"
uncovered = -1
surfaceOperations = ("grow", "shrink", "smooth", "clean")
# Snapped vertices whose faces would turn over or shrink below this share of their area go back, over up to this many rounds.
conformSliverShare = 0.1
conformRepairs = 4
# A vertex where a face around it turns more than this from their mean stays put: it sits on a crease (a cliff's edge), which
# sliding it would move, and a border there already runs on a modeled edge.
conformCreaseDegrees = 20
# A vertex this share of the mesh's edge length from an evened border already lies on it: it stays, and no edge crosses the border
# there, so conforming a border again leaves it as it is.
conformOnLineShare = 0.1
# A transition strip takes faces reaching this fraction past its width, so a contour cut at the width counts as inside it.
transitionTolerance = 1e-4
# A strip face's middle may lie this share of the width nearer or farther than its corners' distances say. Beyond it the distance does
# not run evenly across the face (it reaches over a wall's whole height with every corner on the border) and its mapping would smear.
transitionLinearity = 0.25
# Cleaning takes over small pieces, then pieces the first takeover left small, for up to this many rounds.
cleanRounds = 4
rebuildModes = ("surroundings", "height")
shapingChoices = ("keep", "reset", "rebuild")
# Rebuilding from the surroundings solves for smooth heights until the residual falls this far below where it started.
relaxTolerance = 1e-10


# Regions

def regionMesh(name, outline, bottom, top):
  outlineArray = bridgeMeshAccess.toArray(outline)
  if outlineArray.ndim != 2 or outlineArray.shape[1] != 2 or len(outlineArray) < 3:
    raise ValueError(f"A region outline is at least three [x, y] points, got {outline!r}")
  if not bottom < top:
    raise ValueError(f"A region's bottom must lie below its top, got {bottom} and {top}")
  sides = len(outlineArray)
  vertices = [(x, y, bottom) for x, y in outlineArray] + [(x, y, top) for x, y in outlineArray]
  faces = [tuple(reversed(range(sides))), tuple(range(sides, 2 * sides))] + [(index, (index + 1) % sides, sides + (index + 1) % sides, sides + index) for index in range(sides)]
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata(vertices, [], faces)
  mesh.validate()
  return mesh


def describeRegion(regionObject):
  outline, bottom, top = bridgeMeshAccess.regionShape(regionObject)
  x, y = outline[:, 0], outline[:, 1]
  area = abs(float(numpy.dot(x, numpy.roll(y, -1)) - numpy.dot(y, numpy.roll(x, -1)))) / 2
  return {"name": regionObject.name, "intent": regionObject[bridgeMeshAccess.regionIntentProperty], "outline": [[round(float(a), 2), round(float(b), 2)] for a, b in outline],
    "bottom": round(bottom, 2), "top": round(top, 2), "area": round(area)}


def createRegion(name, outline, bottom, top, intent):
  bridgeObjects.requireNewName(name)
  if not intent.strip():
    raise ValueError("A region needs its intent: what the area is to become")
  regionObject = bpy.data.objects.new(name, regionMesh(name, outline, bottom, top))
  regionObject[bridgeMeshAccess.regionIntentProperty] = intent.strip()
  regionObject.display_type = "WIRE"
  regionObject.hide_render = True
  bridgeObjects.targetCollection(regionCollectionName).objects.link(regionObject)
  return describeRegion(regionObject)


def editRegion(name, outline, bottom, top, intent):
  regionObject = bridgeMeshAccess.requireRegion(name)
  if outline is None and bottom is None and top is None and intent is None:
    raise ValueError("editRegion needs an outline, bottom, top, or intent")
  currentOutline, currentBottom, currentTop = bridgeMeshAccess.regionShape(regionObject)
  if outline is not None or bottom is not None or top is not None:
    oldMesh = regionObject.data
    regionObject.matrix_world = mathutils.Matrix.Identity(4)
    regionObject.data = regionMesh(name, outline if outline is not None else currentOutline.tolist(), currentBottom if bottom is None else bottom, currentTop if top is None else top)
    bpy.data.meshes.remove(oldMesh)
  if intent is not None:
    if not intent.strip():
      raise ValueError("A region needs its intent: what the area is to become")
    regionObject[bridgeMeshAccess.regionIntentProperty] = intent.strip()
  return describeRegion(regionObject)


def getRegions():
  bpy.context.view_layer.update()
  return {"regions": [describeRegion(sceneObject) for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" and bridgeMeshAccess.regionIntentProperty in sceneObject]}


# Surfacing layers

def requireLayer(sceneObject, name):
  layers = bridgeMeshAccess.surfaceLayers(sceneObject)
  if name not in [layer["name"] for layer in layers]:
    raise ValueError(f"'{sceneObject.name}' has no surfacing layer '{name}'; its layers: {[layer['name'] for layer in layers]}")
  return layers


def readFaceInts(mesh, attributeName):
  values = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
  mesh.attributes[attributeName].data.foreach_get("value", values)
  return values


def writeFaceInts(mesh, attributeName, values):
  mesh.attributes[attributeName].data.foreach_set("value", numpy.asarray(values, dtype=numpy.int32))


def materialSlot(sceneObject, materialName):
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  slot = next((index for index, existing in enumerate(sceneObject.material_slots) if existing.material == material), None)
  if slot is None:
    sceneObject.data.materials.append(material)
    slot = len(sceneObject.material_slots) - 1
  return slot


def shownSurface(sceneObject, belowPosition=None):
  """The material each face shows (the topmost unmuted layer covering it, else the material it had when the first layer was added)
  and the position in the layer order of the layer that decides it, -1 for the base; with belowPosition, as only the layers under that
  position would show it."""
  mesh = sceneObject.data
  materialIndices = readFaceInts(mesh, baseAttributeName)
  deciders = numpy.full(len(materialIndices), -1)
  for position, layer in enumerate(bridgeMeshAccess.surfaceLayers(sceneObject)[:belowPosition]):
    if not layer["muted"]:
      values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
      covered = values != uncovered
      materialIndices = numpy.where(covered, values, materialIndices)
      deciders = numpy.where(covered, position, deciders)
  return materialIndices, deciders


def readCornerVectors(mesh, attributeName):
  vectors = numpy.empty(len(mesh.loops) * 3)
  mesh.attributes[attributeName].data.foreach_get("vector", vectors)
  return vectors.reshape(-1, 3)


def writeCornerVectors(mesh, attributeName, vectors):
  mesh.attributes[attributeName].data.foreach_set("vector", numpy.asarray(vectors, dtype=numpy.float64).ravel())


def mappingAttributes(mesh):
  """The names of the corner mappings a layered mesh keeps besides its UV maps: its base mapping and its layers' transition mappings."""
  return [attribute.name for attribute in mesh.attributes if attribute.name == baseMappingName or attribute.name.startswith(transitionMappingPrefix)]


def compose(sceneObject):
  """Show each face's material, and once a layer maps a transition its own way, each face's mapping, as the layers decide them."""
  mesh = sceneObject.data
  shown, deciders = shownSurface(sceneObject)
  mesh.polygons.foreach_set("material_index", shown)
  if baseMappingName in mesh.attributes:
    loopTotals, _ = bridgeMeshAccess.faceLoops(sceneObject)
    loopDeciders = numpy.repeat(deciders, loopTotals)
    mapping = readCornerVectors(mesh, baseMappingName)
    for position, layer in enumerate(bridgeMeshAccess.surfaceLayers(sceneObject)):
      name = transitionMappingPrefix + layer["name"]
      if name in mesh.attributes:
        own = readCornerVectors(mesh, name)
        showing = (loopDeciders == position) & ~numpy.isnan(own[:, 0])
        mapping[showing] = own[showing]
    mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_set("uv", mapping[:, :2].ravel())
  mesh.update()
  return describeLayers(sceneObject)


def writeLayerValues(sceneObject, layer, values, replaced):
  """Write a layer's paint; the faces whose paint was replaced lose any transition mapping the layer held for them."""
  mesh = sceneObject.data
  writeFaceInts(mesh, layerAttributePrefix + layer, values)
  name = transitionMappingPrefix + layer
  if name in mesh.attributes and replaced.any():
    loopTotals, _ = bridgeMeshAccess.faceLoops(sceneObject)
    mapping = readCornerVectors(mesh, name)
    mapping[numpy.repeat(replaced, loopTotals)] = numpy.nan
    writeCornerVectors(mesh, name, mapping)


def describeLayers(sceneObject):
  mesh = sceneObject.data
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  described = []
  for layer in bridgeMeshAccess.surfaceLayers(sceneObject):
    values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
    covered = values[values != uncovered]
    described.append({"name": layer["name"], "muted": layer["muted"], "faces": {slotNames[slot]: int((covered == slot).sum()) for slot in numpy.unique(covered)}})
  return {"object": sceneObject.name, "layers": described}


def hasSurfaceLayers(sceneObject):
  return bool(bridgeMeshAccess.surfaceLayers(sceneObject))


def addSurfaceLayer(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = bridgeMeshAccess.surfaceLayers(sceneObject)
  if not name or name in [layer["name"] for layer in layers]:
    raise ValueError(f"'{objectName}' already has a surfacing layer '{name}'" if name else "A surfacing layer needs a name")
  mesh = sceneObject.data
  if not layers:
    materialIndices = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", materialIndices)
    mesh.attributes.new(baseAttributeName, "INT", "FACE")
    writeFaceInts(mesh, baseAttributeName, materialIndices)
  mesh.attributes.new(layerAttributePrefix + name, "INT", "FACE")
  writeFaceInts(mesh, layerAttributePrefix + name, numpy.full(len(mesh.polygons), uncovered))
  sceneObject[layerOrderProperty] = json.dumps(layers + [{"name": name, "muted": False}])
  return compose(sceneObject)


def setSurfaceLayer(objectName, name, muted, position):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = requireLayer(sceneObject, name)
  if muted is None and position is None:
    raise ValueError("setSurfaceLayer needs muted or position")
  layer = next(layer for layer in layers if layer["name"] == name)
  if muted is not None:
    layer["muted"] = bool(muted)
  if position is not None:
    if not 0 <= position < len(layers):
      raise ValueError(f"position is 0 (bottom) to {len(layers) - 1} (top), got {position}")
    layers.remove(layer)
    layers.insert(position, layer)
  sceneObject[layerOrderProperty] = json.dumps(layers)
  return compose(sceneObject)


def removeSurfaceLayer(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = [layer for layer in requireLayer(sceneObject, name) if layer["name"] != name]
  mesh = sceneObject.data
  for attributeName in (layerAttributePrefix + name, transitionMappingPrefix + name):
    if attributeName in mesh.attributes:
      mesh.attributes.remove(mesh.attributes[attributeName])
  sceneObject[layerOrderProperty] = json.dumps(layers)
  described = compose(sceneObject)
  if not layers:
    for attributeName in (baseAttributeName, baseMappingName):
      if attributeName in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[attributeName])
    del sceneObject[layerOrderProperty]
  return described


def faceCenters(sceneObject):
  with bridgeMeshAccess.shapedMesh(sceneObject) as mesh:
    centers = numpy.empty(len(mesh.polygons) * 3)
    mesh.polygons.foreach_get("center", centers)
  return bridgeMeshAccess.worldPositions(sceneObject, centers.reshape(-1, 3))


def nearestDistances(points, others):
  tree = mathutils.kdtree.KDTree(len(others))
  for index, point in enumerate(others):
    tree.insert(point, index)
  tree.balance()
  return numpy.array([tree.find(point)[2] for point in points])


def roughenedSelection(sceneObject, mask, edgeNoise):
  """The selection with its edge wandering as a painted edge does instead of following a circle, a line, or the grid: its border is
  moved along the surface by smooth noise, up to edgeNoise's amplitude, and each face near it takes the side of the moved border its
  middle lies on (its signed distance from the border, less the move along that distance's slope). Both sides of a narrow stroke move
  alike, so it bends as a whole instead of pinching apart, and no face farther than the amplitude from the border changes. The noise is
  laid out in plan, so it stays put when the ground is reshaped, and the same edgeNoise on the same selector picks the same faces again."""
  if edgeNoise is None or mask.all() or not mask.any():
    return mask
  unknown = sorted(set(edgeNoise) - {"featureSize", "amplitude", "seed"})
  if unknown or "featureSize" not in edgeNoise or edgeNoise.get("amplitude", 0) <= 0:
    raise ValueError(f"edgeNoise is {{featureSize, amplitude (positive), seed}}, got {edgeNoise!r}")
  centers, normals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  amplitude = edgeNoise["amplitude"]
  borderEdges, _, _ = bridgeMeshAccess.faceBorders(sceneObject, mask, ~mask)
  ends = bridgeMeshAccess.meshEdges(sceneObject.data)[borderEdges]
  border = bridgeMeshAccess.BorderDistance(positions[ends[:, 0]], positions[ends[:, 1]])
  reach = amplitude + float(numpy.linalg.norm(positions[ends[:, 1]] - positions[ends[:, 0]], axis=1).max())
  near = numpy.flatnonzero(nearestDistances(centers, positions[numpy.unique(ends)]) <= reach)
  plan = centers[near] * numpy.array([1.0, 1.0, 0.0])
  vectors = bridgeNoise.noiseVectors(bridgeNoise.noiseSamplePoints(plan, edgeNoise["featureSize"], edgeNoise.get("seed", 0))) / bridgeNoise.noiseSpread
  vectors -= (vectors * normals[near]).sum(axis=1, keepdims=True) * normals[near]
  lengths = numpy.linalg.norm(vectors, axis=1)
  moves = (amplitude * numpy.tanh(lengths) / numpy.maximum(lengths, 1e-12))[:, None] * vectors
  result = mask.copy()
  for face, move in zip(near, moves):
    distance, segment, along = border.nearest(centers[face])
    side = 1.0 if mask[face] else -1.0
    slope = side * (centers[face] - (border.starts[segment] + along * border.spans[segment])) / distance
    result[face] = side * distance - slope @ move > 0
  return result


def paintSurface(objectName, layer, material, selector, edgeNoise):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = roughenedSelection(sceneObject, mask, edgeNoise)
  slot = materialSlot(sceneObject, material)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  values[mask] = slot
  writeLayerValues(sceneObject, layer, values, mask)
  return {"painted": int(mask.sum())} | compose(sceneObject)


def eraseSurface(objectName, layer, selector, edgeNoise):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = roughenedSelection(sceneObject, mask, edgeNoise)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  erased = int((mask & (values != uncovered)).sum())
  values[mask] = uncovered
  writeLayerValues(sceneObject, layer, values, mask)
  return {"erased": erased} | compose(sceneObject)


def faceNeighbourPairs(mesh):
  """Pairs of faces sharing an edge, each pair once."""
  _, first, second = bridgeMeshAccess.sharedEdges(mesh)
  return first, second


def cornerNeighbourPairs(sceneObject):
  """Every pair of faces sharing a corner, once for each corner they share and each face with itself once per corner: (face, other)."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  order = numpy.argsort(loopVertices, kind="stable")
  sortedVertices = loopVertices[order]
  starts = numpy.flatnonzero(numpy.r_[True, sortedVertices[1:] != sortedVertices[:-1]])
  sizes = numpy.diff(numpy.r_[starts, len(order)])
  group = numpy.repeat(numpy.arange(len(starts)), sizes)
  counts = sizes[group]
  firsts = numpy.repeat(numpy.arange(len(order)), counts)
  seconds = starts[group][firsts] + numpy.arange(counts.sum()) - numpy.repeat(numpy.cumsum(counts) - counts, counts)
  return loopFaces[order[firsts]], loopFaces[order[seconds]]


def cleanedLayer(sceneObject, layer, within, minimumArea):
  """A layer's values once the specks and holes smaller than minimumArea that it or a layer beneath it shows are cleaned (cleanedValues
  on the surface as shown): where what lies beneath already shows the material taking over, this layer is lifted off; where its own
  paint takes over, reaching the face through faces it shows, its paint fills in; any other speck of its own is lifted off, so it never
  takes copies of other layers' materials. A speck one layer beneath paints amid another's is left for cleaning that layer."""
  mesh = sceneObject.data
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  layers = [entry["name"] for entry in bridgeMeshAccess.surfaceLayers(sceneObject)]
  position = layers.index(layer)
  shown, deciders = shownSurface(sceneObject)
  beneath, _ = shownSurface(sceneObject, position)
  cleaned = cleanedValues(mesh, shown, within & (deciders <= position), faceAreas(sceneObject, positions), minimumArea)
  taken = cleaned != shown
  reached = (deciders == position) & ~taken
  first, second = faceNeighbourPairs(mesh)
  while True:
    arriving = numpy.zeros(len(reached), dtype=bool)
    for source, target in ((first, second), (second, first)):
      arriving[target[reached[source] & taken[target] & ~reached[target] & (cleaned[source] == cleaned[target])]] = True
    if not arriving.any():
      break
    reached |= arriving
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  lifted = taken & (deciders == position) & ((beneath == cleaned) | ~reached)
  filled = taken & reached & (beneath != cleaned)
  return numpy.where(lifted, uncovered, numpy.where(filled, cleaned, values)), values


def editSurface(objectName, layer, operation, steps, selector, minimumArea):
  """Grow a layer's covered faces outward, shrink them inward, or smooth them (each face takes the value that holds most of the surface
  around its corners, itself included, which absorbs islands and rounds off notches and teeth a face or two across), within the
  selector; or clean it (cleanedLayer)."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if operation not in surfaceOperations:
    raise ValueError(f"operation is one of {list(surfaceOperations)}, got '{operation}'")
  if not isinstance(steps, int) or steps < 1:
    raise ValueError(f"steps is a positive whole number, got {steps!r}")
  if (operation == "clean") != (minimumArea is not None):
    raise ValueError("clean needs a minimumArea, and only clean takes one")
  if minimumArea is not None and minimumArea <= 0:
    raise ValueError(f"minimumArea must be positive, got {minimumArea}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  mesh = sceneObject.data
  if operation == "clean":
    shownBefore, _ = shownSurface(sceneObject)
    values, before = cleanedLayer(sceneObject, layer, within, minimumArea)
    writeLayerValues(sceneObject, layer, values, values != before)
    described = compose(sceneObject)
    return {"changed": int((shownSurface(sceneObject)[0] != shownBefore).sum())} | described
  first, second = faceNeighbourPairs(mesh)
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  before = values.copy()
  if operation == "smooth":
    positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
    areas = faceAreas(sceneObject, positions)
    faces, others = cornerNeighbourPairs(sceneObject)
  for _ in range(steps):
    updated = values.copy()
    if operation == "grow":
      for source, target in ((first, second), (second, first)):
        spreading = (values[source] != uncovered) & (values[target] == uncovered)
        updated[target[spreading]] = values[source[spreading]]
    elif operation == "shrink":
      for source, target in ((first, second), (second, first)):
        updated[target[(values[source] == uncovered) & (values[target] != uncovered)]] = uncovered
    else:
      candidates = numpy.unique(values)
      columns = numpy.searchsorted(candidates, values)
      votes = numpy.zeros((len(values), len(candidates)))
      numpy.add.at(votes, (faces, columns[others]), areas[others])
      # A tie keeps the face as it is.
      votes[numpy.arange(len(values)), columns] += 1e-6 * areas
      updated = candidates[votes.argmax(axis=1)]
    values = numpy.where(within, updated, values)
  writeLayerValues(sceneObject, layer, values, values != before)
  return {"changed": int((values != before).sum())} | compose(sceneObject)


def faceAreas(sceneObject, positions):
  return numpy.linalg.norm(bridgeMeshAccess.faceNormals(sceneObject, positions), axis=1)


def loopNeighbours(sceneObject):
  """For each face corner, the corner after it and the corner before it around the same face."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopStarts = numpy.repeat(numpy.cumsum(loopTotals) - loopTotals, loopTotals)
  repeated = numpy.repeat(loopTotals, loopTotals)
  offsets = numpy.arange(len(loopVertices)) - loopStarts
  return loopStarts + (offsets + 1) % repeated, loopStarts + (offsets - 1) % repeated


def movableVertices(sceneObject, within, positions):
  """Vertices every face around which lies within the face mask, off the mesh's open edge, and not on a crease."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  outside = numpy.bincount(loopVertices[~within[loopFaces]], minlength=len(positions))
  normals = bridgeMeshAccess.faceNormals(sceneObject, positions)
  vertexNormals = numpy.zeros_like(positions)
  numpy.add.at(vertexNormals, loopVertices, normals[loopFaces])
  vertexNormals /= numpy.maximum(numpy.linalg.norm(vertexNormals, axis=1, keepdims=True), 1e-12)
  units = normals / numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  flattest = numpy.ones(len(positions))
  numpy.minimum.at(flattest, loopVertices, (units[loopFaces] * vertexNormals[loopVertices]).sum(axis=1))
  return (outside == 0) & ~bridgeMeshAccess.boundaryVertexMask(sceneObject) & (flattest >= math.cos(math.radians(conformCreaseDegrees)))


def vectorArea(ring):
  """A closed loop's area as a vector along the axis it winds counterclockwise around."""
  return numpy.cross(ring, numpy.roll(ring, -1, axis=0)).sum(axis=0) / 2


def keepingArea(original, evened):
  """An evened loop pushed out (or in) evenly along its length until it encloses the area the original did: by Steiner's formula, a
  loop pushed out by d gains its perimeter times d plus pi d squared."""
  held = vectorArea(original)
  perimeter = float(numpy.linalg.norm(numpy.roll(evened, -1, axis=0) - evened, axis=1).sum())
  # A loop that winds around nothing seen from any side (a band around a pillar) has no area to keep.
  if numpy.linalg.norm(held) <= 1e-9 * perimeter * perimeter:
    return evened
  axis = held / numpy.linalg.norm(held)
  outward = numpy.cross(numpy.roll(evened, -1, axis=0) - numpy.roll(evened, 1, axis=0), axis)
  outward /= numpy.maximum(numpy.linalg.norm(outward, axis=1, keepdims=True), 1e-12)
  missing = float(numpy.linalg.norm(held)) - float(vectorArea(evened) @ axis)
  # A loop encloses at most a circle's area for its length, so the root is never below zero but for rounding.
  return evened + (2 * missing / (perimeter + math.sqrt(max(perimeter * perimeter + 4 * math.pi * missing, 0.0)))) * outward


def resampledChain(points, closed, step):
  """A chain's points (a closed one ending on its first) placed evenly along it, about `step` apart, keeping its ends."""
  arcs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1))])
  count = max(math.ceil(arcs[-1] / step), 3 if closed else 1)
  samples = numpy.linspace(0.0, arcs[-1], count + 1)
  return numpy.column_stack([numpy.interp(samples, arcs, points[:, axis]) for axis in range(3)])


def evenedChain(points, closed, spread):
  """A border chain (its points in order, a closed one ending on its first) evened out along its length: placed evenly along it, then
  each point taken to the mean of the chain around it, weighted by a Gaussian over `spread` units of arc, and that mean's own shortfall
  added back once (twice the mean less the mean of the mean), so saw teeth go while bends much wider than the spread stay where they
  are. An open chain runs on past each end as its own reflection through that end, so it keeps its ends, joining the borders beyond
  them, and runs straight into them; a closed one keeps the area it encloses."""
  original = points[:-1] if closed else points
  points = resampledChain(points, closed, min(spread, float(numpy.median(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1)))) / 4)
  ring = points[:-1] if closed else points
  spacing = float(numpy.linalg.norm(points[1] - points[0]))
  half = math.ceil(4 * spread / spacing)
  taps = numpy.exp(-0.5 * (numpy.arange(-half, half + 1) * spacing / spread) ** 2)
  taps /= taps.sum()

  def blurred(source):
    padded = numpy.pad(source, ((half, half), (0, 0)), mode="wrap") if closed else numpy.pad(source, ((half, half), (0, 0)), mode="reflect", reflect_type="odd")
    return numpy.column_stack([numpy.convolve(padded[:, axis], taps, mode="valid") for axis in range(3)])

  once = blurred(ring)
  evened = 2 * once - blurred(once)
  if closed:
    evened = keepingArea(original, evened)
    return numpy.vstack([evened, evened[:1]])
  return evened


def chainPieces(chain, positions, movable, closed, spread, shortestLoop):
  """A border chain split into stretches whose every edge runs between vertices off creases (evened, as evenedChain does) and the
  stretches between (kept as they are): (points, evened) in order. A closed chain wholly off creases is evened whole, unless its outline
  is shorter than shortestLoop: a speck too small for the smoothing to even, left as it is."""
  vertices = numpy.array(chain)
  points = positions[vertices]
  conformable = movable[vertices[:-1]] & movable[vertices[1:]]
  if closed and conformable.all():
    if numpy.linalg.norm(numpy.diff(points, axis=0), axis=1).sum() < shortestLoop:
      return [(points, False)]
    return [(evenedChain(points, True, spread), True)]
  if closed:
    shift = int(numpy.flatnonzero(~conformable)[0])
    vertices = numpy.concatenate([vertices[shift:-1], vertices[:shift + 1]])
    points = positions[vertices]
    conformable = numpy.roll(conformable, -shift)
  breaks = numpy.flatnonzero(conformable[1:] != conformable[:-1]) + 1
  pieces = []
  for first, last in zip(numpy.r_[0, breaks], numpy.r_[breaks, len(conformable)]):
    stretch = points[first:last + 1]
    pieces.append((evenedChain(stretch, False, spread) if conformable[first] else stretch, bool(conformable[first])))
  return pieces


class BorderCurves:
  """A surfacing layer's borders as polylines, each segment with the value on its left and right (seen from the side the faces face) and
  whether it was evened: chains of the mesh edges between faces of different value, evened (evenedChain) along stretches off creases."""

  def __init__(self, sceneObject, values, positions, movable, spread, shortestLoop):
    mesh = sceneObject.data
    edgeIndices, firstFaces, secondFaces = bridgeMeshAccess.sharedEdges(mesh)
    segments = bridgeMeshAccess.meshEdges(mesh)[edgeIndices[values[firstFaces] != values[secondFaces]]]
    loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
    nextLoops, _ = loopNeighbours(sceneObject)
    leftFaces = dict(zip(zip(loopVertices.tolist(), loopVertices[nextLoops].tolist()), numpy.repeat(numpy.arange(len(loopTotals)), loopTotals).tolist()))
    degree = numpy.bincount(segments.ravel(), minlength=len(positions))
    starts, ends, startTangents, endTangents, sides, evened, curves = [], [], [], [], [], [], []
    for chain in borderChains(segments):
      faces = leftFaces.get((chain[0], chain[1])), leftFaces.get((chain[1], chain[0]))
      if None in faces:
        raise ValueError(f"The faces of '{sceneObject.name}' on either side of the edge between vertices {chain[0]} and {chain[1]} wind the same way; recalculate its normals (cleanupMesh)")
      closed = len(chain) > 3 and chain[0] == chain[-1] and degree[chain[0]] == 2
      for points, isEvened in chainPieces(chain, positions, movable, closed, spread, shortestLoop):
        tangents = numpy.gradient(points, axis=0)
        tangents /= numpy.maximum(numpy.linalg.norm(tangents, axis=1, keepdims=True), 1e-12)
        starts.append(points[:-1])
        ends.append(points[1:])
        startTangents.append(tangents[:-1])
        endTangents.append(tangents[1:])
        count = len(points) - 1
        sides.append(numpy.tile([values[faces[0]], values[faces[1]]], (count, 1)))
        evened.append(numpy.full(count, isEvened))
        curves.append(numpy.full(count, len(curves)))
    self.starts, self.ends = numpy.concatenate(starts), numpy.concatenate(ends)
    self.startTangents, self.endTangents = numpy.concatenate(startTangents), numpy.concatenate(endTangents)
    self.sides, self.evened, self.curves = numpy.concatenate(sides), numpy.concatenate(evened), numpy.concatenate(curves)
    self.distance = bridgeMeshAccess.BorderDistance(self.starts, self.ends)
    evenedPoints = numpy.concatenate([self.starts[self.evened], self.ends[self.evened]]) if self.evened.any() else numpy.zeros((0, 3))
    self.evenedTree = mathutils.kdtree.KDTree(len(evenedPoints))
    for index, point in enumerate(evenedPoints):
      self.evenedTree.insert(point, index)
    self.evenedTree.balance()
    self.hasEvened = bool(len(evenedPoints))

  def nearEvened(self, points, reach):
    """Which points lie within reach of an evened curve's points."""
    if not self.hasEvened:
      return numpy.zeros(len(points), dtype=bool)
    return numpy.array([self.evenedTree.find(point)[2] <= reach for point in points], dtype=bool)

  def signedNearest(self, point, normal):
    """The distance from a point to the nearest border curve, positive on its left, and that curve's segment."""
    distance, segment, along = self.distance.nearest(point)
    nearest = self.starts[segment] + along * (self.ends[segment] - self.starts[segment])
    tangent = (1 - along) * self.startTangents[segment] + along * self.endTangents[segment]
    return (distance if (point - nearest) @ numpy.cross(normal, tangent) > 0 else -distance), segment


def borderSlides(curves, candidates, signed, segments, edges, positions, onLine):
  """Where the evened border crosses a mesh edge between two vertices off it and near the same curve, the point between them where it
  crosses; the nearer end slides onto it. Each vertex takes the nearest such point; returned with its partner along that edge and how
  far toward it it slides."""
  first, second = edges[:, 0], edges[:, 1]
  crossing = candidates[first] & candidates[second]
  crossing[crossing] = (curves.curves[segments[first[crossing]]] == curves.curves[segments[second[crossing]]]) & curves.evened[segments[first[crossing]]]
  crossing &= (numpy.abs(signed[first]) > onLine) & (numpy.abs(signed[second]) > onLine) & (numpy.sign(signed[first]) != numpy.sign(signed[second]))
  first, second = first[crossing], second[crossing]
  balance = signed[first] / (signed[first] - signed[second])
  nearFirst = balance <= 0.5
  movers = numpy.where(nearFirst, first, second)
  partners = numpy.where(nearFirst, second, first)
  fractions = numpy.where(nearFirst, balance, 1 - balance)
  distances = fractions * numpy.linalg.norm(positions[first] - positions[second], axis=1)
  order = numpy.lexsort((distances, movers))
  nearest = order[numpy.r_[True, movers[order][1:] != movers[order][:-1]]] if len(order) else order
  return movers[nearest], partners[nearest], fractions[nearest]


def slideAlongEdges(sceneObject, movers, partners, fractions):
  """Move each vertex the given fraction toward its partner in the mesh and in every shaping pass alike, so the passes stay as they
  were relative to each other, and carry each face's UVs along with the move."""
  mesh = sceneObject.data
  _, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  with bridgeMeshAccess.shapedMesh(sceneObject) as shown:
    before = numpy.empty(len(shown.vertices) * 3)
    shown.vertices.foreach_get("co", before)
  before = before.reshape(-1, 3)
  blocks = [mesh.vertices] + ([key.data for key in mesh.shape_keys.key_blocks] if mesh.shape_keys else [])
  for block in blocks:
    coordinates = numpy.empty(len(block) * 3)
    block.foreach_get("co", coordinates)
    coordinates = coordinates.reshape(-1, 3)
    coordinates[movers] += fractions[:, None] * (coordinates[partners] - coordinates[movers])
    block.foreach_set("co", coordinates.ravel())
  moves = numpy.zeros_like(before)
  moves[movers] = fractions[:, None] * (before[partners] - before[movers])
  nextLoops, previousLoops = loopNeighbours(sceneObject)
  moved = numpy.flatnonzero(numpy.isin(loopVertices, movers))
  here = before[loopVertices[moved]]
  spans = numpy.stack([before[loopVertices[nextLoops[moved]]] - here, before[loopVertices[previousLoops[moved]]] - here], axis=2)
  # The move in each face's own two spans, by least squares, carries that face's UVs as its mapping runs across it.
  weights = numpy.linalg.solve(numpy.einsum("lji,ljk->lik", spans, spans), numpy.einsum("lji,lj->li", spans, moves[loopVertices[moved]])[..., None])[..., 0]

  def carried(mapping):
    mappingSpans = numpy.stack([mapping[nextLoops[moved]] - mapping[moved], mapping[previousLoops[moved]] - mapping[moved]], axis=2)
    mapping[moved] += numpy.einsum("lik,lk->li", mappingSpans, weights)
    return mapping

  for uvLayer in mesh.uv_layers:
    uvs = numpy.empty(len(mesh.loops) * 2)
    uvLayer.data.foreach_get("uv", uvs)
    uvLayer.data.foreach_set("uv", carried(uvs.reshape(-1, 2)).ravel())
  for name in mappingAttributes(mesh):
    writeCornerVectors(mesh, name, carried(readCornerVectors(mesh, name)))
  mesh.update()


def conformSurfaceEdges(objectName, layer, smoothing, selector):
  """Bring a surfacing layer's edges onto the mesh's own edges along a smooth line: each border is evened out along its length over
  about `smoothing` units (evenedChain), the vertex nearest where a mesh edge crosses the evened line slides along that edge onto it
  (in every shaping pass alike, carrying UVs), and the faces near it take the side of the line their middle lies on, so the border runs
  on modeled edges without saw teeth, keeping the painted area."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if smoothing <= 0:
    raise ValueError(f"smoothing must be positive, got {smoothing}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  mesh = sceneObject.data
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  if len(numpy.unique(values[within])) < 2:
    raise ValueError(f"Layer '{layer}' of '{objectName}' holds one value everywhere {selector!r} picks, so it has no edge to conform there")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  edgeLength = float(numpy.median(numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)))
  movable = movableVertices(sceneObject, within, positions)
  curves = BorderCurves(sceneObject, values, positions, movable, smoothing, math.pi * smoothing)
  reach = 2 * (smoothing + edgeLength)
  candidates = movable & curves.nearEvened(positions, reach)
  signed = numpy.zeros(len(positions))
  segments = numpy.zeros(len(positions), dtype=numpy.int64)
  for vertex in numpy.flatnonzero(candidates):
    signed[vertex], segments[vertex] = curves.signedNearest(positions[vertex], normals[vertex])
  movers, partners, fractions = borderSlides(curves, candidates, signed, segments, edges, positions, conformOnLineShare * edgeLength)
  areasBefore = faceAreas(sceneObject, positions)
  normalsBefore = bridgeMeshAccess.faceNormals(sceneObject, positions)
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  kept = numpy.zeros(len(positions), dtype=bool)
  for _ in range(conformRepairs):
    active = ~kept[movers]
    updated = positions.copy()
    updated[movers[active]] += fractions[active, None] * (positions[partners[active]] - positions[movers[active]])
    normalsAfter = bridgeMeshAccess.faceNormals(sceneObject, updated)
    spoiled = ((normalsBefore * normalsAfter).sum(axis=1) <= 0) | (numpy.linalg.norm(normalsAfter, axis=1) < conformSliverShare * areasBefore)
    newlyKept = numpy.intersect1d(numpy.unique(loopVertices[spoiled[loopFaces]]), movers[active])
    if not len(newlyKept):
      break
    kept[newlyKept] = True
  active = ~kept[movers]
  movers, partners, fractions = movers[active], partners[active], fractions[active]
  moved = positions.copy()
  moved[movers] += fractions[:, None] * (positions[partners] - positions[movers])
  if len(movers):
    slideAlongEdges(sceneObject, movers, partners, fractions)
  onLine = numpy.zeros(len(positions), dtype=bool)
  onLine[movers] = True
  # A face on a crease keeps its value unless a corner of it moved: an evened line passing near the crease should not carry the
  # material across it.
  touchesMove = numpy.bincount(loopFaces, weights=onLine[loopVertices], minlength=len(loopTotals)) > 0
  allMovable = numpy.bincount(loopFaces, weights=~movable[loopVertices], minlength=len(loopTotals)) == 0
  centers = numpy.zeros((len(loopTotals), 3))
  numpy.add.at(centers, loopFaces, moved[loopVertices])
  centers /= loopTotals[:, None]
  faceDirections = bridgeMeshAccess.faceNormals(sceneObject, moved)
  faceDirections /= numpy.maximum(numpy.linalg.norm(faceDirections, axis=1, keepdims=True), 1e-12)
  deciding = within & (touchesMove | allMovable)
  deciding[deciding] = curves.nearEvened(centers[deciding], reach)
  updatedValues = values.copy()
  for face in numpy.flatnonzero(deciding):
    side, segment = curves.signedNearest(centers[face], faceDirections[face])
    left, right = curves.sides[segment]
    if curves.evened[segment] and values[face] in (left, right) and abs(side) > 1e-9:
      updatedValues[face] = left if side > 0 else right
  writeLayerValues(sceneObject, layer, updatedValues, updatedValues != values)
  return {
    "movedVertices": int(len(movers)), "keptInPlace": int(kept.sum()), "changedFaces": int((updatedValues != values).sum()),
  } | compose(sceneObject)


def faceComponents(first, second, count):
  """Each face's connected piece over the given neighbour pairs, named by the smallest face index in it."""
  component = numpy.arange(count)
  while True:
    lower = numpy.minimum(component[first], component[second])
    previous = component.copy()
    numpy.minimum.at(component, first, lower)
    numpy.minimum.at(component, second, lower)
    component = component[component]
    if (component == previous).all():
      return component


def cleanedValues(mesh, values, within, areas, minimumArea):
  """Values with every piece of one value smaller than minimumArea (a speck of a material, a hole in one) lying wholly inside the face
  mask taken over by the value it shares the most edges with."""
  first, second = faceNeighbourPairs(mesh)
  values = values.copy()
  for _ in range(cleanRounds):
    same = values[first] == values[second]
    component = faceComponents(first[same], second[same], len(values))
    pieceArea = numpy.bincount(component, weights=areas, minlength=len(values))
    pieceInside = numpy.bincount(component, weights=(~within).astype(numpy.float64), minlength=len(values)) == 0
    small = (pieceArea[component] < minimumArea) & pieceInside[component]
    border = ~same
    sides = numpy.r_[first[border], second[border]]
    across = numpy.r_[second[border], first[border]]
    taking = small[sides]
    if not taking.any():
      break
    pairs, counts = numpy.unique(numpy.column_stack([component[sides[taking]], values[across[taking]]]), axis=0, return_counts=True)
    order = numpy.lexsort((-counts, pairs[:, 0]))
    winners = pairs[order][numpy.r_[True, pairs[order][1:, 0] != pairs[order][:-1, 0]]]
    replacement = numpy.full(len(values), numpy.iinfo(numpy.int64).min)
    replacement[winners[:, 0]] = winners[:, 1]
    smallFaces = numpy.flatnonzero(small & (replacement[component] != numpy.iinfo(numpy.int64).min))
    values[smallFaces] = replacement[component[smallFaces]]
  return values


def borderChains(segments):
  """Border edges [vertex, vertex] joined end to end into chains, each a list of vertex indices."""
  touching = {}
  for index, (start, end) in enumerate(segments):
    touching.setdefault(int(start), []).append(index)
    touching.setdefault(int(end), []).append(index)
  used = numpy.zeros(len(segments), dtype=bool)
  chains = []
  for seed in range(len(segments)):
    if used[seed]:
      continue
    used[seed] = True
    chain = [int(segments[seed][0]), int(segments[seed][1])]
    for forward in (True, False):
      while True:
        tip = chain[-1] if forward else chain[0]
        onward = [index for index in touching[tip] if not used[index]] if len(touching[tip]) == 2 else []
        if not onward:
          break
        used[onward[0]] = True
        start, end = (int(vertex) for vertex in segments[onward[0]])
        following = end if start == tip else start
        if forward:
          chain.append(following)
        else:
          chain.insert(0, following)
    chains.append(chain)
  return chains


def footBorders(sceneObject, side, other):
  """The mesh edges where a face of `side` meets a face of `other` lying lower than it (the foot of a wall, where rock rises from the
  ground, and not its lip), with the faces on each side."""
  borderEdges, sideFaces, otherFaces = bridgeMeshAccess.faceBorders(sceneObject, side, other)
  heights = faceCenters(sceneObject)[:, 2]
  rising = heights[sideFaces] > heights[otherFaces]
  return borderEdges[rising], sideFaces[rising], otherFaces[rising]


def paintTransition(objectName, layer, material, selector, toward, width, worldUnitsPerRepeat, onlyAbove):
  """Paint the faces lying wholly within `width` of where the faces picked by `selector` meet those picked by `toward`, on the
  selector's side, and map them so the texture's bottom edge lies on that border and its top `width` away, repeating along the border
  every worldUnitsPerRepeat units: how a transition texture blends one ground into the next. The mapping is the layer's own
  (compose), so erasing, muting, or removing the transition shows the mapping beneath again. Faces reaching past `width`, or over which
  the distance does not run evenly, are left and counted; cut a contour at `width` first (cutContours with distanceFrom, and the same
  onlyAbove) so the strip ends on a modeled edge. With onlyAbove, the strip runs only from the border where the selector's faces rise
  above the toward faces (a wall's foot, not the lip where ground ends above rock falling away), and only faces above it are painted."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if width <= 0 or worldUnitsPerRepeat <= 0:
    raise ValueError(f"width and worldUnitsPerRepeat must be positive, got {width} and {worldUnitsPerRepeat}")
  mesh = sceneObject.data
  side = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  other = bridgeMeshAccess.evaluateSelector(toward, sceneObject, "faces") & ~side
  borderEdges, _, _ = (footBorders if onlyAbove else bridgeMeshAccess.faceBorders)(sceneObject, side, other)
  if not len(borderEdges):
    raise ValueError(f"The faces {selector!r} picks never {'rise above' if onlyAbove else 'meet'} the faces {toward!r} picks on '{objectName}'")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  chains = borderChains(edges[borderEdges])
  segments = numpy.array([[start, end] for chain in chains for start, end in zip(chain, chain[1:])])
  lengths = numpy.linalg.norm(positions[segments[:, 1]] - positions[segments[:, 0]], axis=1)
  chainSizes = [len(chain) - 1 for chain in chains]
  chainStarts = numpy.cumsum([0] + chainSizes)[:-1]
  travelled = numpy.cumsum(lengths) - lengths
  segmentArcs = travelled - numpy.repeat(travelled[chainStarts], chainSizes)
  segmentChains = numpy.repeat(numpy.arange(len(chains)), chainSizes)
  chainLengths = numpy.bincount(segmentChains, weights=lengths)
  closedChains = numpy.array([len(chain) > 2 and chain[0] == chain[-1] for chain in chains])
  # A closed border takes a whole number of repeats, so its strip runs on without a seam where the border starts.
  repeats = numpy.where(closedChains, chainLengths / numpy.maximum(numpy.round(chainLengths / worldUnitsPerRepeat), 1), worldUnitsPerRepeat)
  border = bridgeMeshAccess.BorderDistance(positions[segments[:, 0]], positions[segments[:, 1]])
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  distances = numpy.full(len(positions), numpy.inf)
  owner = numpy.zeros(len(positions), dtype=numpy.int64)
  fraction = numpy.zeros(len(positions))
  for vertex in numpy.unique(loopVertices[side[loopFaces]]):
    distances[vertex], owner[vertex], fraction[vertex] = border.nearest(positions[vertex])
  farthest = numpy.zeros(len(loopTotals))
  closest = numpy.full(len(loopTotals), numpy.inf)
  numpy.maximum.at(farthest, loopFaces, distances[loopVertices])
  numpy.minimum.at(closest, loopFaces, distances[loopVertices])
  reach = width * (1 + transitionTolerance)
  near = side & (closest < width * (1 - transitionTolerance))
  even = numpy.zeros(len(loopTotals), dtype=bool)
  centers = faceCenters(sceneObject)
  above = numpy.ones(len(loopTotals), dtype=bool)
  cornerMeans = numpy.bincount(loopFaces, weights=numpy.where(numpy.isfinite(distances[loopVertices]), distances[loopVertices], 0), minlength=len(loopTotals)) / loopTotals
  for face in numpy.flatnonzero(near):
    distance, segment, along = border.nearest(centers[face])
    even[face] = abs(distance - cornerMeans[face]) <= transitionLinearity * width
    above[face] = centers[face][2] > (border.starts[segment] + along * border.spans[segment])[2]
  # A face whose corners measure along different stretches of border (where they meet at a corner) has no one way along to map.
  loopChains = segmentChains[owner[loopVertices]]
  lowest = numpy.full(len(loopTotals), len(chains))
  highest = numpy.full(len(loopTotals), -1)
  numpy.minimum.at(lowest, loopFaces, loopChains)
  numpy.maximum.at(highest, loopFaces, loopChains)
  wanted = near & (above | (not onlyAbove))
  strip = wanted & even & (lowest == highest) & (farthest <= reach)
  alongLoops = segmentArcs[owner[loopVertices]] + fraction[loopVertices] * lengths[owner[loopVertices]]
  firstAlong = alongLoops[numpy.cumsum(loopTotals) - loopTotals][loopFaces]
  loopSpans = chainLengths[loopChains]
  alongLoops = numpy.where(closedChains[loopChains], firstAlong + numpy.mod(alongLoops - firstAlong + loopSpans / 2, loopSpans) - loopSpans / 2, alongLoops)
  stripLoops = numpy.flatnonzero(strip[loopFaces])
  stripVertices = loopVertices[stripLoops]
  along = alongLoops[stripLoops] / repeats[loopChains[stripLoops]]
  if bridgeSurfacing.uvLayerName not in mesh.uv_layers:
    mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName)
  if baseMappingName not in mesh.attributes:
    uvs = numpy.empty(len(mesh.loops) * 2)
    mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_get("uv", uvs)
    mesh.attributes.new(baseMappingName, "FLOAT_VECTOR", "CORNER")
    writeCornerVectors(mesh, baseMappingName, numpy.column_stack([uvs.reshape(-1, 2), numpy.zeros(len(mesh.loops))]))
  mappingName = transitionMappingPrefix + layer
  if mappingName not in mesh.attributes:
    mesh.attributes.new(mappingName, "FLOAT_VECTOR", "CORNER")
    writeCornerVectors(mesh, mappingName, numpy.full((len(mesh.loops), 3), numpy.nan))
  mapping = readCornerVectors(mesh, mappingName)
  mapping[stripLoops] = numpy.column_stack([along, numpy.minimum(distances[stripVertices] / width, 1.0), numpy.zeros(len(stripLoops))])
  writeCornerVectors(mesh, mappingName, mapping)
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  values[strip] = materialSlot(sceneObject, material)
  writeFaceInts(mesh, layerAttributePrefix + layer, values)
  return {"painted": int(strip.sum()), "straddlingFaces": int((wanted & ~strip).sum()), "borderLength": round(float(lengths.sum()), 1)} | compose(sceneObject)


def projectUVs(objectName, method, worldUnitsPerRepeat, selector, direction):
  """Project UVs onto the selector's faces (bridgeSurfacing.projectedUVs); on a mesh whose layers map transitions their own way, into its
  base mapping, which faces show wherever no transition decides them."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  faceMask, selectedLoops, projected = bridgeSurfacing.projectedUVs(sceneObject, method, worldUnitsPerRepeat, selector, direction)
  mesh = sceneObject.data
  if baseMappingName in mesh.attributes:
    mapping = readCornerVectors(mesh, baseMappingName)
    mapping[selectedLoops, :2] = projected[selectedLoops]
    writeCornerVectors(mesh, baseMappingName, mapping)
    compose(sceneObject)
  else:
    if bridgeSurfacing.uvLayerName not in mesh.uv_layers:
      mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName)
    uvLayer = mesh.uv_layers[bridgeSurfacing.uvLayerName]
    mesh.uv_layers.active = uvLayer
    uvs = numpy.empty(len(mesh.loops) * 2)
    uvLayer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)
    uvs[selectedLoops] = projected[selectedLoops]
    uvLayer.data.foreach_set("uv", uvs.ravel())
    mesh.update()
  return {"object": objectName, "method": method, "faces": int(faceMask.sum()), "worldUnitsPerRepeat": worldUnitsPerRepeat}


# Broad strokes: taking an area back

def resetRegion(objectName, selector, passes, fadeDistance):
  """Take shaping back inside the selection: each named pass (every pass when none are named) loses what it moved there, faded out
  over fadeDistance from the selection's edge so the area rejoins its surroundings. Passes hold moves, not shapes: a pass kept that
  also moved the area (stillShapedBy) keeps its moves, so a level it raised the ground to (a fill's plateau) now stands that much off
  where it was, wherever the passes taken back lifted the ground under it."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  available = [entry["name"] for entry in bridgePasses.passList(sceneObject)]
  if not available:
    raise ValueError(f"'{objectName}' has no shaping passes to take back; rebuildRegion reshapes an area without them")
  names = available if passes is None else passes
  unknown = sorted(set(names) - set(available))
  if unknown:
    raise ValueError(f"'{objectName}' has no shaping passes {unknown}; its passes: {available}")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = bridgeShaping.maskWeights(sceneObject, selector, fadeDistance, positions)
  keys = sceneObject.data.shape_keys
  base = bridgePasses.keyCoordinates(keys.reference_key)
  for name in names:
    key = keys.key_blocks[name]
    coordinates = bridgePasses.keyCoordinates(key)
    key.data.foreach_set("co", (base + (coordinates - base) * (1 - weights)[:, None]).ravel())
  sceneObject.data.update()
  inside = weights > 0
  kept = [name for name in available if name not in names and numpy.abs(bridgePasses.keyCoordinates(keys.key_blocks[name])[inside] - base[inside]).max() > 1e-6]
  return {"object": objectName, "passes": names, "affectedVertices": int(inside.sum()), "stillShapedBy": kept}


def planWeights(sceneObject, plan):
  """The mesh's edges, once for each triangle of its faces they bound, with half the cotangent of the triangle's angle facing them in
  plan: weights that span a height rising evenly in plan as an even rise, whichever way the triangles' diagonals run."""
  mesh = sceneObject.data
  mesh.calc_loop_triangles()
  corners = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("vertices", corners)
  triangles = corners.reshape(-1, 3)
  firsts, seconds, weights = [], [], []
  for corner in range(3):
    at, after, before = triangles[:, corner], triangles[:, (corner + 1) % 3], triangles[:, (corner + 2) % 3]
    toAfter, toBefore = plan[after] - plan[at], plan[before] - plan[at]
    cross = numpy.abs(toAfter[:, 0] * toBefore[:, 1] - toAfter[:, 1] * toBefore[:, 0])
    upright = cross <= 1e-6 * numpy.linalg.norm(toAfter, axis=1) * numpy.linalg.norm(toBefore, axis=1)
    # A triangle standing upright spans no ground in plan, and an obtuse angle's negative cotangent could leave the heights without a
    # solution: neither weighs.
    cotangents = numpy.where(upright, 0.0, (toAfter * toBefore).sum(axis=1) / numpy.maximum(cross, 1e-300))
    firsts.append(after)
    seconds.append(before)
    weights.append(numpy.maximum(cotangents, 0.0) / 2)
  return numpy.concatenate(firsts), numpy.concatenate(seconds), numpy.concatenate(weights)


def relaxedHeights(sceneObject, plan, heights, free):
  """Heights for the free vertices that span smoothly between the fixed ones around them: the discrete Laplace equation over the
  mesh's triangles laid out in plan (planWeights), solved by conjugate gradients."""
  first, second, weights = planWeights(sceneObject, plan)
  heights = heights.copy()
  freeIndices = numpy.flatnonzero(free)
  local = numpy.full(len(heights), -1)
  local[freeIndices] = numpy.arange(len(freeIndices))
  degrees = (numpy.bincount(first, weights=weights, minlength=len(heights)) + numpy.bincount(second, weights=weights, minlength=len(heights)))[freeIndices]
  if (degrees <= 0).any():
    raise ValueError("The selection holds vertices with no ground around them in plan to take a height from")
  knowns = numpy.zeros(len(freeIndices))
  for this, other in ((first, second), (second, first)):
    toFixed = free[this] & ~free[other]
    numpy.add.at(knowns, local[this[toFixed]], weights[toFixed] * heights[other[toFixed]])
  inner = free[first] & free[second]
  innerFirst, innerSecond, innerWeights = local[first[inner]], local[second[inner]], weights[inner]

  def laplacian(values):
    result = degrees * values
    numpy.subtract.at(result, innerFirst, innerWeights * values[innerSecond])
    numpy.subtract.at(result, innerSecond, innerWeights * values[innerFirst])
    return result

  solution = heights[freeIndices]
  residual = knowns - laplacian(solution)
  direction = residual.copy()
  squared = float(residual @ residual)
  target = relaxTolerance * max(float(knowns @ knowns), 1.0)
  for _ in range(4 * len(freeIndices) + 100):
    if squared <= target:
      heights[freeIndices] = solution
      return heights
    stepped = laplacian(direction)
    step = squared / float(direction @ stepped)
    solution = solution + step * direction
    residual = residual - step * stepped
    nextSquared = float(residual @ residual)
    direction = residual + (nextSquared / squared) * direction
    squared = nextSquared
  raise ValueError("The rebuilt heights did not settle; every part of the selection must touch ground outside it to take heights from")


def rebuildRegion(objectName, selector, mode, height, fadeDistance):
  """Give the selection a fresh start: heights spanned smoothly from its surroundings (surroundings), or one level (height), faded in
  over fadeDistance from its edge. With shaping passes, the vertices also go back to where the base lays them out in plan, so sideways
  moves (a roughened or faceted wall, contours a fill snapped onto) leave no creases, and the change goes into the active pass; without
  passes nothing records where they lay, and they keep their places in plan. Its triangles' diagonals then turn to follow the new
  ground (triangulateAlongContours), as diagonals turned for the old shape would crease the new one."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if mode not in rebuildModes:
    raise ValueError(f"mode is one of {list(rebuildModes)}, got '{mode}'")
  if (mode == "height") != (height is not None):
    raise ValueError("A height rebuild takes a height, and only it does")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = bridgeShaping.maskWeights(sceneObject, selector, fadeDistance, positions)
  free = weights > 0
  updated = positions.copy()
  if bridgeMeshAccess.hasShapingPasses(sceneObject):
    laidOut = bridgeMeshAccess.worldPositions(sceneObject, bridgePasses.keyCoordinates(sceneObject.data.shape_keys.reference_key))
    updated[:, :2] += weights[:, None] * (laidOut[:, :2] - positions[:, :2])
  if mode == "surroundings":
    if free.all():
      raise ValueError("The selection covers the whole mesh, so there are no surroundings to span from")
    targets = relaxedHeights(sceneObject, updated[:, :2], positions[:, 2], free)
  else:
    targets = numpy.full(len(positions), float(height))
  updated[:, 2] += weights * (targets - positions[:, 2])
  bridgeShaping.writeWorldPositions(sceneObject, updated)
  turned = bridgeShaping.triangulateAlongContours(sceneObject, updated, free)["turnedDiagonals"]
  return {
    "object": objectName, "mode": mode, "affectedVertices": int(free.sum()), "largestMove": round(float(numpy.linalg.norm(updated - positions, axis=1).max()), 3),
    "turnedDiagonals": turned,
  }


def objectsInRegion(regionName, terrainNames):
  """Rendered objects other than the terrain and regions whose origin lies inside the region, lights and emitters included."""
  regionObject = bridgeMeshAccess.requireRegion(regionName)
  candidates = [sceneObject for sceneObject in bpy.context.scene.objects
    if sceneObject.name not in terrainNames and bridgeMeshAccess.regionIntentProperty not in sceneObject and sceneObject.type != "CAMERA"
    and (not sceneObject.hide_render or bridgeEnvironment.isEmitter(sceneObject))]
  if not candidates:
    return []
  origins = numpy.array([list(sceneObject.matrix_world.translation) for sceneObject in candidates])
  inside = bridgeMeshAccess.insideRegion(regionObject, origins)
  return [sceneObject.name for sceneObject, isInside in zip(candidates, inside) if isInside]


def clearRegion(region, terrainObject, shaping, surfacing, objects, fadeDistance, edgeNoise):
  """Take a region back to start it again: its surfacing erased from every layer of the terrain, over the region's faces or, with
  edgeNoise, over them as that edgeNoise moves their edge (roughenedSelection), so the edgeNoise the region was painted with takes back
  what spilled past its edge; its shaping kept, reset (passes taken back), or rebuilt (spanned from its surroundings); and the objects
  placed in it deleted. Surfacing goes first, while the faces lie where they were painted."""
  if shaping not in shapingChoices:
    raise ValueError(f"shaping is one of {list(shapingChoices)}, got '{shaping}'")
  if not isinstance(surfacing, bool) or not isinstance(objects, bool):
    raise ValueError("surfacing and objects are true or false")
  if edgeNoise is not None and not surfacing:
    raise ValueError("edgeNoise moves the edge of the surfacing clearRegion erases; it needs surfacing")
  bridgeMeshAccess.requireRegion(region)
  selector = {"region": region}
  outcome = {"region": region}
  if surfacing:
    sceneObject = bridgeMeshAccess.requireMeshObject(terrainObject)
    layers = bridgeMeshAccess.surfaceLayers(sceneObject)
    if not layers:
      raise ValueError(f"'{terrainObject}' has no surfacing layers to erase from")
    mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
    bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
    mask = roughenedSelection(sceneObject, mask, edgeNoise)
    erased = 0
    for layer in layers:
      values = readFaceInts(sceneObject.data, layerAttributePrefix + layer["name"])
      erased += int((mask & (values != uncovered)).sum())
      values[mask] = uncovered
      writeLayerValues(sceneObject, layer["name"], values, mask)
    compose(sceneObject)
    outcome["surfacing"] = {"erasedFaces": erased}
  if shaping == "reset":
    outcome["shaping"] = resetRegion(terrainObject, selector, None, fadeDistance)
  elif shaping == "rebuild":
    outcome["shaping"] = rebuildRegion(terrainObject, selector, "surroundings", None, fadeDistance)
  if objects:
    terrainCollection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
    terrainNames = {member.name for member in terrainCollection.all_objects} if terrainCollection else set()
    names = objectsInRegion(region, terrainNames | {terrainObject})
    for name in names:
      bpy.data.objects.remove(bpy.data.objects[name])
    outcome["objects"] = {"deleted": names}
  return outcome


commands = {
  "createRegion": (createRegion, True),
  "editRegion": (editRegion, True),
  "getRegions": (getRegions, False),
  "addSurfaceLayer": (addSurfaceLayer, True),
  "setSurfaceLayer": (setSurfaceLayer, True),
  "removeSurfaceLayer": (removeSurfaceLayer, True),
  "paintSurface": (paintSurface, True),
  "eraseSurface": (eraseSurface, True),
  "editSurface": (editSurface, True),
  "conformSurfaceEdges": (conformSurfaceEdges, True),
  "paintTransition": (paintTransition, True),
  "projectUVs": (projectUVs, True),
  "resetRegion": (resetRegion, True),
  "rebuildRegion": (rebuildRegion, True),
  "clearRegion": (clearRegion, True),
}
