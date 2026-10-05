"""EQEmu's server map files for an EQG zone, written and read as the server reads them (docs/serverFiles.md): the collision map (.map V2)
and the server's rebuild of it into triangles, the region boxes (.wtr V2), and the nav mesh container (.nav). Every reader decodes to
the last byte or raises, naming the offset. Coordinates are in three frames: zone (the .zon, .ter and .mod, and Blender), server
(zone y, zone x, z) and Recast (zone y, z, zone x)."""
import math
import struct
import zlib

import numpy

import eqArchive
import eqgFiles

mapVersion = 0x02000000
mapVersionOne = 0x01000000
mapFileHeader = struct.Struct("<3I")
mapHeader = struct.Struct("<9If")
countLayout = struct.Struct("<I")
countPair = struct.Struct("<2I")
placementValues = struct.Struct("<9f")
modelPolygonType = numpy.dtype([("indices", "<u4", 3), ("vis", "u1")])
waterMagic = b"EQEMUWATER"
waterVersion = 2
waterRecord = struct.Struct("<I12f")
# The server's region types (water_map.h) by .zon region prefix: the three zonewright writes, and APK_, which Peridot's freeporteast.wtr
# gives type 4 for freeporteast.zon's APK_01. awater writes an unknown prefix as Water, and matches case-sensitively (water_map.cpp:258);
# how the client reads a prefix in another case is untraced. This table refuses both.
waterRegionTypes = {"AWT_": 1, "ALV_": 2, "ATP_": 3, "APK_": 4}
waterRegionTypeNames = {
  0: "Normal", 1: "Water", 2: "Lava", 3: "ZoneLine", 4: "PvP", 5: "Slime", 6: "Ice", 7: "VWater", 8: "GeneralArea", 9: "PreferPathing",
  10: "DisableNavMesh",
}
navAreaNames = {0: "Normal", 1: "Water", 2: "Lava", 3: "ZoneLine", 4: "PvP", 5: "Slime", 6: "Ice", 7: "VWater", 8: "GeneralArea", 9: "Portal", 10: "Prefer", 11: "Disabled"}
navMagic = b"EQNAVMESH"
navVersion = 2
navFileHeader = struct.Struct("<9s3I")
navParameters = struct.Struct("<5f2i")
navTileEntry = struct.Struct("<Ii")
detourMagic = ord("D") << 24 | ord("N") << 16 | ord("A") << 8 | ord("V")
detourVersion = 7
tileHeader = struct.Struct("<5iI9i10f")
tileCountFields = ("polygonCount", "vertexCount", "maximumLinkCount", "detailMeshCount", "detailVertexCount", "detailTriangleCount",
  "boundingVolumeNodeCount", "offMeshConnectionCount")
tileHeaderFields = ("magic", "version", "x", "y", "layer", "userID") + tileCountFields + ("offMeshBase", "walkableHeight", "walkableRadius",
  "walkableClimb", "boundsLow", "boundsHigh", "quantizeFactor")
navPolygonType = numpy.dtype([("firstLink", "<u4"), ("vertices", "<u2", 6), ("neighbours", "<u2", 6), ("flags", "<u2"), ("vertexCount", "u1"), ("areaAndType", "u1")])
navLinkType = numpy.dtype([("reference", "<u4"), ("next", "<u4"), ("edge", "u1"), ("side", "u1"), ("low", "u1"), ("high", "u1")])
detailMeshType = numpy.dtype([("vertexBase", "<u4"), ("triangleBase", "<u4"), ("vertexCount", "u1"), ("triangleCount", "u1"), ("padding", "u1", 2)])
boundingVolumeNodeType = numpy.dtype([("low", "<u2", 3), ("high", "<u2", 3), ("index", "<i4")])
offMeshConnectionType = numpy.dtype([("positions", "<f4", 6), ("radius", "<f4"), ("polygon", "<u2"), ("flags", "u1"), ("side", "u1"), ("userID", "<u4")])
# In file order.
tileSections = (
  ("vertices", numpy.dtype(("<f4", 3)), "vertexCount"),
  ("polygons", navPolygonType, "polygonCount"),
  ("links", navLinkType, "maximumLinkCount"),
  ("detailMeshes", detailMeshType, "detailMeshCount"),
  ("detailVertices", numpy.dtype(("<f4", 3)), "detailVertexCount"),
  ("detailTriangles", numpy.dtype(("u1", 4)), "detailTriangleCount"),
  ("boundingVolumeNodes", boundingVolumeNodeType, "boundingVolumeNodeCount"),
  ("offMeshConnections", offMeshConnectionType, "offMeshConnectionCount"),
)


