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
zoneRegionBytes = 40
skinnedBoneBytes = 56
skinnedWeightBytes = 36
zoneLightBytes = 32
layerRecordBytes = 32


def readString(stringTable, offset):
  return stringTable[offset:stringTable.index(b"\0", offset)].decode("latin1")


def parseModel(modelBytes, sourceName):
  """EQGM (.mod) or EQGT (.ter) versions 1-3: materials, vertices, triangles. Bones and later sections are skipped."""
  magic = modelBytes[:4]
  if magic == b"EQGT":
    version, stringLength, materialCount, vertexCount, triangleCount = struct.unpack_from("<5I", modelBytes, 4)
    position = 24
  elif magic == b"EQGM":
    version, stringLength, materialCount, vertexCount, triangleCount, _ = struct.unpack_from("<6I", modelBytes, 4)
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
  if triangleCount and int(triangles["indices"].max()) >= vertexCount:
    raise ValueError(f"{sourceName}: triangle index {int(triangles['indices'].max())} exceeds {vertexCount} vertices")
  # Material -1 marks triangles with no material.
  if triangleCount and not (-1 <= int(triangles["material"].min()) and int(triangles["material"].max()) < materialCount):
    raise ValueError(f"{sourceName}: triangle materials span {int(triangles['material'].min())}..{int(triangles['material'].max())} with {materialCount} materials")
  return {
    "vertices": vertices["position"].astype(numpy.float64),
    "uvs": vertices["uv"].astype(numpy.float64),
    "triangles": triangles["indices"].astype(numpy.int64),
    "triangleMaterials": triangles["material"].astype(numpy.int64),
    "triangleFlags": triangles["flags"],
    "materials": materials,
  }


def parseZone(zoneBytes, sourceName):
  """EQGZ .zon: model names and object placements. Version 2 appends per-vertex baked light to each object."""
  if zoneBytes[:4] != b"EQGZ":
    raise ValueError(f"{sourceName}: not an EQGZ zone")
  version, stringLength, modelCount, objectCount, regionCount, lightCount = struct.unpack_from("<6I", zoneBytes, 4)
  if version not in supportedZoneVersions:
    raise ValueError(f"{sourceName}: EQGZ version {version} is not supported (only {supportedZoneVersions})")
  position = 28
  stringTable = zoneBytes[position:position + stringLength]
  position += stringLength
  modelNames = [readString(stringTable, offset).lower() for offset in struct.unpack_from(f"<{modelCount}I", zoneBytes, position)]
  position += 4 * modelCount
  placements = []
  for _ in range(objectCount):
    modelIndex, nameOffset, x, y, z, heading, rotationY, rotationX, scale = struct.unpack_from("<iI7f", zoneBytes, position)
    if not 0 <= modelIndex < modelCount:
      raise ValueError(f"{sourceName}: object at {position} references model {modelIndex} of {modelCount}")
    position += 36
    if version == 2:
      colorCount = struct.unpack_from("<I", zoneBytes, position)[0]
      position += 4 + 4 * colorCount
    placements.append({
      "model": modelNames[modelIndex],
      "name": readString(stringTable, nameOffset),
      "position": (x, y, z),
      "rotation": (heading, rotationY, rotationX),
      "scale": scale,
    })
  regionNames = []
  for _ in range(regionCount):
    regionNames.append(readString(stringTable, struct.unpack_from("<I", zoneBytes, position)[0]))
    position += zoneRegionBytes
  position += lightCount * zoneLightBytes
  if position != len(zoneBytes):
    raise ValueError(f"{sourceName}: zone data ends at {position} of {len(zoneBytes)} bytes")
  return {"modelNames": modelNames, "placements": placements, "regionNames": regionNames, "lightCount": lightCount}


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


def placeVertices(vertices, placement):
  return vertices @ placementMatrix(placement).T + placement["position"]


def parseSkinnedModel(modelBytes, sourceName):
  """EQGS (.mds): materials and named pieces (body, heads), each with bind-pose vertices and triangles. Bones and weights are skipped."""
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
  position += boneCount * skinnedBoneBytes
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
    position += triangleCount * modelTriangleType.itemsize + weightCount * weightBytes
    if triangleCount and int(triangles["indices"].max()) >= vertexCount:
      raise ValueError(f"{sourceName}: piece triangle index {int(triangles['indices'].max())} exceeds {vertexCount} vertices")
    pieces.append({
      "name": readString(stringTable, nameOffset),
      "isMain": bool(isMain),
      "vertices": vertices["position"].astype(numpy.float64),
      "uvs": vertices["uv"].astype(numpy.float64),
      "triangles": triangles["indices"].astype(numpy.int64),
      "triangleMaterials": triangles["material"].astype(numpy.int64),
    })
  return {"materials": materials, "pieces": pieces}


def skinnedLayoutEnd(modelBytes, position, pieceCount, vertexType, weightBytes):
  for _ in range(pieceCount):
    if position + 20 > len(modelBytes):
      return None
    _, _, vertexCount, triangleCount, weightCount = struct.unpack_from("<5I", modelBytes, position)
    position += 20 + vertexCount * vertexType.itemsize + triangleCount * modelTriangleType.itemsize + weightCount * weightBytes
  return position


def parseLayers(layerBytes, sourceName):
  """EQGL (.lay): named texture-set layers, each a 32-byte record (name, five texture slots, two more fields) after the string table."""
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
    layers[readString(stringTable, nameOffset).upper()] = [readString(stringTable, offset).lower() for offset in textureOffsets if offset != 0xFFFFFFFF]
  return layers
