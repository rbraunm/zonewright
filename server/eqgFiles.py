import re
import struct

import numpy

supportedModelVersions = (1, 2, 3)
supportedZoneVersions = (1, 2)
modelVertexTypes = {
  1: numpy.dtype([("position", "<f4", 3), ("normal", "<f4", 3), ("uv", "<f4", 2)]),
  2: numpy.dtype([("position", "<f4", 3), ("normal", "<f4", 3), ("uv", "<f4", 2)]),
  3: numpy.dtype([("position", "<f4", 3), ("normal", "<f4", 3), ("color", "<u4"), ("uv", "<f4", 2), ("secondUV", "<f4", 2)]),
}
modelTriangleType = numpy.dtype([("indices", "<u4", 3), ("material", "<i4"), ("flags", "<u4")])
# A triangle's flags: 0x1 lets players through it (the client's collision tests skip it, EQGraphicsDX9.dll 0x100c6bfd), as the
# client's own zones flag liquid surfaces and cutout cards. Material -1 is no material: never drawn (the draw batches skip it,
# 0x100621a3) but still collided with, an invisible wall.
passableFlag = 0x1
noMaterial = -1
boneType = numpy.dtype([
  ("name", "<u4"), ("next", "<i4"), ("childCount", "<u4"), ("firstChild", "<i4"),
  ("position", "<f4", 3), ("rotation", "<f4", 4), ("scale", "<f4", 3),
])
weightType = numpy.dtype([("count", "<u4"), ("influences", [("bone", "<i4"), ("weight", "<f4")], 4)])
animationFrameType = numpy.dtype([("time", "<u4"), ("position", "<f4", 3), ("rotation", "<f4", 4), ("scale", "<f4", 3)])
animationHeaderBytes = {1: 16, 2: 20}
zoneRegionBytes = 40
# A region's first turn field counts 512ths of a turn: read so, the boxes of the client's water regions written in that unit (most store
# -128, a quarter turn) lie over their water, and those written in radians (-pi/2 in most) turn by about a degree and lie over theirs
# unturned (docs/clientRendering.md, Regions).
regionTurnUnits = 512
zoneLinePrefix = "ATP_"
skinnedWeightBytes = weightType.itemsize
zoneLightBytes = 32
layerRecordBytes = 32


def readString(stringTable, offset):
  return stringTable[offset:stringTable.index(b"\0", offset)].decode("latin1")


def readBones(modelBytes, position, boneCount, stringTable):
  """Bone records: name, next sibling, child count, first child, and the bind transform (position, rotation x y z w, scale)."""
  records = numpy.frombuffer(modelBytes, dtype=boneType, count=boneCount, offset=position)
  return {
    "names": [readString(stringTable, offset).upper() for offset in records["name"]],
    "next": records["next"].astype(numpy.int64), "childCount": records["childCount"].astype(numpy.int64), "firstChild": records["firstChild"].astype(numpy.int64),
    "position": records["position"].astype(numpy.float64), "rotation": records["rotation"].astype(numpy.float64), "scale": records["scale"].astype(numpy.float64),
  }