class ByteReader:
  """A cursor over bytes that refuses to read past their end or to stop short of it, naming the offset."""

  def __init__(self, data, source):
    self.data, self.source, self.offset = data, source, 0

  def take(self, size, what):
    if self.offset + size > len(self.data):
      raise ValueError(f"{self.source}: {what} at byte {self.offset} needs {size} bytes; {len(self.data) - self.offset} remain")
    start = self.offset
    self.offset += size
    return start

  def values(self, layout, what):
    return layout.unpack_from(self.data, self.take(layout.size, what))

  def array(self, dtype, count, what):
    dtype = numpy.dtype(dtype)
    start = self.take(dtype.itemsize * count, what)
    return numpy.frombuffer(self.data, dtype=dtype, count=count, offset=start).copy()

  def string(self, what):
    end = self.data.find(b"\0", self.offset)
    if end < 0:
      raise ValueError(f"{self.source}: {what} at byte {self.offset} has no terminating zero")
    text = self.data[self.offset:end].decode("latin1")
    self.offset = end + 1
    return text

  def finish(self, what):
    if self.offset != len(self.data):
      raise ValueError(f"{self.source}: {len(self.data) - self.offset} bytes follow the {what}, from byte {self.offset}")


def inflated(stream, expectedSize, source, start):
  """A zlib stream inflated whole, refusing one that is cut short, carries bytes past its end, or inflates to another size than the
  header says (the server checks none of these: map.cpp:456)."""
  inflater = zlib.decompressobj()
  try:
    data = inflater.decompress(stream)
  except zlib.error as error:
    raise ValueError(f"{source}: the zlib stream from byte {start} does not inflate: {error}") from error
  if not inflater.eof:
    raise ValueError(f"{source}: the zlib stream from byte {start} ends before its last block")
  if inflater.unused_data:
    raise ValueError(f"{source}: {len(inflater.unused_data)} bytes follow the zlib stream, from byte {start + len(stream) - len(inflater.unused_data)}")
  if len(data) != expectedSize:
    raise ValueError(f"{source}: the zlib stream from byte {start} inflates to {len(data)} bytes; the header says {expectedSize}")
  return data


def finiteValues(values, what):
  if not numpy.isfinite(numpy.asarray(values, dtype=numpy.float64)).all():
    raise ValueError(f"{what} holds a number that is not finite")


def zoneFilesOf(archiveBytes, looseZon=None):
  """What a zone's .map is built from: its .zon (the archive's one .zon, or the loose .zon the client loads in its place) and, by
  archive name, every model file the .zon names that the archive holds."""
  archive = eqArchive.EQArchive("zone archive", archiveBytes)
  if looseZon is None:
    zoneNames = [name for name in archive.entries if name.endswith(".zon")]
    if len(zoneNames) != 1:
      raise ValueError(f"The zone archive holds {len(zoneNames)} .zon files; without a loose .zon, a zone archive holds one")
    looseZon = archive.read(zoneNames[0])
  modelNames = eqgFiles.parseZone(looseZon, "the .zon")["modelNames"]
  return {"zon": looseZon, "models": {name: archive.read(name) for name in dict.fromkeys(modelNames) if name in archive.entries}}


