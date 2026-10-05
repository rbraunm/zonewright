"""Authoring a zone the way an environment artist does: regions that say what an area is to become, surfacing layers painted by intent
and edited at their boundaries, and broad strokes that take an area back to start it again. Runs under Blender's Python."""
import collections
import json
import math

import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeCaveData
import bridgeEnvironment
import bridgeExport
import bridgeMeshAccess
import bridgeNoise
import bridgeObjects
import bridgePasses
import bridgeShaping
import bridgeSurfacing

regionCollectionName = "regions"
regionAccess = ("play", "view", "none")
layerOrderProperty = bridgeMeshAccess.surfaceLayersProperty
baseAttributeName = "zonewrightSurfaceBase"
layerAttributePrefix = "zonewrightSurface:"
# Each border edge a conform run put on its evened line keeps the smoothing it was evened at, so a later run at that smoothing or
# less leaves it as it is: evening a line that is already even still moves it (a narrow stroke's ends round off further each time).
# Paint changed beside an edge clears its record.
conformedPrefix = "zonewrightConformed:"
uncovered = -1
surfaceOperations = ("grow", "shrink", "smooth", "clean")
# Snapped vertices whose faces would turn over or shrink below this share of their area go back, over up to this many rounds.
conformSliverShare = 0.1
conformRepairs = 4
# A vertex where a face around it turns more than this from their mean stays put: it sits on a crease (a cliff's edge), which
# sliding it would move, and a border there already runs on a modeled edge.
conformCreaseDegrees = 20
# A vertex this share of the mesh's edge length from an evened border already lies on it: it stays, and no edge crosses the border
# there.
conformOnLineShare = 0.1
# Where the nearer end of a crossed edge cannot slide onto the line, the farther end may, unless that takes it more than this share
# of the way along the edge, stretching the faces behind it.
conformFarthestSlide = 0.75
# Where two pieces touch at a corner, the chains run on through it the way they bend least (the bend of a pair is one plus the
# cosine between the ways they leave); two ways bending within this much of each other leave it ambiguous, and the painted value
# is taken to run on through it.
conformPinchTie = 0.25
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
  return {"name": regionObject.name, "intent": regionObject[bridgeMeshAccess.regionIntentProperty], "access": regionObject.get(bridgeMeshAccess.regionAccessProperty),
    "outline": [[round(float(a), 2), round(float(b), 2)] for a, b in outline], "bottom": round(bottom, 2), "top": round(top, 2), "area": round(area)}


def requireAccess(access):
  if access not in regionAccess:
    raise ValueError(f"A region's access is one of {list(regionAccess)} (players walk or swim there; seen but never entered; never reached), got {access!r}")


def createRegion(name, outline, bottom, top, intent, access):
  bridgeObjects.requireNewName(name)
  if not intent.strip():
    raise ValueError("A region needs its intent: what the area is to become")
  requireAccess(access)
  regionObject = bpy.data.objects.new(name, regionMesh(name, outline, bottom, top))
  regionObject[bridgeMeshAccess.regionIntentProperty] = intent.strip()
  regionObject[bridgeMeshAccess.regionAccessProperty] = access
  regionObject.display_type = "WIRE"
  regionObject.hide_render = True
  bridgeObjects.targetCollection(regionCollectionName).objects.link(regionObject)
  return describeRegion(regionObject)