def parseModel(modelBytes, sourceName):
  """EQGM (.mod) or EQGT (.ter) versions 1-3: materials, vertices, triangles, and for a skinned EQGM its bones and one weight record per vertex."""
  magic = modelBytes[:4]
  boneCount = 0
  if magic == b"EQGT":
    version, stringLength, materialCount, vertexCount, triangleCount = struct.unpack_from("<5I", modelBytes, 4)
    position = 24
  elif magic == b"EQGM":
    version, stringLength, materialCount, vertexCount, triangleCount, boneCount = struct.unpack_from("<6I", modelBytes, 4)
    position = 28
  else:
    raise ValueError(f"{sourceName}: magic {magic!r} is not EQGM or EQGT")
  if version not in supportedModelVersions:
    raise ValueError(f"{sourceName}: model version {version} is not supported (only {supportedModelVersions})")
  stringTable = modelBytes[position:position + stringLength]
  position += stringLength
  materials = []
  for _ in range(materialCount):
    _, nameOffset, shaderOffset, propertyCount = struct.unpack_from("<4I", modelBytes, position)
    position += 16
    properties = {}
    for _ in range(propertyCount):
      propertyNameOffset, propertyType, propertyValue = struct.unpack_from("<III", modelBytes, position)
      position += 12
      if propertyType == 2:
        properties[readString(stringTable, propertyNameOffset)] = readString(stringTable, propertyValue)
      elif propertyType == 0:
        properties[readString(stringTable, propertyNameOffset)] = struct.unpack("<f", struct.pack("<I", propertyValue))[0]
      else:
        properties[readString(stringTable, propertyNameOffset)] = propertyValue
    materials.append({"name": readString(stringTable, nameOffset), "shader": readString(stringTable, shaderOffset), "properties": properties})
  vertexType = modelVertexTypes[version]
  vertices = numpy.frombuffer(modelBytes, dtype=vertexType, count=vertexCount, offset=position)
  position += vertexCount * vertexType.itemsize
  triangles = numpy.frombuffer(modelBytes, dtype=modelTriangleType, count=triangleCount, offset=position)
  position += triangleCount * modelTriangleType.itemsize
  if triangleCount and int(triangles["indices"].max()) >= vertexCount:
    raise ValueError(f"{sourceName}: triangle index {int(triangles['indices'].max())} exceeds {vertexCount} vertices")
  bones = weights = None
  if boneCount:
    if position + boneCount * boneType.itemsize + vertexCount * weightType.itemsize != len(modelBytes):
      raise ValueError(f"{sourceName}: {boneCount} bones and {vertexCount} weight records do not fill its {len(modelBytes) - position} remaining bytes")
    bones = readBones(modelBytes, position, boneCount, stringTable)
    weights = numpy.frombuffer(modelBytes, dtype=weightType, count=vertexCount, offset=position + boneCount * boneType.itemsize)
  # The client draws no material index at or past the material count, so a collision-only model without materials whose triangles
  # name 0 has none.
  triangleMaterials = triangles["material"].astype(numpy.int64)
  if materialCount == 0 and triangleCount and set(numpy.unique(triangleMaterials).tolist()) <= {noMaterial, 0}:
    triangleMaterials = numpy.full(triangleCount, noMaterial, dtype=numpy.int64)
  if triangleCount and not (noMaterial <= int(triangleMaterials.min()) and int(triangleMaterials.max()) < materialCount):
    raise ValueError(f"{sourceName}: triangle materials span {int(triangleMaterials.min())}..{int(triangleMaterials.max())} with {materialCount} materials")
  return {
    "vertices": vertices["position"].astype(numpy.float64),
    "normals": vertices["normal"].astype(numpy.float64),
    "uvs": vertices["uv"].astype(numpy.float64),
    "triangles": triangles["indices"].astype(numpy.int64),
    "triangleMaterials": triangleMaterials,
    "triangleFlags": triangles["flags"],
    "materials": materials,
    "bones": bones,
    "weights": weights,
  }


def lightPosition(first, second, third):
  """Where the client places a .zon light, in the zone's own axes (the axes its placements and terrain use): (field 2, -field 1, field 3)
  (EQGraphicsDX9.dll 0x10065acb). Freeport West's torch lights land on its torch models only so."""
  return (second, -first, third)


def lightFields(position):
  """The three .zon fields that place a light at a position in the zone's axes (lightPosition's inverse)."""
  x, y, z = position
  return (-y, x, z)