def indexedVertices(corners):
  """Triangle corners as one vertex list and indices into it: each distinct float32 position once, in first-seen order, -0.0 the same
  as 0.0 and the first spelling kept (azone's AddFace, map.cpp:954)."""
  points = corners.reshape(-1, 3)
  keys = (points + numpy.float32(0)).view(numpy.uint32)
  _, first, inverse = numpy.unique(keys, axis=0, return_index=True, return_inverse=True)
  order = numpy.argsort(first)
  rank = numpy.empty(len(first), dtype=numpy.uint32)
  rank[order] = numpy.arange(len(first), dtype=numpy.uint32)
  return points[first[order]], rank[inverse.ravel()]


def mapContent(zoneFiles):
  """azone's collision map of a zone's files (map.cpp:636-740): terrain triangles baked into the collidable and non-collidable lists,
  each placed model under its map name (the .zon's spelling of it in the placement's model entry, ')' written '_', eqg_loader.cpp:103,
  so two spellings of one file are two models), and each other placement with its turns in radians as the .zon stores them. Refuses
  what azone or the server would drop or place apart from the client, naming it."""
  zone = eqgFiles.parseZone(zoneFiles["zon"], "the .zon")
  parsedModels, mapModels, placements = {}, {}, []
  terrainCorners, terrainPassable = [], []
  for index, placement in enumerate(zone["placements"]):
    archiveName = placement["model"]
    mapName = placement["modelFileName"].replace(")", "_")
    label = f"Placement {index} '{placement['name']}' of {mapName}"
    if archiveName not in zoneFiles["models"]:
      raise ValueError(f"{label}: the zone's files hold no {archiveName}; azone would drop the placement and the server would have no collision for it")
    if archiveName not in parsedModels:
      parsedModels[archiveName] = eqgFiles.parseModel(zoneFiles["models"][archiveName], archiveName)
      finiteValues(parsedModels[archiveName]["vertices"], f"Model {mapName}'s vertices")
    model = parsedModels[archiveName]
    bakedByAzone = placement["name"].startswith("TER") or mapName.endswith(".ter")
    terrainForClient = archiveName.endswith(".ter")
    if bakedByAzone and not terrainForClient:
      raise ValueError(f"{label}: its name starts TER, so azone would bake it as terrain where its vertices are, while the client places it")
    if terrainForClient and not bakedByAzone:
      raise ValueError(f"{label}: azone would place a terrain whose file ends otherwise than .ter as a model, while the client draws it where its vertices are")
    if terrainForClient:
      terrainCorners.append(model["vertices"].astype(numpy.float32)[model["triangles"]][:, :, [1, 0, 2]])
      terrainPassable.append((model["triangleFlags"] & eqgFiles.passableFlag) != 0)
      continue
    if mapModels.setdefault(mapName, archiveName) != archiveName:
      raise ValueError(f"Models {mapModels[mapName]} and {archiveName} share the map name {mapName}; the server would place one for both")
    heading, turnY, turnX = placement["rotation"]
    values = numpy.array([*placement["position"], turnX, turnY, heading, *[placement["scale"]] * 3], dtype=numpy.float32)
    finiteValues(values, label)
    placements.append({"name": mapName, "position": values[0:3], "rotation": values[3:6], "scale": values[6:9]})
  corners = numpy.concatenate(terrainCorners) if terrainCorners else numpy.zeros((0, 3, 3), dtype=numpy.float32)
  passable = numpy.concatenate(terrainPassable) if terrainPassable else numpy.zeros(0, dtype=bool)
  collidableVertices, collidableIndices = indexedVertices(corners[~passable])
  nonCollidableVertices, nonCollidableIndices = indexedVertices(corners[passable])
  models = []
  for mapName in sorted(mapModels, key=lambda name: name.encode("latin1")):
    model = parsedModels[mapModels[mapName]]
    polygons = numpy.zeros(len(model["triangles"]), dtype=modelPolygonType)
    polygons["indices"] = model["triangles"]
    polygons["vis"] = (model["triangleFlags"] & eqgFiles.passableFlag) == 0
    models.append({"name": mapName, "vertices": model["vertices"].astype(numpy.float32), "polygons": polygons})
  return {
    "collidableVertices": collidableVertices, "collidableIndices": collidableIndices,
    "nonCollidableVertices": nonCollidableVertices, "nonCollidableIndices": nonCollidableIndices,
    "models": models, "placements": placements,
  }