def editRegion(name, outline, bottom, top, intent, access):
  regionObject = bridgeMeshAccess.requireRegion(name)
  if outline is None and bottom is None and top is None and intent is None and access is None:
    raise ValueError("editRegion needs an outline, bottom, top, intent, or access")
  if access is not None:
    requireAccess(access)
    regionObject[bridgeMeshAccess.regionAccessProperty] = access
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
  regions = [describeRegion(sceneObject) for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" and bridgeMeshAccess.regionIntentProperty in sceneObject]
  return {"regions": regions, "undecided": [region["name"] for region in regions if region["access"] is None]}


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
  """Each face's material from the topmost unmuted layer covering it (below belowPosition), else its base, and that layer's position (-1)."""
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
  # Read and written in the attribute's own single precision, which Blender copies without converting each value.
  vectors = numpy.empty(len(mesh.loops) * 3, dtype=numpy.float32)
  mesh.attributes[attributeName].data.foreach_get("vector", vectors)
  return vectors.reshape(-1, 3).astype(numpy.float64)


def writeCornerVectors(mesh, attributeName, vectors):
  mesh.attributes[attributeName].data.foreach_set("vector", numpy.asarray(vectors, dtype=numpy.float32).ravel())


def mappingAttributes(mesh):
  """The names of the corner mappings a layered mesh keeps besides its UV maps: its base mapping and its layers' transition mappings."""
  return [attribute.name for attribute in mesh.attributes if attribute.name == bridgeSurfacing.baseMappingName or attribute.name.startswith(bridgeSurfacing.transitionMappingPrefix)]


def compose(sceneObject):
  """Show the layers (showLayers) and describe them."""
  showLayers(sceneObject)
  return describeLayers(sceneObject)


def showLayers(sceneObject):
  """Show each face's material, and once a layer maps a transition its own way, each face's mapping, as the layers decide them."""
  mesh = sceneObject.data
  shown, deciders = shownSurface(sceneObject)
  mesh.polygons.foreach_set("material_index", shown)
  if bridgeSurfacing.baseMappingName in mesh.attributes:
    loopTotals, _ = bridgeMeshAccess.faceLoops(sceneObject)
    loopDeciders = numpy.repeat(deciders, loopTotals)
    mapping = readCornerVectors(mesh, bridgeSurfacing.baseMappingName)
    for position, layer in enumerate(bridgeMeshAccess.surfaceLayers(sceneObject)):
      name = bridgeSurfacing.transitionMappingPrefix + layer["name"]
      if name in mesh.attributes:
        own = readCornerVectors(mesh, name)
        showing = (loopDeciders == position) & ~numpy.isnan(own[:, 0])
        mapping[showing] = own[showing]
    mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_set("uv", mapping[:, :2].astype(numpy.float32).ravel())
  mesh.update()


def readEdgeFloats(mesh, attributeName):
  values = numpy.empty(len(mesh.edges))
  mesh.attributes[attributeName].data.foreach_get("value", values)
  return values


def writeLayerValues(sceneObject, layer, values, replaced):
  """Write a layer's paint; replaced faces lose the layer's transition mapping, and their edges the record of being evened."""
  mesh = sceneObject.data
  writeFaceInts(mesh, layerAttributePrefix + layer, values)
  if not replaced.any():
    return
  loopTotals, _ = bridgeMeshAccess.faceLoops(sceneObject)
  replacedLoops = numpy.repeat(replaced, loopTotals)
  name = bridgeSurfacing.transitionMappingPrefix + layer
  if name in mesh.attributes:
    mapping = readCornerVectors(mesh, name)
    mapping[replacedLoops] = numpy.nan
    writeCornerVectors(mesh, name, mapping)
  markName = conformedPrefix + layer
  if markName in mesh.attributes:
    loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
    mesh.loops.foreach_get("edge_index", loopEdges)
    marks = readEdgeFloats(mesh, markName)
    marks[loopEdges[replacedLoops]] = 0
    mesh.attributes[markName].data.foreach_set("value", marks)


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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "addSurfaceLayer")
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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "setSurfaceLayer")
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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "removeSurfaceLayer")
  layers = [layer for layer in requireLayer(sceneObject, name) if layer["name"] != name]
  mesh = sceneObject.data
  for attributeName in (layerAttributePrefix + name, bridgeSurfacing.transitionMappingPrefix + name, conformedPrefix + name):
    if attributeName in mesh.attributes:
      mesh.attributes.remove(mesh.attributes[attributeName])
  sceneObject[layerOrderProperty] = json.dumps(layers)
  described = compose(sceneObject)
  if not layers:
    for attributeName in (baseAttributeName, bridgeSurfacing.baseMappingName):
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
  """The selection with its border moved along the surface by smooth noise up to the amplitude, its pieces kept whole (wholePieces)."""
  if edgeNoise is None:
    return mask
  unknown = sorted(set(edgeNoise) - {"featureSize", "amplitude", "seed"})
  if unknown or "featureSize" not in edgeNoise or edgeNoise.get("amplitude", 0) <= 0:
    raise ValueError(f"edgeNoise is {{featureSize, amplitude (positive), seed}}, got {edgeNoise!r}")
  if mask.all() or not mask.any():
    return mask
  centers, normals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  amplitude = edgeNoise["amplitude"]
  borderEdges, _, _ = bridgeMeshAccess.faceBorders(sceneObject, mask, ~mask)
  ends = bridgeMeshAccess.meshEdges(sceneObject.data)[borderEdges]
  border = bridgeMeshAccess.BorderDistance(positions[ends[:, 0]], positions[ends[:, 1]])
  near = numpy.flatnonzero(noiseReach(sceneObject, mask, amplitude))
  # The noise is sampled in plan, so the same edgeNoise on the same faces picks the same faces again; the move runs along each face.
  plan = centers[near] * numpy.array([1.0, 1.0, 0.0])
  vectors = bridgeNoise.noiseVectors(bridgeNoise.noiseSamplePoints(plan, edgeNoise["featureSize"], edgeNoise.get("seed", 0))) / bridgeNoise.noiseSpread
  vectors -= (vectors * normals[near]).sum(axis=1, keepdims=True) * normals[near]
  lengths = numpy.linalg.norm(vectors, axis=1)
  moves = (amplitude * numpy.tanh(lengths) / numpy.maximum(lengths, 1e-12))[:, None] * vectors
  result = mask.copy()
  # Each face near the border takes the side of the moved border its middle lies on: its signed distance from the border, less the
  # move along that distance's slope.
  for face, move in zip(near, moves):
    distance, segment, along = border.nearest(centers[face])
    side = 1.0 if mask[face] else -1.0
    slope = side * (centers[face] - (border.starts[segment] + along * border.spans[segment])) / distance
    result[face] = side * distance - slope @ move > 0
  return wholePieces(sceneObject, mask, result)


def noiseReach(sceneObject, mask, amplitude):
  """The faces edge noise of this amplitude can move across: those whose middles lie within it and a border edge of the mask's border."""
  centers, _, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  borderEdges, _, _ = bridgeMeshAccess.faceBorders(sceneObject, mask, ~mask)
  if not len(borderEdges):
    return numpy.zeros(len(mask), dtype=bool)
  ends = bridgeMeshAccess.meshEdges(sceneObject.data)[borderEdges]
  reach = amplitude + float(numpy.linalg.norm(positions[ends[:, 1]] - positions[ends[:, 0]], axis=1).max())
  return nearestDistances(centers, positions[numpy.unique(ends)]) <= reach


def wholePieces(sceneObject, mask, roughened):
  """The roughened selection with each piece of the selection rejoined where the noise pinched it, and faces it stranded off them dropped."""
  first, second = faceNeighbourPairs(sceneObject.data)
  count = len(mask)
  inside = mask[first] & mask[second]
  pieces = faceComponents(first[inside], second[inside], count)
  result = roughened.copy()

  def parts():
    joined = result[first] & result[second]
    return faceComponents(first[joined], second[joined], count)

  current = parts()
  # Faces the noise added past a neck that touch the rest only at a corner are specks of their own.
  anchored = numpy.zeros(count, dtype=bool)
  anchored[current[result & mask]] = True
  result &= anchored[current]
  current = parts()
  broken = [piece for piece in numpy.unique(pieces[mask]).tolist() if len(numpy.unique(current[(pieces == piece) & result])) > 1]
  if not broken:
    return result
  neighbours = [[] for _ in range(count)]
  for a, b in zip(first.tolist(), second.tolist()):
    neighbours[a].append(b)
    neighbours[b].append(a)
  for piece in broken:
    members = (pieces == piece) & mask
    while True:
      touching = numpy.unique(current[members & result])
      if len(touching) < 2:
        break
      # The cheapest way through the piece's own faces from one part to another: the fewest faces the noise took to put back.
      sources = numpy.flatnonzero(members & result & (current == touching[0])).tolist()
      costs = {face: 0 for face in sources}
      previous = {}
      queue = collections.deque(sources)
      reached = None
      while queue:
        face = queue.popleft()
        if result[face] and current[face] != touching[0]:
          reached = face
          break
        for neighbour in neighbours[face]:
          if not members[neighbour]:
            continue
          cost = costs[face] + (0 if result[neighbour] else 1)
          if cost < costs.get(neighbour, math.inf):
            costs[neighbour] = cost
            previous[neighbour] = face
            if result[neighbour]:
              queue.appendleft(neighbour)
            else:
              queue.append(neighbour)
      while reached in previous:
        reached = previous[reached]
        result[reached] = True
      current = parts()
  return result


