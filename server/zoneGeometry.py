import collections
import struct

import numpy

import eqArchive
import eqgFiles
import eqgTerrain
import eqWorldFile
import zoneSources

surfaceKinds = ("solid", "invisible", "passable", "water", "lava", "cutout", "translucent")
surfaceCode = {kind: code for code, kind in enumerate(surfaceKinds)}
wldMaskedRenderMethod = 0x13


class GeometryBuilder:
  def __init__(self):
    self.vertexChunks = []
    self.triangleChunks = []
    self.textureChunks = []
    self.surfaceChunks = []
    self.uvAreaChunks = []
    self.objectChunks = []
    self.paintedChunks = []
    self.vertexCount = 0
    self.textureIndex = {}
    self.droppedTriangles = collections.Counter()

  def textureID(self, textureName):
    if textureName is None:
      return -1
    return self.textureIndex.setdefault(textureName, len(self.textureIndex))

  def add(self, vertices, triangles, triangleTextures, triangleSurfaces, uvs, isObject, trianglePainted):
    """A mesh's triangles; uvs (one per vertex, or None where the source has none) give each triangle's area in texture repeats,
    isObject marks a placed object rather than the zone's own terrain or region meshes, and trianglePainted marks triangles whose
    material paints its ground from a palette map or blends textures."""
    self.vertexChunks.append(vertices)
    self.triangleChunks.append(triangles + self.vertexCount)
    self.textureChunks.append(triangleTextures)
    self.surfaceChunks.append(triangleSurfaces)
    if uvs is None:
      self.uvAreaChunks.append(numpy.full(len(triangles), numpy.nan))
    else:
      corners = uvs[triangles]
      edges = corners[:, 1:] - corners[:, :1]
      self.uvAreaChunks.append(numpy.abs(edges[:, 0, 0] * edges[:, 1, 1] - edges[:, 0, 1] * edges[:, 1, 0]) / 2)
    self.objectChunks.append(numpy.full(len(triangles), isObject))
    self.paintedChunks.append(numpy.broadcast_to(numpy.asarray(trianglePainted, dtype=bool), (len(triangles),)).copy())
    self.vertexCount += len(vertices)

  def build(self, **details):
    return {
      "vertices": numpy.concatenate(self.vertexChunks),
      "triangles": numpy.concatenate(self.triangleChunks),
      "triangleTextures": numpy.concatenate(self.textureChunks),
      "triangleSurfaces": numpy.concatenate(self.surfaceChunks),
      "triangleUVAreas": numpy.concatenate(self.uvAreaChunks),
      "triangleIsObject": numpy.concatenate(self.objectChunks),
      "trianglePainted": numpy.concatenate(self.paintedChunks),
      "textureNames": list(self.textureIndex),
      "droppedTriangles": dict(sorted(self.droppedTriangles.items())),
    } | details


def eqgMaterialPainted(material):
  """A material that paints ground from a palette map, choosing among detail textures, or blends textures in its shader."""
  return "e_TexturePalette0" in material["properties"] or "blend" in material["shader"].lower()


def eqgMaterialSurface(material):
  shader = material["shader"].lower()
  if "lava" in shader:
    return surfaceCode["lava"]
  if "water" in shader:
    return surfaceCode["water"]
  if shader.startswith("chroma"):
    return surfaceCode["cutout"]
  if shader.startswith(("alpha", "addalpha")):
    return surfaceCode["translucent"]
  return surfaceCode["solid"]


