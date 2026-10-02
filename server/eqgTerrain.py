"""EQ terrain zones (EQTZP): the .zon header, the .dat tiles as the client's terrain loader reads them (EQGraphicsDX9.dll 0x1010c0d0,
tiles at 0x101004a0), and the .eco ecosystems whose texture layers color them."""
import math
import re
import struct

import numpy

tileCoordinateOrigin = 100000
supportedTerrainVersions = ("4", None)
supportedDataVersions = (20, 21)
# Quad flag bit choosing the diagonal: clear splits the quad from its (0, 0) corner to (1, 1), set from (1, 0) to (0, 1)
# (the client's height query, 0x100f2fc0).
quadDiagonalFlag = 0x80
# The quad kind bits (set by 0x100f2100): 1 reads back as kind -1 and 4 as kind 1 (0x100f20c0), while 2, like no bit, reads back as the
# ordinary kind 0. How the client draws kinds -1 and 1 is not traced yet.
quadKindFlags = 0x05
ecosystemLayerKeys = {
  "MINHEIGHT": ("minHeight", float), "MAXHEIGHT": ("maxHeight", float), "HEIGHTTOL": ("heightTolerance", float),
  "MINSLOPE": ("minSlope", int), "MAXSLOPE": ("maxSlope", int), "SLOPETOL": ("slopeTolerance", int),
  "COLORMAP": ("coverMap", str), "COVERMAP": ("coverMap", str), "BLENDMAP": ("blendMap", str), "BLENDSOFTNESS": ("blendSoftness", int),
  "LAYERINGMAP": ("layeringMap", str), "COVERAGEMAP": ("layeringMap", str), "LAYERINGAREA": ("layeringArea", int),
  "DETAILMAP": ("detailMap", str), "DETAILREPEAT": ("detailRepeat", int), "NORMALMAP": ("normalMap", str), "NORMALREPEAT": ("normalRepeat", int),
}


class Reader:
  def __init__(self, data, sourceName):
    self.data = data
    self.sourceName = sourceName
    self.position = 0

  def read(self, layout):
    values = struct.unpack_from("<" + layout, self.data, self.position)
    self.position += struct.calcsize("<" + layout)
    return values[0] if len(values) == 1 else values

  def string(self):
    end = self.data.index(b"\0", self.position)
    value = self.data[self.position:end].decode("latin1")
    self.position = end + 1
    return value

  def array(self, dataType, count):
    values = numpy.frombuffer(self.data, dtype=dataType, count=count, offset=self.position)
    self.position += values.nbytes
    return values


def parseTerrainHeader(zonText, sourceName):
  def field(key, required=True):
    match = re.search(rf"\*{key}\s+(\S+)", zonText)
    if match is None and required:
      raise ValueError(f"{sourceName}: missing *{key}")
    return match.group(1) if match else None
  version = field("VERSION", required=False)
  if version not in supportedTerrainVersions:
    raise ValueError(f"{sourceName}: EQTZP version {version} is not supported")
  return {
    "name": field("NAME"),
    "version": version,
    "quadsPerTile": int(field("QUADSPERTILE")),
    "unitsPerVertex": float(field("UNITSPERVERT")),
  }


def tileOrigin(longitude, latitude, tileSize):
  return (longitude - tileCoordinateOrigin) * tileSize, (latitude - tileCoordinateOrigin) * tileSize


def readPlacement(reader, version, tileSize):
  """An object a tile places: its model, the ecosystem that placed it (empty for one placed by hand), and its position from its own
  tile's origin, z above the ground beneath it."""
  modelName = reader.string().lower()
  ecosystem = reader.string().lower() if version > 5 else ""
  longitude, latitude = reader.read("ii")
  x, y, z, rotationX, rotationY, rotationZ, scaleX, scaleY, scaleZ = reader.read("9f")
  if version > 15:
    reader.read("B")
  originX, originY = tileOrigin(longitude, latitude, tileSize)
  return {
    "model": modelName + ".mod", "ecosystem": ecosystem, "position": (originX + x, originY + y, z), "rotationDegrees": (rotationX, rotationY, rotationZ),
    "scale": (scaleX, scaleY, scaleZ),
  }