def strokeFaces(sceneObject, selector, edgeNoise):
  """The faces a surface stroke takes: its selector's (a cave's lining only where the selector names the cave), its edge moved by
  edgeNoise (roughenedSelection)."""
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, mask)
  return bridgeCaveData.surfaceMask(sceneObject, selector, roughenedSelection(sceneObject, mask, edgeNoise))


def paintSurface(objectName, layer, material, selector, edgeNoise, keepCaveStroke=True):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "paintSurface")
  requireLayer(sceneObject, layer)
  mask = strokeFaces(sceneObject, selector, edgeNoise)
  slot = materialSlot(sceneObject, material)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  values[mask] = slot
  writeLayerValues(sceneObject, layer, values, mask)
  if keepCaveStroke:
    bridgeCaveData.keepStroke(sceneObject, selector, mask, "paintSurface", {"layer": layer, "material": material, "selector": selector, "edgeNoise": edgeNoise})
  return {"painted": int(mask.sum())} | compose(sceneObject)


def eraseSurface(objectName, layer, selector, edgeNoise, keepCaveStroke=True):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "eraseSurface")
  requireLayer(sceneObject, layer)
  mask = strokeFaces(sceneObject, selector, edgeNoise)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  erased = int((mask & (values != uncovered)).sum())
  values[mask] = uncovered
  writeLayerValues(sceneObject, layer, values, mask)
  if keepCaveStroke:
    bridgeCaveData.keepStroke(sceneObject, selector, mask, "eraseSurface", {"layer": layer, "selector": selector, "edgeNoise": edgeNoise})
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
  """A layer's values with the specks and holes smaller than minimumArea that it or a layer beneath shows cleaned, from its own paint only."""
  mesh = sceneObject.data
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  layers = [entry["name"] for entry in bridgeMeshAccess.surfaceLayers(sceneObject)]
  position = layers.index(layer)
  shown, deciders = shownSurface(sceneObject)
  beneath, _ = shownSurface(sceneObject, position)
  # Where what lies beneath already shows the material taking over, this layer is lifted off; where its own paint takes over, reaching
  # the face through faces it shows, its paint fills in; any other speck of its own is lifted off. It never takes copies of other
  # layers' materials, so a speck one layer beneath paints amid another's is left for cleaning that layer.
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


def editSurface(objectName, layer, operation, steps, selector, minimumArea, keepCaveStroke=True):
  """Grow, shrink, or smooth a layer's painted area within the selector, or clean it (cleanedLayer)."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "editSurface")
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
  within = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, within)
  if keepCaveStroke:
    bridgeCaveData.keepStroke(sceneObject, selector, within, "editSurface", {"layer": layer, "operation": operation, "steps": steps, "selector": selector, "minimumArea": minimumArea})
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
      # Each face takes the value holding most of the surface around its corners, itself included: islands are absorbed and notches
      # and teeth a face or two across rounded off.
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
  """An evened loop pushed out or in evenly until it encloses the original's area (Steiner: out by d gains perimeter d + pi d^2)."""
  held = vectorArea(original)
  perimeter = float(numpy.linalg.norm(numpy.roll(evened, -1, axis=0) - evened, axis=1).sum())
  # A loop that winds around nothing seen from any side (a band around a pillar) has no area to keep.
  if numpy.linalg.norm(held) <= 1e-9 * perimeter * perimeter:
    return evened
  axis = held / numpy.linalg.norm(held)
  outward = numpy.cross(numpy.roll(evened, -1, axis=0) - numpy.roll(evened, 1, axis=0), axis)
  outward /= numpy.maximum(numpy.linalg.norm(outward, axis=1, keepdims=True), 1e-12)
  missing = float(numpy.linalg.norm(held)) - float(vectorArea(evened) @ axis)
  # A loop encloses at most a circle's area for its length and the original encloses some, so the root's argument is positive.
  return evened + (2 * missing / (perimeter + math.sqrt(perimeter * perimeter + 4 * math.pi * missing))) * outward


def resampledChain(points, closed, step):
  """A chain's points (a closed one ending on its first) placed evenly along it, about `step` apart, keeping its ends."""
  arcs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1))])
  count = max(math.ceil(arcs[-1] / step), 3 if closed else 1)
  samples = numpy.linspace(0.0, arcs[-1], count + 1)
  return numpy.column_stack([numpy.interp(samples, arcs, points[:, axis]) for axis in range(3)])


def evenedChain(points, closed, spread):
  """A border chain resampled evenly and Gaussian-evened over `spread` of arc, twice the mean less the mean of the mean (keepingArea)."""
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

  # Adding the mean's own shortfall back once takes saw teeth out while bends much wider than the spread stay where they are; an
  # open chain runs on past each end as its own reflection through it, so it keeps its ends and runs straight into them.
  once = blurred(ring)
  evened = 2 * once - blurred(once)
  if closed:
    evened = keepingArea(original, evened)
    return numpy.vstack([evened, evened[:1]])
  return evened


def chainPieces(chain, positions, movable, settled, closed, spread, shortestLoop):
  """A chain as (points, evened) stretches: evened where its edges join movable vertices and are not settled, else kept as they are."""
  vertices = numpy.array(chain)
  points = positions[vertices]
  conformable = movable[vertices[:-1]] & movable[vertices[1:]] & ~settled
  # A closed piece whose outline is shorter than shortestLoop is a speck too small for the smoothing to even.
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