def mapPayload(content):
  """A collision map's inflated bytes (azone map.cpp:54-370): the header (no placement groups or terrain tiles), both triangle lists,
  the models, and the placements."""
  parts = [mapHeader.pack(len(content["collidableVertices"]), len(content["collidableIndices"]), len(content["nonCollidableVertices"]),
    len(content["nonCollidableIndices"]), len(content["models"]), len(content["placements"]), 0, 0, 0, 0.0)]
  for key, dtype in (("collidableVertices", "<f4"), ("collidableIndices", "<u4"), ("nonCollidableVertices", "<f4"), ("nonCollidableIndices", "<u4")):
    parts.append(numpy.ascontiguousarray(content[key], dtype=dtype).tobytes())
  for model in content["models"]:
    parts.append(model["name"].encode("latin1") + b"\0" + countPair.pack(len(model["vertices"]), len(model["polygons"])))
    parts.append(numpy.ascontiguousarray(model["vertices"], dtype="<f4").tobytes() + numpy.ascontiguousarray(model["polygons"], dtype=modelPolygonType).tobytes())
  for placement in content["placements"]:
    parts.append(placement["name"].encode("latin1") + b"\0" + placementValues.pack(*placement["position"], *placement["rotation"], *placement["scale"]))
  return b"".join(parts)


def mapFile(payload):
  """A V2 .map: the version word, the compressed and inflated sizes, then the payload deflated at zlib's default level."""
  compressed = zlib.compress(payload)
  return mapFileHeader.pack(mapVersion, len(compressed), len(payload)) + compressed


def mapBytes(zoneFiles):
  return mapFile(mapPayload(mapContent(zoneFiles)))


def readMap(data, source="the .map"):
  """A V2 .map decoded to its last byte into mapContent's form; a V1 map is recognized and refused, and so are placement groups and
  terrain tiles (EQG v4 and EQ terrain zones), which an EQG zone's map never holds."""
  if len(data) < 4:
    raise ValueError(f"{source}: {len(data)} bytes hold no version word")
  version = struct.unpack_from("<I", data, 0)[0]
  if version == mapVersionOne:
    raise ValueError(f"{source}: a V1 map (a list of faces, as azone once wrote); zonewright reads V2 maps only")
  if version != mapVersion:
    raise ValueError(f"{source}: version word {version:#010x} at byte 0 is not a V2 map's {mapVersion:#010x}")
  _, compressedSize, inflatedSize = ByteReader(data, source).values(mapFileHeader, "file header")
  if mapFileHeader.size + compressedSize != len(data):
    raise ValueError(f"{source}: the header's {compressedSize}-byte stream from byte {mapFileHeader.size} ends at byte {mapFileHeader.size + compressedSize}, the file at byte {len(data)}")
  reader = ByteReader(inflated(data[mapFileHeader.size:], inflatedSize, source, mapFileHeader.size), f"{source} (inflated)")
  vertexCount, indexCount, nonCollidableVertexCount, nonCollidableIndexCount, modelCount, placementCount, groupCount, tileCount, quadsPerTile, unitsPerVertex = reader.values(mapHeader, "header")
  if groupCount or tileCount or quadsPerTile or unitsPerVertex:
    raise ValueError(f"{reader.source}: {groupCount} placement groups and {tileCount} terrain tiles ({quadsPerTile} quads per tile, {unitsPerVertex} units per vertex); zonewright reads EQG zone maps, which hold none")
  content = {}
  for key, dtype, count, listCount in (("collidableVertices", ("<f4", 3), vertexCount, None), ("collidableIndices", "<u4", indexCount, vertexCount),
      ("nonCollidableVertices", ("<f4", 3), nonCollidableVertexCount, None), ("nonCollidableIndices", "<u4", nonCollidableIndexCount, nonCollidableVertexCount)):
    start = reader.offset
    content[key] = reader.array(dtype, count, key)
    if listCount is not None and (count % 3 or (count and int(content[key].max()) >= listCount)):
      raise ValueError(f"{reader.source}: the {count} {key} from byte {start} are not whole triangles of indices under {listCount}")
  content["models"], names = [], {}
  for index in range(modelCount):
    start = reader.offset
    name = reader.string(f"model {index}'s name")
    if name in names:
      raise ValueError(f"{reader.source}: model {index} at byte {start} is named {name}, as model {names[name]} is; the server keeps the last")
    names[name] = index
    modelVertexCount, polygonCount = reader.values(countPair, f"model {name}'s counts")
    vertices = reader.array(("<f4", 3), modelVertexCount, f"model {name}'s vertices")
    polygonStart = reader.offset
    polygons = reader.array(modelPolygonType, polygonCount, f"model {name}'s polygons")
    if polygonCount and int(polygons["indices"].max()) >= modelVertexCount:
      raise ValueError(f"{reader.source}: model {name}'s polygons from byte {polygonStart} index past its {modelVertexCount} vertices")
    content["models"].append({"name": name, "vertices": vertices, "polygons": polygons})
  content["placements"] = []
  for index in range(placementCount):
    name = reader.string(f"placement {index}'s name")
    values = numpy.array(reader.values(placementValues, f"placement {index} ({name})"), dtype=numpy.float32)
    content["placements"].append({"name": name, "position": values[0:3], "rotation": values[3:6], "scale": values[6:9]})
  reader.finish("placements")
  return content