def parseTerrain(zonText, datBytes, sourceName):
  """EQTZP terrain: each tile's height grid, vertex colors (tint and baked light), quad flags, and ecosystem layers (the first
  covering the tile, each later one with its coverage mask), the objects and object groups the tiles place, in world units."""
  header = parseTerrainHeader(zonText, sourceName)
  quads = header["quadsPerTile"]
  vertexSide = quads + 1
  tileSize = quads * header["unitsPerVertex"]
  reader = Reader(datBytes, sourceName)
  version, _, _ = reader.read("III")
  if version not in supportedDataVersions:
    raise ValueError(f"{sourceName}: terrain data version {version} is not supported (only {supportedDataVersions})")
  baseTexture = reader.string().lower()
  tileCount = reader.read("I")
  tiles, placements, groups, regionNames = [], [], [], []
  for _ in range(tileCount):
    longitude, latitude, _ = reader.read("iii")
    heights = reader.array("<f4", vertexSide * vertexSide).reshape(vertexSide, vertexSide)
    tints = reader.array("<u4", vertexSide * vertexSide).reshape(vertexSide, vertexSide)
    baked = reader.array("<u4", vertexSide * vertexSide).reshape(vertexSide, vertexSide)
    quadFlags = reader.array("u1", quads * quads).reshape(quads, quads)
    reader.read("f")
    if version > 20:
      reader.read("i")
      if reader.read("B"):
        reader.read("4f")
    reader.read("f")
    layers = []
    for index in range(reader.read("I")):
      ecosystem = reader.string().lower()
      mask = None
      if index:
        maskSide = reader.read("I")
        mask = reader.array("u1", maskSide * maskSide).reshape(maskSide, maskSide)
      layers.append({"ecosystem": ecosystem, "mask": mask})
    tileX, tileY = tileOrigin(longitude, latitude, tileSize)
    tiles.append({
      "longitude": longitude, "latitude": latitude, "x": tileX, "y": tileY, "heights": heights, "tints": tints, "baked": baked, "quadFlags": quadFlags,
      "layers": layers, "baseLayer": layers[0]["ecosystem"] if layers else None,
    })
    for _ in range(reader.read("I")):
      placements.append(readPlacement(reader, version, tileSize))
    for _ in range(reader.read("I")):
      regionNames.append(reader.string())
      reader.read("i")
      reader.string()
      reader.read("II")
      reader.read("12f")
    for _ in range(reader.read("I")):
      reader.string()
      reader.string()
      reader.read("b")
      reader.read("II")
      reader.read("10f")
    for _ in range(reader.read("I")):
      name = reader.string().lower()
      groupLongitude, groupLatitude = reader.read("ii")
      x, y, z, rotationX, rotationY, rotationZ, scaleX, scaleY, scaleZ, _ = reader.read("10f")
      originX, originY = tileOrigin(groupLongitude, groupLatitude, tileSize)
      groups.append({
        "group": name, "position": (originX + x, originY + y, z), "rotationDegrees": (rotationX, rotationY, rotationZ), "scale": (scaleX, scaleY, scaleZ),
      })
  if reader.position != len(datBytes):
    raise ValueError(f"{sourceName}: terrain data ends at {reader.position} of {len(datBytes)} bytes")
  return {
    "header": header, "dataVersion": version, "baseTexture": baseTexture, "tileSize": tileSize, "tiles": tiles, "placements": placements,
    "groups": groups, "regionNames": regionNames,
  }


def tileTriangles(quadFlags):
  """A tile's grid triangles (vertex index row * (quads + 1) + column, counter-clockwise from above), each quad split along the
  diagonal its flag picks."""
  quads = quadFlags.shape[0]
  rows, columns = numpy.meshgrid(numpy.arange(quads), numpy.arange(quads), indexing="ij")
  corner = (rows * (quads + 1) + columns).ravel()
  right, up, upRight = corner + 1, corner + quads + 1, corner + quads + 2
  crossed = (quadFlags.ravel() & quadDiagonalFlag) != 0
  first = numpy.where(crossed[:, None], numpy.stack([corner, right, up], axis=1), numpy.stack([corner, right, upRight], axis=1))
  second = numpy.where(crossed[:, None], numpy.stack([right, upRight, up], axis=1), numpy.stack([corner, upRight, up], axis=1))
  return numpy.concatenate([first, second])


def quadKinds(terrain):
  """The unordinary quad kind bits any tile sets."""
  return sorted(set().union(*(set(numpy.unique(tile["quadFlags"] & quadKindFlags).tolist()) for tile in terrain["tiles"])) - {0})