def pinchJoins(segments, values, leftFaces, positions, normals, reach):
  """Where four border edges meet at a pinch, the pairs a chain runs on through: those running straightest over `reach`, values allowing."""
  touching = {}
  for index, (start, end) in enumerate(segments.tolist()):
    touching.setdefault(start, []).append(index)
    touching.setdefault(end, []).append(index)

  def across(vertex, index):
    start, end = segments[index]
    return int(end if start == vertex else start)

  def farPoint(vertex, index):
    previous, current = vertex, across(vertex, index)
    travelled = float(numpy.linalg.norm(positions[current] - positions[previous]))
    while travelled < reach and current != vertex and len(touching[current]) == 2:
      index = next(other for other in touching[current] if other != index)
      previous, current = current, across(current, index)
      travelled += float(numpy.linalg.norm(positions[current] - positions[previous]))
    return positions[current]

  joins = {}
  for vertex, indices in touching.items():
    if len(indices) != 4:
      continue
    normal = normals[vertex]
    directions = numpy.array([positions[across(vertex, index)] - positions[vertex] for index in indices])
    directions -= (directions @ normal)[:, None] * normal
    first = directions[0] / numpy.linalg.norm(directions[0])
    order = numpy.argsort(numpy.arctan2(directions @ numpy.cross(normal, first), directions @ first))
    ordered = [indices[position] for position in order]
    # Around the vertex counterclockwise, sector i lies between edge i and edge i + 1: the face left of the edge leaving along edge i.
    sectors = [values[leftFaces[(vertex, across(vertex, index))]] for index in ordered]
    far = [farPoint(vertex, index) - positions[vertex] for index in ordered]
    far = [direction / max(float(numpy.linalg.norm(direction)), 1e-12) for direction in far]
    options = []
    for shift in (0, 1):
      joined = sectors[1 - shift]
      if joined == sectors[3 - shift]:
        pairs = [(ordered[shift], ordered[shift + 1]), (ordered[shift + 2], ordered[(shift + 3) % 4])]
        bend = sum(1 + float(far[ordered.index(a)] @ far[ordered.index(b)]) for a, b in pairs)
        options.append((bend, joined == uncovered, pairs))
    if not options:
      continue
    options.sort(key=lambda option: option[0])
    if len(options) == 2 and options[1][0] - options[0][0] < conformPinchTie:
      options.sort(key=lambda option: option[1])
    joins[vertex] = {a: b for pair in options[0][2] for a, b in (pair, pair[::-1])}
  return joins


class BorderCurves:
  """A layer's borders as polylines (chains of edges between faces of different value, evened by chainPieces) with each segment's sides."""

  def __init__(self, sceneObject, values, positions, normals, movable, settled, spread, shortestLoop):
    mesh = sceneObject.data
    edgeIndices, firstFaces, secondFaces = bridgeMeshAccess.sharedEdges(mesh)
    borderEdges = edgeIndices[values[firstFaces] != values[secondFaces]]
    segments = bridgeMeshAccess.meshEdges(mesh)[borderEdges]
    loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
    nextLoops, _ = loopNeighbours(sceneObject)
    leftFaces = dict(zip(zip(loopVertices.tolist(), loopVertices[nextLoops].tolist()), numpy.repeat(numpy.arange(len(loopTotals)), loopTotals).tolist()))
    for start, end in segments.tolist():
      if (start, end) not in leftFaces or (end, start) not in leftFaces:
        raise ValueError(f"The faces of '{sceneObject.name}' on either side of the edge between vertices {start} and {end} wind the same way; recalculate its normals (cleanupMesh)")
    joins = pinchJoins(segments, values, leftFaces, positions, normals, spread)
    starts, ends, startTangents, endTangents, sides, evened, curves = [], [], [], [], [], [], []
    for chain, indices, closed in bridgeMeshAccess.tracedChains(segments, joins):
      faces = leftFaces[(chain[0], chain[1])], leftFaces[(chain[1], chain[0])]
      for points, isEvened in chainPieces(chain, positions, movable, settled[borderEdges[indices]], closed, spread, shortestLoop):
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

  def signedTo(self, points, normal, curve):
    """The distances from points to one curve, positive on its left."""
    chosen = numpy.flatnonzero(self.curves == curve)
    starts, spans = self.starts[chosen], self.ends[chosen] - self.starts[chosen]
    along = numpy.clip(((points[:, None] - starts[None]) * spans[None]).sum(axis=2) / numpy.maximum((spans * spans).sum(axis=1), 1e-12)[None], 0, 1)
    nearest = starts[None] + along[..., None] * spans[None]
    distances = numpy.linalg.norm(nearest - points[:, None], axis=2)
    best = distances.argmin(axis=1)
    rows = numpy.arange(len(points))
    fractions, segments = along[rows, best], chosen[best]
    tangents = (1 - fractions)[:, None] * self.startTangents[segments] + fractions[:, None] * self.endTangents[segments]
    left = ((points - nearest[rows, best]) * numpy.cross(normal, tangents)).sum(axis=1) > 0
    return numpy.where(left, distances[rows, best], -distances[rows, best])


def crossings(curves, positions, normals, candidates, nearestCurves, edges, onLine):
  """The mesh edges an evened curve crosses between two vertices off it, one of them nearest that curve, and how far along each."""
  first, second = edges[:, 0], edges[:, 1]
  both = candidates[first] & candidates[second]
  firsts, seconds, balances = [], [], []
  # An edge across a stroke one face wide runs from a vertex nearest one side's curve to a vertex nearest the other's: each end is
  # measured from both curves, so the edge is found crossing both.
  for curve in numpy.unique(nearestCurves[numpy.r_[first[both], second[both]]]).tolist():
    if not curves.evened[curves.curves == curve][0]:
      continue
    touching = both & ((nearestCurves[first] == curve) | (nearestCurves[second] == curve))
    ends = numpy.unique(numpy.r_[first[touching], second[touching]])
    signed = numpy.zeros(len(positions))
    signed[ends] = curves.signedTo(positions[ends], normals[ends], curve)
    crossing = touching & (numpy.abs(signed[first]) > onLine) & (numpy.abs(signed[second]) > onLine) & (numpy.sign(signed[first]) != numpy.sign(signed[second]))
    firsts.append(first[crossing])
    seconds.append(second[crossing])
    balances.append(signed[first[crossing]] / (signed[first[crossing]] - signed[second[crossing]]))
  if not firsts:
    return numpy.zeros(0, dtype=numpy.int64), numpy.zeros(0, dtype=numpy.int64), numpy.zeros(0)
  return numpy.concatenate(firsts), numpy.concatenate(seconds), numpy.concatenate(balances)


def assignedSlides(first, second, balance, lengths):
  """Shortest slides first, a vertex for each crossing and a crossing for each vertex: its nearer end, else its farther end."""
  options = sorted(
    (fraction * length, crossing, mover, partner, fraction)
    for crossing, (start, end, along, length) in enumerate(zip(first.tolist(), second.tolist(), balance.tolist(), lengths.tolist()))
    for mover, partner, fraction in ((start, end, along), (end, start, 1 - along)) if fraction <= conformFarthestSlide
  )
  served, taken, movers, partners, fractions = set(), set(), [], [], []
  for _, crossing, mover, partner, fraction in options:
    if crossing not in served and mover not in taken:
      served.add(crossing)
      taken.add(mover)
      movers.append(mover)
      partners.append(partner)
      fractions.append(fraction)
  return numpy.array(movers, dtype=numpy.int64), numpy.array(partners, dtype=numpy.int64), numpy.array(fractions, dtype=numpy.float64)