def turned(points, turns):
  """Points turned about X, then Y, then Z, one float32 operation at a time, as the server turns a placement's vertices."""
  x, y, z = points[..., 0], points[..., 1], points[..., 2]
  for axis, turn in enumerate(turns):
    cosine, sine = numpy.float32(math.cos(float(turn))), numpy.float32(math.sin(float(turn)))
    if axis == 0:
      y, z = cosine * y - sine * z, sine * y + cosine * z
    elif axis == 1:
      x, z = cosine * x + sine * z, -(sine * x) + cosine * z
    else:
      x, y = cosine * x - sine * y, sine * x + cosine * y
  return numpy.stack([x, y, z], axis=-1)


def collisionTriangles(content, source="the .map"):
  """The server's collision from a decoded map (map.cpp LoadV2): the collidable list as stored, then each placement's polygons marked
  vis, turned, scaled, moved and swapped into server axes, in float32. A placement naming no model in the map raises, where the
  server would skip it."""
  vertices = content["collidableVertices"]
  parts = [vertices[content["collidableIndices"]].reshape(-1, 3, 3)]
  models = {model["name"]: model for model in content["models"]}
  for index, placement in enumerate(content["placements"]):
    if placement["name"] not in models:
      raise ValueError(f"{source}: placement {index} names model {placement['name']}, which the map does not hold; the server would skip it")
    model = models[placement["name"]]
    corners = model["vertices"][model["polygons"]["indices"][model["polygons"]["vis"] != 0]]
    placed = turned(corners, placement["rotation"]) * placement["scale"] + placement["position"]
    parts.append(placed[..., [1, 0, 2]])
  return numpy.concatenate(parts).astype(numpy.float32)


def mapCollision(data, source="the .map"):
  """Float32 triangles (n, 3 corners, xyz) in server axes, as the server builds them from a .map's bytes."""
  return collisionTriangles(readMap(data, source), source)


def inZoneAxes(serverPoints):
  return serverPoints[..., [1, 0, 2]]


def inRecastAxes(serverPoints):
  return serverPoints[..., [0, 2, 1]]