def parseZone(zoneBytes, sourceName):
  """EQGZ .zon: model names (lowercased, as archives key them, and as the file spells them, which the server's .map keeps), object
  placements, regions (name, center, three turns, half extents), and lights (name, position, RGB 0-1, radius). Version 2 appends
  per-vertex baked light to each object."""
  if zoneBytes[:4] != b"EQGZ":
    raise ValueError(f"{sourceName}: not an EQGZ zone")
  version, stringLength, modelCount, objectCount, regionCount, lightCount = struct.unpack_from("<6I", zoneBytes, 4)
  if version not in supportedZoneVersions:
    raise ValueError(f"{sourceName}: EQGZ version {version} is not supported (only {supportedZoneVersions})")
  position = 28
  stringTable = zoneBytes[position:position + stringLength]
  position += stringLength
  modelFileNames = [readString(stringTable, offset) for offset in struct.unpack_from(f"<{modelCount}I", zoneBytes, position)]
  modelNames = [name.lower() for name in modelFileNames]
  position += 4 * modelCount
  placements = []
  for _ in range(objectCount):
    modelIndex, nameOffset, x, y, z, heading, rotationY, rotationX, scale = struct.unpack_from("<iI7f", zoneBytes, position)
    if not 0 <= modelIndex < modelCount:
      raise ValueError(f"{sourceName}: object at {position} references model {modelIndex} of {modelCount}")
    position += 36
    colors = None
    if version == 2:
      colorCount = struct.unpack_from("<I", zoneBytes, position)[0]
      colors = numpy.frombuffer(zoneBytes, dtype="<u4", count=colorCount, offset=position + 4)
      position += 4 + 4 * colorCount
    placements.append({
      "model": modelNames[modelIndex],
      "name": readString(stringTable, nameOffset),
      "position": (x, y, z),
      "rotation": (heading, rotationY, rotationX),
      "scale": scale,
      "colors": colors,
    })
  regions = []
  for _ in range(regionCount):
    nameOffset, *values = struct.unpack_from("<I9f", zoneBytes, position)
    regions.append({"name": readString(stringTable, nameOffset), "center": tuple(values[:3]), "rotation": tuple(values[3:6]), "halfExtents": tuple(values[6:])})
    position += zoneRegionBytes
  lights = []
  for _ in range(lightCount):
    nameOffset, first, second, third, red, green, blue, radius = struct.unpack_from("<I7f", zoneBytes, position)
    lights.append({"name": readString(stringTable, nameOffset), "position": lightPosition(first, second, third), "color": (red, green, blue), "radius": radius})
    position += zoneLightBytes
  if position != len(zoneBytes):
    raise ValueError(f"{sourceName}: zone data ends at {position} of {len(zoneBytes)} bytes")
  return {"version": version, "modelNames": modelNames, "modelFileNames": modelFileNames, "placements": placements, "regions": regions, "lights": lights}


def isZoneLine(regionName):
  return regionName.upper().startswith(zoneLinePrefix)


def zoneLineNumber(regionName):
  """A zone line's number as the client reads it: atoi on the digits after ATP_ (eqgame 0x487530), 0 without any."""
  match = re.match(r"[0-9]+", regionName[len(zoneLinePrefix):])
  return int(match.group(0)) if match else 0


def regionBox(region):
  """A .zon region's box: about its center, its half extents' sizes along its own axes (some files store them negative), turned about
  Z by its first turn field in 512ths of a turn, counter-clockwise from above, as a heading in degrees. Its other two turn fields,
  whose reading is untraced, are given when either is not 0."""
  box = {"center": tuple(region["center"]), "halfExtents": tuple(abs(value) for value in region["halfExtents"]), "headingDegrees": region["rotation"][0] * 360 / regionTurnUnits}
  return box | ({"tiltFields": tuple(region["rotation"][1:])} if any(region["rotation"][1:]) else {})