def unspoiledSlides(sceneObject, positions, movers, partners, fractions):
  """The slides less those folding a face or making a sliver (the longest on each handed once to its edge's other end), and how many failed."""
  areasBefore = faceAreas(sceneObject, positions)
  normalsBefore = bridgeMeshAccess.faceNormals(sceneObject, positions)
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  retried = numpy.zeros(len(movers), dtype=bool)
  # A farther end only takes a crossing whose nearer end serves another; dropping it leaves that crossing no worse off.
  nearer = fractions <= 0.5
  dropped = 0
  for repair in range(conformRepairs + 1):
    updated = positions.copy()
    updated[movers] += fractions[:, None] * (positions[partners] - positions[movers])
    normalsAfter = bridgeMeshAccess.faceNormals(sceneObject, updated)
    spoiled = ((normalsBefore * normalsAfter).sum(axis=1) <= 0) | (numpy.linalg.norm(normalsAfter, axis=1) < conformSliverShare * areasBefore)
    if not spoiled.any():
      break
    slideOf = numpy.full(len(positions), -1)
    slideOf[movers] = numpy.arange(len(movers))
    slideLengths = numpy.linalg.norm(updated - positions, axis=1)
    spoiledLoops = numpy.flatnonzero(spoiled[loopFaces] & (slideOf[loopVertices] >= 0))
    # Of the slides spoiling a face, the longest is taken back; the others may be fine once it is.
    order = numpy.lexsort((-slideLengths[loopVertices[spoiledLoops]], loopFaces[spoiledLoops]))
    firstOfFace = order[numpy.r_[True, loopFaces[spoiledLoops][order][1:] != loopFaces[spoiledLoops][order][:-1]]] if len(order) else order
    blamed = numpy.unique(slideOf[loopVertices[spoiledLoops[firstOfFace]]])
    keep = numpy.ones(len(movers), dtype=bool)
    moving = set(movers.tolist())
    for slide in blamed.tolist():
      partner = int(partners[slide])
      if repair < conformRepairs and not retried[slide] and partner not in moving and 1 - fractions[slide] <= conformFarthestSlide:
        moving.discard(int(movers[slide]))
        moving.add(partner)
        movers[slide], partners[slide], fractions[slide] = partner, movers[slide], 1 - fractions[slide]
        retried[slide] = True
      else:
        keep[slide] = False
    dropped += int((~keep & nearer).sum())
    movers, partners, fractions, retried, nearer = movers[keep], partners[keep], fractions[keep], retried[keep], nearer[keep]
  return movers, partners, fractions, dropped


def slideAlongEdges(sceneObject, movers, partners, fractions):
  """Move each vertex the fraction toward its partner alike in the mesh and every shaping pass, carrying each face's mappings along."""
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
  """Bring a layer's borders onto the mesh's own edges along evened lines; borders evened at this smoothing or more stay as they are."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "conformSurfaceEdges")
  requireLayer(sceneObject, layer)
  if smoothing <= 0:
    raise ValueError(f"smoothing must be positive, got {smoothing}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  within = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, within)
  mesh = sceneObject.data
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  if len(numpy.unique(values[within])) < 2:
    raise ValueError(f"Layer '{layer}' of '{objectName}' holds one value everywhere {selector!r} picks, so it has no edge to conform there")
  markName = conformedPrefix + layer
  settled = (readEdgeFloats(mesh, markName) if markName in mesh.attributes else numpy.zeros(len(mesh.edges))) >= smoothing
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  edgeLength = float(numpy.median(numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)))
  onLine = conformOnLineShare * edgeLength
  movable = movableVertices(sceneObject, within, positions)
  # A cave's plug, ring, and lining stay where its cut put them, in every pass.
  caveVertices = bridgeCaveData.fixedInPlan(sceneObject)
  caveVerticesLeft = int((movable & caveVertices).sum())
  movable &= ~caveVertices
  edgeIndices, firstFaces, secondFaces = bridgeMeshAccess.sharedEdges(mesh)
  inside = within[firstFaces] & within[secondFaces]
  alreadyEvened = int(settled[edgeIndices[(values[firstFaces] != values[secondFaces]) & inside]].sum())
  curves = BorderCurves(sceneObject, values, positions, normals, movable, settled, smoothing, math.pi * smoothing)
  reach = 2 * (smoothing + edgeLength)
  candidates = movable & curves.nearEvened(positions, reach)
  nearestCurves = numpy.full(len(positions), -1)
  for vertex in numpy.flatnonzero(candidates).tolist():
    nearestCurves[vertex] = curves.curves[curves.signedNearest(positions[vertex], normals[vertex])[1]]
  first, second, balance = crossings(curves, positions, normals, candidates, nearestCurves, edges, onLine)
  movers, partners, fractions = assignedSlides(first, second, balance, numpy.linalg.norm(positions[first] - positions[second], axis=1))
  movers, partners, fractions, dropped = unspoiledSlides(sceneObject, positions, movers, partners, fractions)
  moved = positions.copy()
  moved[movers] += fractions[:, None] * (positions[partners] - positions[movers])
  if len(movers):
    slideAlongEdges(sceneObject, movers, partners, fractions)
  updatedValues = decidedFaces(sceneObject, curves, values, within, movable, moved, movers, reach)
  writeLayerValues(sceneObject, layer, updatedValues, updatedValues != values)
  marks = readEdgeFloats(mesh, markName) if markName in mesh.attributes else numpy.zeros(len(mesh.edges))
  bordersAfter = edgeIndices[(updatedValues[firstFaces] != updatedValues[secondFaces]) & inside]
  onEvened = movable & curves.nearEvened(moved, edgeLength)
  marks[bordersAfter[onEvened[edges[bordersAfter, 0]] & onEvened[edges[bordersAfter, 1]]]] = smoothing
  if markName not in mesh.attributes:
    mesh.attributes.new(markName, "FLOAT", "EDGE")
  mesh.attributes[markName].data.foreach_set("value", marks)
  return {
    "movedVertices": int(len(movers)), "keptInPlace": dropped, "changedFaces": int((updatedValues != values).sum()), "alreadyEvened": alreadyEvened,
  } | ({"caveVerticesLeft": caveVerticesLeft} if bridgeCaveData.holdsCaves(sceneObject) else {}) | compose(sceneObject)


def decidedFaces(sceneObject, curves, values, within, movable, moved, movers, reach):
  """Each face near an evened curve takes the side of the curve nearest its middle that holds most of it."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  slid = numpy.zeros(len(moved), dtype=bool)
  slid[movers] = True
  # A face on a crease keeps its value unless a corner of it moved: an evened line passing near the crease should not carry the
  # material across it.
  touchesMove = numpy.bincount(loopFaces, weights=slid[loopVertices], minlength=len(loopTotals)) > 0
  allMovable = numpy.bincount(loopFaces, weights=~movable[loopVertices], minlength=len(loopTotals)) == 0
  centers = numpy.zeros((len(loopTotals), 3))
  numpy.add.at(centers, loopFaces, moved[loopVertices])
  centers /= loopTotals[:, None]
  faceDirections = bridgeMeshAccess.faceNormals(sceneObject, moved)
  faceDirections /= numpy.maximum(numpy.linalg.norm(faceDirections, axis=1, keepdims=True), 1e-12)
  deciding = within & (touchesMove | allMovable)
  deciding[deciding] = curves.nearEvened(centers[deciding], reach)
  loopStarts = numpy.cumsum(loopTotals) - loopTotals
  updatedValues = values.copy()
  for face in numpy.flatnonzero(deciding).tolist():
    _, segment = curves.signedNearest(centers[face], faceDirections[face])
    left, right = curves.sides[segment]
    if not curves.evened[segment] or values[face] not in (left, right):
      continue
    # Its middle alone can lie across a curving line from most of the face (two corners on the line, the third off it beyond a
    # bulge), and its corners can all lie on the line: points between its middle and each corner say which side holds most of it.
    corners = moved[loopVertices[loopStarts[face]:loopStarts[face] + loopTotals[face]]]
    samples = numpy.vstack([centers[face], (corners + centers[face]) / 2])
    side = float(curves.signedTo(samples, faceDirections[face], curves.curves[segment]).sum())
    if abs(side) > 1e-9:
      updatedValues[face] = left if side > 0 else right
  return updatedValues


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