def addEQGModel(builder, model, placement, isObject):
  vertices, triangles, uvs, keptTriangles = model["vertices"], model["triangles"], model["uvs"], slice(None)
  finite = numpy.isfinite(vertices).all(axis=1)
  # A few client models carry NaN vertices; the triangles using them cannot render, so they are dropped and reported.
  if not finite.all():
    keptTriangles = finite[triangles].all(axis=1)
    builder.droppedTriangles[placement["model"]] += int((~keptTriangles).sum())
    remap = numpy.cumsum(finite) - 1
    vertices, triangles, uvs = vertices[finite], remap[triangles[keptTriangles]], uvs[finite]
  materialTextures = numpy.array([builder.textureID(material["properties"].get("e_TextureDiffuse0", "").lower() or None) for material in model["materials"]] + [-1], dtype=numpy.int64)
  materialSurfaces = numpy.array([eqgMaterialSurface(material) for material in model["materials"]] + [surfaceCode["invisible"]], dtype=numpy.int8)
  materialPainted = numpy.array([eqgMaterialPainted(material) for material in model["materials"]] + [False], dtype=bool)
  # Material -1 (no material) indexes the trailing entry.
  triangleMaterials = model["triangleMaterials"][keptTriangles]
  materialIndices = numpy.where(triangleMaterials < 0, len(model["materials"]), triangleMaterials)
  surfaces = materialSurfaces[materialIndices]
  # A solid or invisible triangle flagged passable is passable, as a classic zone's passable solid triangles are.
  flagged = (model["triangleFlags"][keptTriangles] & eqgFiles.passableFlag).astype(bool) & numpy.isin(surfaces, (surfaceCode["solid"], surfaceCode["invisible"]))
  surfaces = numpy.where(flagged, surfaceCode["passable"], surfaces).astype(numpy.int8)
  placed = vertices @ placement["transform"].T + placement["position"] if "transform" in placement else eqgFiles.placeVertices(vertices, placement)
  builder.add(placed, triangles, materialTextures[materialIndices], surfaces, uvs, isObject, materialPainted[materialIndices])


def addPlacements(builder, library, placements, isObject):
  missingModels = set()
  placementCounts = collections.Counter()
  for placement in placements:
    placementCounts[placement["model"]] += 1
    model = library.model(placement["model"])
    if model is None:
      missingModels.add(placement["model"])
      continue
    addEQGModel(builder, model, placement, isObject)
  return placementCounts, sorted(missingModels)


def wldPlacementCounts(archive):
  if "objects.wld" not in archive.entries:
    return collections.Counter()
  objectsFile = eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld")
  return collections.Counter(objectsFile.lookupName(struct.unpack_from("<i", fragment.body, 4)[0]).lower() for fragment in objectsFile.fragmentsOfType(0x15))


def wldRegionNames(worldFile):
  return [fragment.name for fragment in worldFile.fragmentsOfType(0x29) for _ in range(struct.unpack_from("<I", fragment.body, 8)[0])]


def buildWLDGeometry(clientRoot, source):
  archive = eqArchive.EQArchive(source["archive"])
  worldFile = eqWorldFile.WorldFile(archive.read(f"{source['zone']}.wld"), source["zone"])
  builder = GeometryBuilder()
  for mesh in worldFile.meshes():
    materialTextures = numpy.array([builder.textureID(material["textureNames"][0] if material["textureNames"] else None) for material in mesh["materials"]] + [-1], dtype=numpy.int64)
    materialSurfaces = numpy.array([
      surfaceCode["invisible"] if material["renderMethod"] == eqWorldFile.invisibleRenderMethod
      else surfaceCode["cutout"] if material["renderMethod"] & 0xFF == wldMaskedRenderMethod
      else surfaceCode["solid"]
      for material in mesh["materials"]
    ] + [surfaceCode["invisible"]], dtype=numpy.int8)
    materialIndices = mesh["triangleMaterials"]
    if len(materialIndices) and int(materialIndices.max()) >= len(mesh["materials"]):
      raise ValueError(f"{source['zone']}: mesh '{mesh['name']}' uses material {int(materialIndices.max())} of {len(mesh['materials'])}")
    triangleSurfaces = numpy.where(mesh["isPassable"] & (materialSurfaces[materialIndices] == surfaceCode["solid"]), surfaceCode["passable"], materialSurfaces[materialIndices]).astype(numpy.int8)
    builder.add(mesh["vertices"], mesh["triangles"], materialTextures[materialIndices], triangleSurfaces, mesh["uvs"], False, False)
  geometry = builder.build(placementCounts=wldPlacementCounts(archive), regionNames=wldRegionNames(worldFile), missingModels=[], missingAssetArchives=[])
  return geometry | {"terrainBounds": (geometry["vertices"].min(0), geometry["vertices"].max(0))}