def placementMatrix(placement):
  # Field 0 varies on nearly every object and turns the Z-up terrain in the plane, so it is the heading (Z axis).
  # The order of the two rarely-used tilt fields is unverified; calibrate against client screenshots.
  heading, rotationY, rotationX = placement["rotation"]
  cosineZ, sineZ = numpy.cos(heading), numpy.sin(heading)
  cosineY, sineY = numpy.cos(rotationY), numpy.sin(rotationY)
  cosineX, sineX = numpy.cos(rotationX), numpy.sin(rotationX)
  aroundZ = numpy.array([[cosineZ, -sineZ, 0], [sineZ, cosineZ, 0], [0, 0, 1]])
  aroundY = numpy.array([[cosineY, 0, sineY], [0, 1, 0], [-sineY, 0, cosineY]])
  aroundX = numpy.array([[1, 0, 0], [0, cosineX, -sineX], [0, sineX, cosineX]])
  return aroundZ @ aroundY @ aroundX * placement["scale"]


def drawnTransform(placement):
  """The matrix and offset the client draws a placed model with: a terrain (.ter) where its own vertices are, whatever its placement
  says (in every zone measured, only so does the ground meet the trees, posts, and crates standing on it), any other model turned,
  scaled, and moved by its placement (as the RoF2 client's actors hold them)."""
  if placement["model"].endswith(".ter"):
    return numpy.identity(3), numpy.zeros(3)
  return placementMatrix(placement), numpy.array(placement["position"], dtype=numpy.float64)


def placeVertices(vertices, placement):
  matrix, offset = drawnTransform(placement)
  return vertices @ matrix.T + offset


def parseSkinnedModel(modelBytes, sourceName):
  """EQGS (.mds): materials, bones, and named pieces (body, heads), each with bind-pose vertices, triangles, and weight records (none in the few files that store none)."""
  if modelBytes[:4] != b"EQGS":
    raise ValueError(f"{sourceName}: magic {modelBytes[:4]!r} is not EQGS")
  version, stringLength, materialCount, boneCount, pieceCount = struct.unpack_from("<5I", modelBytes, 4)
  if version not in supportedModelVersions:
    raise ValueError(f"{sourceName}: skinned model version {version} is not supported (only {supportedModelVersions})")
  position = 24
  stringTable = modelBytes[position:position + stringLength]
  position += stringLength
  materials = []
  for _ in range(materialCount):
    _, nameOffset, shaderOffset, propertyCount = struct.unpack_from("<4I", modelBytes, position)
    position += 16
    properties = {}
    for _ in range(propertyCount):
      propertyNameOffset, propertyType, propertyValue = struct.unpack_from("<III", modelBytes, position)
      position += 12
      if propertyType == 2:
        properties[readString(stringTable, propertyNameOffset)] = readString(stringTable, propertyValue)
    materials.append({"name": readString(stringTable, nameOffset), "shader": readString(stringTable, shaderOffset), "properties": properties})
  bones = readBones(modelBytes, position, boneCount, stringTable)
  position += boneCount * boneType.itemsize
  vertexType = modelVertexTypes[version]
  # Most files store one 36-byte weight record per vertex; a few store none despite the header count. Only the layout
  # that consumes the file exactly is accepted.
  weightBytes = next((candidate for candidate in (skinnedWeightBytes, 0) if skinnedLayoutEnd(modelBytes, position, pieceCount, vertexType, candidate) == len(modelBytes)), None)
  if weightBytes is None:
    raise ValueError(f"{sourceName}: no known skinned model layout matches its {len(modelBytes)} bytes")
  pieces = []
  for _ in range(pieceCount):
    isMain, nameOffset, vertexCount, triangleCount, weightCount = struct.unpack_from("<5I", modelBytes, position)
    position += 20
    vertices = numpy.frombuffer(modelBytes, dtype=vertexType, count=vertexCount, offset=position)
    position += vertexCount * vertexType.itemsize
    triangles = numpy.frombuffer(modelBytes, dtype=modelTriangleType, count=triangleCount, offset=position)
    position += triangleCount * modelTriangleType.itemsize
    weights = numpy.frombuffer(modelBytes, dtype=weightType, count=weightCount, offset=position) if weightBytes else None
    position += weightCount * weightBytes
    if weights is not None and weightCount != vertexCount:
      raise ValueError(f"{sourceName}: piece has {weightCount} weight records for {vertexCount} vertices")
    if triangleCount and int(triangles["indices"].max()) >= vertexCount:
      raise ValueError(f"{sourceName}: piece triangle index {int(triangles['indices'].max())} exceeds {vertexCount} vertices")
    pieces.append({
      "name": readString(stringTable, nameOffset),
      "isMain": bool(isMain),
      "vertices": vertices["position"].astype(numpy.float64),
      "uvs": vertices["uv"].astype(numpy.float64),
      "triangles": triangles["indices"].astype(numpy.int64),
      "triangleMaterials": triangles["material"].astype(numpy.int64),
      "weights": weights,
    })
  return {"materials": materials, "bones": bones, "pieces": pieces}