def paintTransition(objectName, layer, material, selector, toward, width, worldUnitsPerRepeat, onlyAbove, keepCaveStroke=True):
  """Paint the faces wholly within `width` of where selector's faces meet toward's, mapped up from that border and along it (stripAlong);
  the material is marked as a transition, so export checks count the borders it lies along as bridged."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "paintTransition")
  requireLayer(sceneObject, layer)
  if width <= 0 or worldUnitsPerRepeat <= 0:
    raise ValueError(f"width and worldUnitsPerRepeat must be positive, got {width} and {worldUnitsPerRepeat}")
  mesh = sceneObject.data
  side = bridgeCaveData.surfaceMask(sceneObject, selector, bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces"))
  other = bridgeCaveData.surfaceMask(sceneObject, toward, bridgeMeshAccess.evaluateSelector(toward, sceneObject, "faces")) & ~side
  borderEdges, _, _ = (bridgeMeshAccess.footBorders if onlyAbove else bridgeMeshAccess.faceBorders)(sceneObject, side, other)
  if not len(borderEdges):
    raise ValueError(f"The faces {selector!r} picks never {'rise above' if onlyAbove else 'meet'} the faces {toward!r} picks on '{objectName}'")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  chains = bridgeMeshAccess.tracedChains(edges[borderEdges], {})
  segments = numpy.array([[start, end] for chain, _, _ in chains for start, end in zip(chain, chain[1:])])
  lengths = numpy.linalg.norm(positions[segments[:, 1]] - positions[segments[:, 0]], axis=1)
  chainSizes = [len(chain) - 1 for chain, _, _ in chains]
  chainStarts = numpy.cumsum([0] + chainSizes)[:-1]
  travelled = numpy.cumsum(lengths) - lengths
  segmentArcs = travelled - numpy.repeat(travelled[chainStarts], chainSizes)
  segmentChains = numpy.repeat(numpy.arange(len(chains)), chainSizes)
  chainLengths = numpy.bincount(segmentChains, weights=lengths)
  closedChains = numpy.array([closed for _, _, closed in chains])
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
  nearestAlong = segmentArcs[owner] + fraction * lengths[owner]
  alongLoops = numpy.zeros(len(loopVertices))
  for chain, (chainVertices, _, closed) in enumerate(chains):
    faces = strip & (lowest == chain)
    if faces.any():
      chainLoops = faces[loopFaces]
      alongLoops[chainLoops] = stripAlong(sceneObject, positions, faces, chainVertices, nearestAlong, chainLengths[chain] if closed else None)[loopVertices[chainLoops]]
  firstAlong = alongLoops[numpy.cumsum(loopTotals) - loopTotals][loopFaces]
  loopSpans = chainLengths[loopChains]
  alongLoops = numpy.where(closedChains[loopChains], firstAlong + numpy.mod(alongLoops - firstAlong + loopSpans / 2, loopSpans) - loopSpans / 2, alongLoops)
  stripLoops = numpy.flatnonzero(strip[loopFaces])
  stripVertices = loopVertices[stripLoops]
  along = alongLoops[stripLoops] / repeats[loopChains[stripLoops]]
  if bridgeSurfacing.uvLayerName not in mesh.uv_layers:
    mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName)
  if bridgeSurfacing.baseMappingName not in mesh.attributes:
    uvs = numpy.empty(len(mesh.loops) * 2)
    mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_get("uv", uvs)
    mesh.attributes.new(bridgeSurfacing.baseMappingName, "FLOAT_VECTOR", "CORNER")
    writeCornerVectors(mesh, bridgeSurfacing.baseMappingName, numpy.column_stack([uvs.reshape(-1, 2), numpy.zeros(len(mesh.loops))]))
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  values[strip] = materialSlot(sceneObject, material)
  writeLayerValues(sceneObject, layer, values, strip)
  bpy.data.materials[material][bridgeSurfacing.transitionPropertyName] = True
  mappingName = bridgeSurfacing.transitionMappingPrefix + layer
  if mappingName not in mesh.attributes:
    mesh.attributes.new(mappingName, "FLOAT_VECTOR", "CORNER")
    writeCornerVectors(mesh, mappingName, numpy.full((len(mesh.loops), 3), numpy.nan))
  mapping = readCornerVectors(mesh, mappingName)
  mapping[stripLoops] = numpy.column_stack([along, numpy.minimum(distances[stripVertices] / width, 1.0), numpy.zeros(len(stripLoops))])
  writeCornerVectors(mesh, mappingName, mapping)
  if keepCaveStroke:
    bridgeCaveData.keepStroke(sceneObject, selector, strip, "paintTransition", {
      "layer": layer, "material": material, "selector": selector, "toward": toward, "width": width, "worldUnitsPerRepeat": worldUnitsPerRepeat, "onlyAbove": onlyAbove,
    })
  return {"painted": int(strip.sum()), "straddlingFaces": int((wanted & ~strip).sum()), "borderLength": round(float(lengths.sum()), 1)} | compose(sceneObject)


def stripAlong(sceneObject, positions, faces, chainVertices, nearestAlong, loopLength):
  """How far along its border each vertex of a strip lies: the border's own vertices where they are, the rest spanned smoothly between."""
  # Taken from the nearest point of the border, a fan of faces above a corner the border turns around (a notch of ground poking up a
  # wall) would all measure from that one point and smear the texture's one column across them; spanned harmonically from the
  # border, they share out the turn. Pieces of strip out of touch with the border keep their nearest points.
  first, second, weights = cotangentWeights(sceneObject, positions, faces)
  stripVertices = numpy.unique(numpy.r_[first, second])
  onBorder = numpy.zeros(len(positions), dtype=bool)
  onBorder[chainVertices] = True
  inStrip = numpy.zeros(len(positions), dtype=bool)
  inStrip[stripVertices] = True
  reached = onBorder & inStrip
  while True:
    spreading = numpy.zeros(len(positions), dtype=bool)
    spreading[second[reached[first] & ~reached[second]]] = True
    spreading[first[reached[second] & ~reached[first]]] = True
    if not spreading.any():
      break
    reached |= spreading
  degrees = numpy.bincount(first, weights=weights, minlength=len(positions)) + numpy.bincount(second, weights=weights, minlength=len(positions))
  free = inStrip & reached & ~onBorder & (degrees > 0)
  failure = "The transition's mapping along its border did not settle"
  if loopLength is None:
    # Nothing holds the vertices square off an open border's end along it, so spanned harmonically they drift back toward the border
    # and squeeze the strip's last faces; kept at their nearest points, a straight strip maps evenly to its end.
    return harmonicValues(first, second, weights, nearestAlong, free & ~squareOffEnds(positions, chainVertices, nearestAlong), failure)
  turns = 2 * math.pi * nearestAlong / loopLength
  across = harmonicValues(first, second, weights, numpy.cos(turns), free, failure)
  upward = harmonicValues(first, second, weights, numpy.sin(turns), free, failure)
  return numpy.mod(numpy.arctan2(upward, across), 2 * math.pi) * loopLength / (2 * math.pi)