def groundHeight(terrain, tilesByOrigin, x, y):
  """The height of the drawn ground at (x, y): the plane of the grid triangle beneath it."""
  tileSize, spacing = terrain["tileSize"], terrain["header"]["unitsPerVertex"]
  originX, originY = math.floor(x / tileSize) * tileSize, math.floor(y / tileSize) * tileSize
  tile = tilesByOrigin.get((originX, originY))
  if tile is None:
    raise ValueError(f"No terrain tile beneath ({x:.2f}, {y:.2f})")
  quads = terrain["header"]["quadsPerTile"]
  localX, localY = (x - originX) / spacing, (y - originY) / spacing
  column, row = min(int(localX), quads - 1), min(int(localY), quads - 1)
  fractionX, fractionY = localX - column, localY - row
  heights = tile["heights"]
  low, right, up, upRight = (float(heights[row, column]), float(heights[row, column + 1]), float(heights[row + 1, column]), float(heights[row + 1, column + 1]))
  if tile["quadFlags"][row, column] & quadDiagonalFlag:
    if fractionX + fractionY <= 1:
      return low + (right - low) * fractionX + (up - low) * fractionY
    return upRight + (up - upRight) * (1 - fractionX) + (right - upRight) * (1 - fractionY)
  if fractionX >= fractionY:
    return low + (right - low) * fractionX + (upRight - right) * fractionY
  return low + (upRight - up) * fractionX + (up - low) * fractionY


def terrainBounds(terrain):
  tiles = terrain["tiles"]
  minimum = (min(tile["x"] for tile in tiles), min(tile["y"] for tile in tiles), min(float(tile["heights"].min()) for tile in tiles))
  maximum = (max(tile["x"] for tile in tiles) + terrain["tileSize"], max(tile["y"] for tile in tiles) + terrain["tileSize"], max(float(tile["heights"].max()) for tile in tiles))
  return minimum, maximum


def parseEcosystem(ecoText, sourceName):
  """An ecosystem's texture layers (*TEXTUREPART), in file order, the first the one the others are weighed against."""
  match = re.search(r"\*TEXTUREPART(.*?)\*END_TEXTUREPART", ecoText, re.S)
  if match is None:
    raise ValueError(f"{sourceName}: no *TEXTUREPART")
  if "*CHILDLAYER" in match.group(1):
    raise ValueError(f"{sourceName}: child texture layers are not read yet")
  layers = []
  for layerMatch in re.finditer(r"\*LAYER\s+(\S+)(.*?)\*END_LAYER", match.group(1), re.S):
    layer = {"name": layerMatch.group(1)}
    for key, value in re.findall(r"\*(\w+)\s+(\S+)", layerMatch.group(2)):
      if key not in ecosystemLayerKeys:
        raise ValueError(f"{sourceName}: layer {layer['name']} has *{key}, which is not read")
      field, kind = ecosystemLayerKeys[key]
      layer[field] = value.lower() if kind is str else kind(float(value)) if kind is int else kind(value)
    layers.append(layer)
  if not layers:
    raise ValueError(f"{sourceName}: no texture layers")
  return layers


def placementMatrix(rotationDegrees, scale):
  """A terrain placement's scale, then its turns in degrees about X, Y, and Z, in that order. The order of the two tilts is unverified;
  only a few hand-placed rocks tilt."""
  rotationX, rotationY, rotationZ = (math.radians(angle) for angle in rotationDegrees)
  cosineX, sineX = math.cos(rotationX), math.sin(rotationX)
  cosineY, sineY = math.cos(rotationY), math.sin(rotationY)
  cosineZ, sineZ = math.cos(rotationZ), math.sin(rotationZ)
  aroundX = numpy.array([[1, 0, 0], [0, cosineX, -sineX], [0, sineX, cosineX]])
  aroundY = numpy.array([[cosineY, 0, sineY], [0, 1, 0], [-sineY, 0, cosineY]])
  aroundZ = numpy.array([[cosineZ, -sineZ, 0], [sineZ, cosineZ, 0], [0, 0, 1]])
  return aroundZ @ aroundY @ aroundX @ numpy.diag(scale)


def placedPosition(terrain, tilesByOrigin, placement):
  """Where a placement stands: its x and y, and its z above the ground there."""
  x, y, z = placement["position"]
  return numpy.array((x, y, groundHeight(terrain, tilesByOrigin, x, y) + z))