def skinnedLayoutEnd(modelBytes, position, pieceCount, vertexType, weightBytes):
  for _ in range(pieceCount):
    if position + 20 > len(modelBytes):
      return None
    _, _, vertexCount, triangleCount, weightCount = struct.unpack_from("<5I", modelBytes, position)
    position += 20 + vertexCount * vertexType.itemsize + triangleCount * modelTriangleType.itemsize + weightCount * weightBytes
  return position


def parseAnimation(animationBytes, sourceName):
  """EQGA (.ani) versions 1-2: each bone's keyframes (time in milliseconds, translation, rotation x y z w, scale), by bone name. Of two
  tracks for one bone the first plays: the client registers them in file order and d3dx9 refuses a name already registered."""
  if animationBytes[:4] != b"EQGA":
    raise ValueError(f"{sourceName}: magic {animationBytes[:4]!r} is not EQGA")
  version, stringLength, trackCount = struct.unpack_from("<3I", animationBytes, 4)
  if version not in animationHeaderBytes:
    raise ValueError(f"{sourceName}: animation version {version} is not supported (only {sorted(animationHeaderBytes)})")
  position = animationHeaderBytes[version]
  stringTable = animationBytes[position:position + stringLength]
  position += stringLength
  tracks = {}
  for _ in range(trackCount):
    frameCount, nameOffset = struct.unpack_from("<2I", animationBytes, position)
    position += 8
    frames = numpy.frombuffer(animationBytes, dtype=animationFrameType, count=frameCount, offset=position)
    position += frameCount * animationFrameType.itemsize
    tracks.setdefault(readString(stringTable, nameOffset).upper(), frames)
  if position != len(animationBytes):
    raise ValueError(f"{sourceName}: {trackCount} tracks end at byte {position} of {len(animationBytes)}")
  return tracks


def parseLayers(layerBytes, sourceName):
  """EQGL (.lay): named texture-set layers, each a 32-byte record (name, five texture slots, two more fields) after the string
  table. Texture names keep their case, which tells the client their type."""
  if layerBytes[:4] != b"EQGL":
    raise ValueError(f"{sourceName}: magic {layerBytes[:4]!r} is not EQGL")
  _, stringLength, layerCount = struct.unpack_from("<3I", layerBytes, 4)
  stringTable = layerBytes[16:16 + stringLength]
  position = 16 + stringLength
  if len(layerBytes) - position != layerCount * layerRecordBytes:
    raise ValueError(f"{sourceName}: {len(layerBytes) - position} bytes of layer records do not fit {layerCount} layers of {layerRecordBytes}")
  layers = {}
  for index in range(layerCount):
    nameOffset, *textureOffsets = struct.unpack_from("<6I", layerBytes, position + index * layerRecordBytes)
    layers[readString(stringTable, nameOffset).upper()] = [readString(stringTable, offset) for offset in textureOffsets if offset != 0xFFFFFFFF]
  return layers
