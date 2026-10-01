import re
import struct

import numpy

tileCoordinateOrigin = 100000
supportedTerrainVersions = ("4", None)


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


def parseTerrain(zonText, datBytes, sourceName):
  """EQTZP terrain: per-tile height grids and model placements, in world units."""
  header = parseTerrainHeader(zonText, sourceName)
  quads = header["quadsPerTile"]
  vertexSide = quads + 1
  tileSize = quads * header["unitsPerVertex"]
  reader = Reader(datBytes, sourceName)
  flags, _, _ = reader.read("III")
  reader.string()
  tileCount = reader.read("I")
  tiles = []
  placements = []
  regionNames = []
  for _ in range(tileCount):
    longitude, latitude, _ = reader.read("iii")
    heights = reader.array("<f4", vertexSide * vertexSide).reshape(vertexSide, vertexSide)
    reader.array("<u4", vertexSide * vertexSide * 2)
    reader.array("u1", quads * quads)
    reader.read("f")
    if reader.read("f") > 0:
      if reader.read("b") > 0:
        reader.read("ffff")
      reader.read("f")
    layerCount = reader.read("I")
    baseLayer = None
    if layerCount:
      baseLayer = reader.string().lower()
      for _ in range(1, layerCount):
        reader.string()
        maskSide = reader.read("I")
        reader.array("u1", maskSide * maskSide)
    tileX = (longitude - tileCoordinateOrigin) * tileSize
    tileY = (latitude - tileCoordinateOrigin) * tileSize
    tiles.append({"x": tileX, "y": tileY, "heights": heights, "baseLayer": baseLayer})
    for _ in range(reader.read("I")):
      # Placements name the model without its .mod extension.
      modelName = reader.string().lower()
      reader.string()
      reader.read("II")
      x, y, z, rotationX, rotationY, rotationZ, scaleX, scaleY, scaleZ = reader.read("9f")
      reader.read("B")
      if flags & 2:
        reader.read("I")
      placements.append({"model": modelName + ".mod", "position": (tileX + x, tileY + y, z), "rotation": (rotationZ, rotationY, rotationX), "scale": scaleX})
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
      reader.string()
      reader.read("II")
      reader.read("10f")
  if reader.position != len(datBytes):
    raise ValueError(f"{sourceName}: terrain data ends at {reader.position} of {len(datBytes)} bytes")
  return {"header": header, "tileSize": tileSize, "tiles": tiles, "placements": placements, "regionNames": regionNames}


def terrainBounds(terrain):
  tiles = terrain["tiles"]
  minimum = (min(tile["x"] for tile in tiles), min(tile["y"] for tile in tiles), min(float(tile["heights"].min()) for tile in tiles))
  maximum = (max(tile["x"] for tile in tiles) + terrain["tileSize"], max(tile["y"] for tile in tiles) + terrain["tileSize"], max(float(tile["heights"].max()) for tile in tiles))
  return minimum, maximum