def squareOffEnds(positions, chainVertices, nearestAlong):
  """The vertices whose nearest point of an open border is one of its ends, lying straight out from it across its end segment."""
  square = numpy.zeros(len(positions), dtype=bool)
  for end, before in ((chainVertices[0], chainVertices[1]), (chainVertices[-1], chainVertices[-2])):
    direction = positions[end] - positions[before]
    offsets = positions - positions[end]
    atEnd = numpy.abs(nearestAlong - nearestAlong[end]) <= transitionTolerance * numpy.linalg.norm(direction)
    square |= atEnd & (numpy.abs(offsets @ direction) <= transitionTolerance * numpy.linalg.norm(offsets, axis=1) * numpy.linalg.norm(direction))
  return square


def projectUVs(objectName, method, worldUnitsPerRepeat, selector, direction):
  """Project UVs onto the selector's faces; on a mesh whose layers map transitions, into its base mapping (compose)."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "projectUVs")
  bridgeCaveData.refuseLiningMapping(sceneObject, selector, "projectUVs")
  faceMask, selectedLoops, projected = bridgeSurfacing.projectedUVs(sceneObject, method, worldUnitsPerRepeat, selector, direction)
  faceMask = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, faceMask)
  selectedLoops = faceMask[numpy.repeat(numpy.arange(len(faceMask)), bridgeMeshAccess.faceLoops(sceneObject)[0])]
  writeProjection(sceneObject, selectedLoops, projected)
  return {"object": objectName, "method": method, "faces": int(faceMask.sum()), "worldUnitsPerRepeat": worldUnitsPerRepeat}


def writeProjection(sceneObject, selectedLoops, projected):
  """Set the selected corners' mapping to a projection: on a mesh whose layers map transitions, into its base mapping (compose); else
  into its UV map."""
  mesh = sceneObject.data
  if bridgeSurfacing.baseMappingName in mesh.attributes:
    mapping = readCornerVectors(mesh, bridgeSurfacing.baseMappingName)
    mapping[selectedLoops, :2] = projected[selectedLoops]
    writeCornerVectors(mesh, bridgeSurfacing.baseMappingName, mapping)
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


# Broad strokes: taking an area back

def resetRegion(objectName, selector, passes, fadeDistance):
  """Take the named shaping passes (or all) back inside the selection, faded over fadeDistance; names the kept passes still shaping it."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "resetRegion")
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
    guarded, _ = bridgeCaveData.guardedKey(sceneObject, coordinates, base + (coordinates - base) * (1 - weights)[:, None])
    key.data.foreach_set("co", guarded.ravel())
  sceneObject.data.update()
  inside = weights > 0
  # Passes hold moves, not shapes: a kept pass that also moved the area keeps its moves, so a level it raised the ground to (a fill's
  # plateau) now stands off where it was by what the passes taken back lifted under it.
  kept = [name for name in available if name not in names and numpy.abs(bridgePasses.keyCoordinates(keys.key_blocks[name])[inside] - base[inside]).max() > 1e-6]
  return {"object": objectName, "passes": names, "affectedVertices": int(inside.sum()), "stillShapedBy": kept}


def cotangentWeights(sceneObject, points, faces):
  """Edges of the given faces' triangles, once per triangle, weighted by half the cotangent of the angle facing them: a discrete Laplacian."""
  mesh = sceneObject.data
  mesh.calc_loop_triangles()
  corners = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("vertices", corners)
  owners = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("polygon_index", owners)
  triangles = corners.reshape(-1, 3)[faces[owners]]
  firsts, seconds, weights = [], [], []
  for corner in range(3):
    at, after, before = triangles[:, corner], triangles[:, (corner + 1) % 3], triangles[:, (corner + 2) % 3]
    toAfter, toBefore = points[after] - points[at], points[before] - points[at]
    cross = numpy.linalg.norm(numpy.cross(toAfter, toBefore), axis=1)
    upright = cross <= 1e-6 * numpy.linalg.norm(toAfter, axis=1) * numpy.linalg.norm(toBefore, axis=1)
    # A triangle with no area (one standing upright, laid out in plan) spans nothing, and an obtuse angle's negative cotangent could
    # leave the values without a solution: neither weighs.
    cotangents = numpy.where(upright, 0.0, (toAfter * toBefore).sum(axis=1) / numpy.maximum(cross, 1e-300))
    firsts.append(after)
    seconds.append(before)
    weights.append(numpy.maximum(cotangents, 0.0) / 2)
  return numpy.concatenate(firsts), numpy.concatenate(seconds), numpy.concatenate(weights)