def buildEQGGeometry(clientRoot, source):
  archivePaths, missingArchives = zoneSources.assetArchivePaths(clientRoot, source)
  library = zoneSources.ModelLibrary(archivePaths, source["zone"])
  zonBytes = source["zonPath"].read_bytes() if "zonPath" in source else library.archives[0].read(source["zon"])
  zone = eqgFiles.parseZone(zonBytes, source["zone"])
  builder = GeometryBuilder()
  terrainPlacements = [placement for placement in zone["placements"] if placement["model"].endswith(".ter")]
  addPlacements(builder, library, terrainPlacements, False)
  terrainVertexCount = builder.vertexCount
  placementCounts, missingModels = addPlacements(builder, library, [placement for placement in zone["placements"] if not placement["model"].endswith(".ter")], True)
  if not builder.vertexChunks:
    raise ValueError(f"{source['zone']}: none of the {len(zone['placements'])} placements has a model in {[archive.archivePath.name for archive in library.archives]}")
  geometry = builder.build(placementCounts=placementCounts, regionNames=[region["name"] for region in zone["regions"]], missingModels=missingModels, missingAssetArchives=missingArchives)
  terrainVertices = geometry["vertices"][:terrainVertexCount]
  return geometry | {"terrainBounds": (terrainVertices.min(0), terrainVertices.max(0)) if terrainVertexCount else None}


def buildTerrainGeometry(clientRoot, source):
  archivePaths, missingArchives = zoneSources.assetArchivePaths(clientRoot, source)
  library = zoneSources.ModelLibrary(archivePaths, source["zone"])
  archive = library.archives[0]
  terrain = eqgTerrain.parseTerrain(archive.read(source["zon"]).decode("latin1"), archive.read(source["zon"][:-4] + ".dat"), source["zone"])
  quads = terrain["header"]["quadsPerTile"]
  spacing = terrain["header"]["unitsPerVertex"]
  rows, columns = numpy.meshgrid(numpy.arange(quads + 1), numpy.arange(quads + 1), indexing="ij")
  builder = GeometryBuilder()
  for tile in terrain["tiles"]:
    # Height rows run along y and columns along x, as the client lays out a tile's vertices.
    vertices = numpy.stack([tile["x"] + columns.ravel() * spacing, tile["y"] + rows.ravel() * spacing, tile["heights"].ravel().astype(numpy.float64)], axis=1)
    gridTriangles = eqgTerrain.tileTriangles(tile["quadFlags"])
    builder.add(vertices, gridTriangles, numpy.full(len(gridTriangles), builder.textureID(tile["baseLayer"]), dtype=numpy.int64), numpy.full(len(gridTriangles), surfaceCode["solid"], dtype=numpy.int8), None, False, False)
  tilesByOrigin = {(tile["x"], tile["y"]): tile for tile in terrain["tiles"]}
  missingModels, placementCounts = set(), collections.Counter()
  for placement in terrain["placements"]:
    placementCounts[placement["model"]] += 1
    model = library.model(placement["model"])
    if model is None:
      missingModels.add(placement["model"])
      continue
    transform = eqgTerrain.placementMatrix(placement["rotationDegrees"], placement["scale"])
    position = eqgTerrain.placedPosition(terrain, tilesByOrigin, placement)
    addEQGModel(builder, model, {"model": placement["model"], "transform": transform, "position": position}, True)
  minimum, maximum = eqgTerrain.terrainBounds(terrain)
  return builder.build(placementCounts=placementCounts, regionNames=terrain["regionNames"], missingModels=sorted(missingModels), missingAssetArchives=missingArchives) | {
    "terrainBounds": (numpy.array(minimum), numpy.array(maximum)),
    "tileShape": {"quadsPerTile": quads, "unitsPerVertex": spacing},
  }


geometryBuilders = {"wld": buildWLDGeometry, "eqgz": buildEQGGeometry, "eqtzp": buildTerrainGeometry}


def buildGeometry(clientRoot, source):
  return geometryBuilders[source["format"]](clientRoot, source) | {"format": source["format"]}