def waterBytes(regions):
  """A V2 .wtr of .zon regions (eqgFiles.parseZone's), in their order: each its type by prefix (waterRegionTypes, matched in its case,
  as awater matches them), center, no turn, scale 1, and half extents as stored, signs included (the server swaps a negative extent
  into its box's low and high, oriented_bounding_box.cpp:74). Refuses an unknown prefix, a turned region, a zero extent, and a number
  that is not finite."""
  records = []
  for region in regions:
    name = region["name"]
    prefix = name[:4]
    if prefix not in waterRegionTypes:
      raise ValueError(f"Region '{name}': no server region type for its prefix (known, in this case: {', '.join(waterRegionTypes)})")
    finiteValues([*region["center"], *region["rotation"], *region["halfExtents"]], f"Region '{name}'")
    if any(region["rotation"]):
      raise ValueError(f"Region '{name}' is turned {list(region['rotation'])}; zonewright writes unturned regions, and the .zon turn's unit is unsettled")
    if not all(region["halfExtents"]):
      raise ValueError(f"Region '{name}' has a zero half extent: {list(region['halfExtents'])}")
    records.append(waterRecord.pack(waterRegionTypes[prefix], *region["center"], 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, *region["halfExtents"]))
  return waterMagic + struct.pack("<2I", waterVersion, len(records)) + b"".join(records)


def readWater(data, source="the .wtr"):
  """A V2 .wtr's region boxes (type, position, rotation in degrees, scale, half extents), decoded to its last byte. A V1 .wtr (the BSP
  tree of an S3D zone) is recognized and refused. A file cut short is refused; the server would log "Loaded Water Map" and drop it
  (water_map.cpp:48-63)."""
  reader = ByteReader(data, source)
  reader.take(len(waterMagic), "magic")
  if data[:len(waterMagic)] != waterMagic:
    raise ValueError(f"{source}: magic {data[:len(waterMagic)]!r} at byte 0 is not {waterMagic!r}")
  version = reader.values(countLayout, "version")[0]
  if version == 1:
    raise ValueError(f"{source}: a V1 water map (the BSP tree of an S3D zone); zonewright reads V2 only")
  if version != waterVersion:
    raise ValueError(f"{source}: version {version} at byte {len(waterMagic)} is not {waterVersion}")
  count = reader.values(countLayout, "region count")[0]
  regions = []
  for index in range(count):
    values = reader.values(waterRecord, f"region {index}")
    regions.append({"type": values[0], "position": values[1:4], "rotation": values[4:7], "scale": values[7:10], "halfExtents": values[10:13]})
  reader.finish(f"{count} regions")
  return regions


def readTile(data, index, reference, source):
  """One Detour tile, its size checked against its header's counts first: Detour's addTile reads and writes by the counts and never
  checks them against the size."""
  reader = ByteReader(data, f"{source}, tile {index} (reference {reference})")
  values = reader.values(tileHeader, "header")
  header = dict(zip(tileHeaderFields[:18], values[:18])) | {"boundsLow": values[18:21], "boundsHigh": values[21:24], "quantizeFactor": values[24]}
  if header["magic"] != detourMagic or header["version"] != detourVersion:
    raise ValueError(f"{reader.source}: magic {header['magic']:#x} and version {header['version']} at byte 0 are not Detour's {detourMagic:#x} and {detourVersion}")
  where = f"{reader.source} ({header['x']}, {header['y']}, layer {header['layer']})"
  negative = [field for field in tileCountFields if header[field] < 0]
  if negative:
    raise ValueError(f"{where}: its header's {negative[0]} is {header[negative[0]]}, below zero")
  expected = tileHeader.size + sum(header[countField] * dtype.itemsize for _, dtype, countField in tileSections)
  if len(data) != expected:
    raise ValueError(f"{where} is {len(data)} bytes, but its header's counts make {expected}")
  tile = {"reference": reference, "header": header}
  for key, dtype, countField in tileSections:
    tile[key] = reader.array(dtype, header[countField], key)
  return tile