def harmonicValues(first, second, weights, values, free, failure):
  """Values for the free vertices spanning smoothly between the fixed ones (the discrete Laplace equation), by conjugate gradients."""
  values = values.copy()
  freeIndices = numpy.flatnonzero(free)
  local = numpy.full(len(values), -1)
  local[freeIndices] = numpy.arange(len(freeIndices))
  degrees = (numpy.bincount(first, weights=weights, minlength=len(values)) + numpy.bincount(second, weights=weights, minlength=len(values)))[freeIndices]
  knowns = numpy.zeros(len(freeIndices))
  for this, other in ((first, second), (second, first)):
    toFixed = free[this] & ~free[other]
    numpy.add.at(knowns, local[this[toFixed]], weights[toFixed] * values[other[toFixed]])
  inner = free[first] & free[second]
  innerFirst, innerSecond, innerWeights = local[first[inner]], local[second[inner]], weights[inner]

  def laplacian(unknowns):
    result = degrees * unknowns
    numpy.subtract.at(result, innerFirst, innerWeights * unknowns[innerSecond])
    numpy.subtract.at(result, innerSecond, innerWeights * unknowns[innerFirst])
    return result

  solution = values[freeIndices]
  residual = knowns - laplacian(solution)
  direction = residual.copy()
  squared = float(residual @ residual)
  target = relaxTolerance * max(float(knowns @ knowns), 1.0)
  for _ in range(4 * len(freeIndices) + 100):
    if squared <= target:
      values[freeIndices] = solution
      return values
    stepped = laplacian(direction)
    step = squared / float(direction @ stepped)
    solution = solution + step * direction
    residual = residual - step * stepped
    nextSquared = float(residual @ residual)
    direction = residual + (nextSquared / squared) * direction
    squared = nextSquared
  raise ValueError(failure)


def relaxedHeights(sceneObject, plan, heights, free):
  """Heights for the free vertices spanning smoothly between the fixed ones over the mesh's triangles laid out in plan."""
  first, second, weights = cotangentWeights(sceneObject, numpy.column_stack([plan, numpy.zeros(len(plan))]), numpy.ones(len(sceneObject.data.polygons), dtype=bool))
  degrees = numpy.bincount(first, weights=weights, minlength=len(heights)) + numpy.bincount(second, weights=weights, minlength=len(heights))
  if (degrees[free] <= 0).any():
    raise ValueError("The selection holds vertices with no ground around them in plan to take a height from")
  return harmonicValues(first, second, weights, heights, free, "The rebuilt heights did not settle; every part of the selection must touch ground outside it to take heights from")


def rebuildRegion(objectName, selector, mode, height, fadeDistance):
  """Span the selection's heights from its surroundings or level it, laid out again in plan where passes record it, diagonals turned."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "rebuildRegion")
  if mode not in rebuildModes:
    raise ValueError(f"mode is one of {list(rebuildModes)}, got '{mode}'")
  if (mode == "height") != (height is not None):
    raise ValueError("A height rebuild takes a height, and only it does")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = bridgeShaping.maskWeights(sceneObject, selector, fadeDistance, positions)
  free = weights > 0
  updated = positions.copy()
  # With shaping passes the base records where each vertex lay in plan, so sideways moves (a roughened or faceted wall, contours a fill
  # snapped onto) are taken back and leave no creases; without them nothing records it.
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
  left = bridgeShaping.writeWorldPositions(sceneObject, updated)
  # Diagonals turned for the old shape would crease the new one.
  turned = bridgeShaping.triangulateAlongContours(sceneObject, updated, free)["turnedDiagonals"]
  return {
    "object": objectName, "mode": mode, "affectedVertices": int(free.sum()), "largestMove": round(float(numpy.linalg.norm(updated - positions, axis=1).max()), 3),
    "turnedDiagonals": turned,
  } | left


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
  """Take a region back: its surfacing erased (edgeNoise moving the edge as roughenedSelection does), its shaping kept, reset, or rebuilt."""
  if shaping not in shapingChoices:
    raise ValueError(f"shaping is one of {list(shapingChoices)}, got '{shaping}'")
  if not isinstance(surfacing, bool) or not isinstance(objects, bool):
    raise ValueError("surfacing and objects are true or false")
  if edgeNoise is not None and not surfacing:
    raise ValueError("edgeNoise moves the edge of the surfacing clearRegion erases; it needs surfacing")
  bridgeMeshAccess.requireRegion(region)
  selector = {"region": region}
  outcome = {"region": region}
  # Surfacing goes first, while the faces lie where they were painted.
  if surfacing:
    sceneObject = bridgeMeshAccess.requireEditableMesh(terrainObject, "clearRegion")
    layers = bridgeMeshAccess.surfaceLayers(sceneObject)
    if not layers:
      raise ValueError(f"'{terrainObject}' has no surfacing layers to erase from")
    regionFaces = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
    bridgeMeshAccess.requireSelection(regionFaces, selector, sceneObject, "faces")
    regionFaces = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, regionFaces)
    mask = bridgeCaveData.surfaceMask(sceneObject, selector, roughenedSelection(sceneObject, regionFaces, edgeNoise))
    # The same edgeNoise picks the same faces only on ground as it was painted; reshaped since, a face of the spill can fall outside.
    # Paint lying wholly within the noise's reach of the region, cut off from paint beyond it, is spill however the ground moved.
    within =bridgeCaveData.surfaceMask(sceneObject, selector, (mask | regionFaces | noiseReach(sceneObject, regionFaces, edgeNoise["amplitude"])) if edgeNoise is not None else mask)
    first, second = faceNeighbourPairs(sceneObject.data)
    erased = 0
    for layer in layers:
      values = readFaceInts(sceneObject.data, layerAttributePrefix + layer["name"])
      same = values[first] == values[second]
      pieces = faceComponents(first[same], second[same], len(values))
      beyond = numpy.bincount(pieces, weights=(~within).astype(numpy.float64), minlength=len(values))[pieces] > 0
      erasing = mask | (within & ~beyond & (values != uncovered))
      erased += int((erasing & (values != uncovered)).sum())
      values[erasing] = uncovered
      writeLayerValues(sceneObject, layer["name"], values, erasing)
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