def readNav(data, source="the .nav"):
  """A .nav's parameters (dtNavMeshParams) and tiles (Detour DNAV version 7: reference, header, vertices, polygons, links, detail
  meshes, detail vertices and triangles, bounding-volume nodes and off-mesh connections), decoded to the last byte (readNavPayload)."""
  reader = ByteReader(data, source)
  magic, version, compressedSize, inflatedSize = reader.values(navFileHeader, "file header")
  if magic != navMagic:
    raise ValueError(f"{source}: magic {magic!r} at byte 0 is not {navMagic!r}")
  if version != navVersion:
    raise ValueError(f"{source}: version {version} at byte {len(navMagic)} is not {navVersion}")
  if navFileHeader.size + compressedSize != len(data):
    raise ValueError(f"{source}: the header's {compressedSize}-byte stream from byte {navFileHeader.size} ends at byte {navFileHeader.size + compressedSize}, the file at byte {len(data)}")
  return readNavPayload(inflated(data[navFileHeader.size:], inflatedSize, source, navFileHeader.size), f"{source} (inflated)")


def readNavPayload(data, source):
  """A nav payload (a .nav's inflated bytes) decoded to the last byte. A zero tile reference or size is refused, naming the tile: the
  server drops the whole mesh on one (pathfinder_nav_mesh.cpp:473-491)."""
  payload = ByteReader(data, source)
  tileCount = payload.values(countLayout, "tile count")[0]
  values = payload.values(navParameters, "parameters")
  parameters = {"origin": values[0:3], "tileWidth": values[3], "tileHeight": values[4], "maximumTiles": values[5], "maximumPolygons": values[6]}
  tiles = []
  for index in range(tileCount):
    entryStart = payload.offset
    reference, size = payload.values(navTileEntry, f"tile {index}'s reference and size")
    if reference == 0 or size <= 0:
      raise ValueError(f"{payload.source}: tile {index} at byte {entryStart} has reference {reference} and size {size}; the server drops the whole mesh on a zero reference or size")
    start = payload.take(size, f"tile {index}'s data")
    tiles.append(readTile(payload.data[start:start + size], index, reference, source))
  payload.finish(f"{tileCount} tiles")
  return {"parameters": parameters, "tiles": tiles}


def tileBytes(tile, index):
  header = tile["header"]
  for key, dtype, countField in tileSections:
    if numpy.shape(tile[key]) != (header[countField], *dtype.shape):
      raise ValueError(f"Tile {index}: {key} of shape {numpy.shape(tile[key])} where its header counts {header[countField]} records of shape {dtype.shape}")
  values = [header[field] for field in tileHeaderFields[:18]] + [*header["boundsLow"], *header["boundsHigh"], header["quantizeFactor"]]
  return tileHeader.pack(*values) + b"".join(numpy.ascontiguousarray(tile[key], dtype=dtype.base if dtype.subdtype else dtype).tobytes() for key, dtype, _ in tileSections)


def navPayload(nav):
  """A nav mesh's inflated bytes: the tile count, the parameters, then each tile's reference, size and data, in the given order."""
  parameters = nav["parameters"]
  parts = [countLayout.pack(len(nav["tiles"])), navParameters.pack(*parameters["origin"], parameters["tileWidth"], parameters["tileHeight"],
    parameters["maximumTiles"], parameters["maximumPolygons"])]
  for index, tile in enumerate(nav["tiles"]):
    data = tileBytes(tile, index)
    parts.append(navTileEntry.pack(tile["reference"], len(data)) + data)
  return b"".join(parts)


def navFile(payload):
  """A .nav container: EQNAVMESH, version 2, the compressed and inflated sizes, then the payload deflated. The payload is first read as
  readNav reads it, so no file is written that the reader would refuse."""
  readNavPayload(payload, "the nav payload")
  compressed = zlib.compress(payload)
  return navFileHeader.pack(navMagic, navVersion, len(compressed), len(payload)) + compressed
