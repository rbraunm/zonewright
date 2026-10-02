import struct

import numpy

wldMagic = 0x54503D02
oldFormatVersion = 0x00015500
newFormatVersion = 0x1000C800
stringHashKey = bytes([0x95, 0x3A, 0xC5, 0x2A, 0x95, 0x7A, 0x95, 0x6A])
passablePolygonFlag = 0x10
invisibleRenderMethod = 0
fragmentAlignment = 4
# Measured: every mesh in the client with mesh operations fits only 6-byte records.
meshOperationBytes = 6


def decodeString(encodedBytes):
  return bytes(value ^ stringHashKey[index % len(stringHashKey)] for index, value in enumerate(encodedBytes))


class Fragment:
  def __init__(self, worldFile, index, fragmentType, body):
    self.worldFile = worldFile
    self.index = index
    self.fragmentType = fragmentType
    self.body = body

  @property
  def name(self):
    return self.worldFile.lookupName(struct.unpack_from("<i", self.body, 0)[0])


class WorldFile:
  """Classic EverQuest WLD: fragments, materials, and meshes. Fragment references are 1-based."""

  def __init__(self, wldBytes, sourceName):
    self.sourceName = sourceName
    magic, version, fragmentCount, _, _, stringHashSize, _ = struct.unpack_from("<IIIIIII", wldBytes, 0)
    if magic != wldMagic:
      raise ValueError(f"{sourceName}: not a WLD file")
    if version not in (oldFormatVersion, newFormatVersion):
      raise ValueError(f"{sourceName}: unknown WLD version {version:#x}")
    self.isOldFormat = version == oldFormatVersion
    position = 28
    self.stringHash = decodeString(wldBytes[position:position + stringHashSize])
    position += stringHashSize
    self.fragments = []
    for index in range(1, fragmentCount + 1):
      fragmentSize, fragmentType = struct.unpack_from("<II", wldBytes, position)
      position += 8
      body = wldBytes[position:position + fragmentSize]
      if len(body) != fragmentSize:
        raise ValueError(f"{sourceName}: fragment {index} runs past the end of the file")
      self.fragments.append(Fragment(self, index, fragmentType, body))
      position += fragmentSize

  def lookupName(self, nameReference):
    if nameReference >= 0:
      return ""
    start = -nameReference
    if start >= len(self.stringHash):
      raise ValueError(f"{self.sourceName}: name reference {nameReference} is outside the {len(self.stringHash)}-byte string hash")
    end = self.stringHash.index(b"\0", start)
    return self.stringHash[start:end].decode("latin1")

  def fragment(self, reference, expectedType):
    if reference < 1 or reference > len(self.fragments):
      raise ValueError(f"{self.sourceName}: fragment reference {reference} out of range")
    referenced = self.fragments[reference - 1]
    if referenced.fragmentType != expectedType:
      raise ValueError(f"{self.sourceName}: fragment {reference} is type {referenced.fragmentType:#x}, expected {expectedType:#x}")
    return referenced

  def referenced(self, reference):
    """A fragment reference: positive is a 1-based index, negative names the fragment through the string hash."""
    if reference > 0:
      if reference > len(self.fragments):
        raise ValueError(f"{self.sourceName}: fragment reference {reference} out of range")
      return self.fragments[reference - 1]
    name = self.lookupName(reference)
    named = [fragment for fragment in self.fragments if fragment.name == name]
    if len(named) != 1:
      raise ValueError(f"{self.sourceName}: name reference '{name}' matches {len(named)} fragments")
    return named[0]

  def fragmentsOfType(self, fragmentType):
    return [fragment for fragment in self.fragments if fragment.fragmentType == fragmentType]

  def bitmapNames(self, bitmapFragment):
    body = bitmapFragment.body
    nameCountLessOne = struct.unpack_from("<I", body, 4)[0]
    position = 8
    names = []
    for _ in range(nameCountLessOne + 1):
      nameLength = struct.unpack_from("<H", body, position)[0]
      position += 2
      names.append(decodeString(body[position:position + nameLength]).rstrip(b"\0").decode("latin1").lower())
      position += nameLength
    if not 0 <= len(body) - position < fragmentAlignment:
      raise ValueError(f"{self.sourceName}: bitmap fragment {bitmapFragment.index} used {position} of {len(body)} bytes")
    return names

  def material(self, materialReference):
    body = self.fragment(materialReference, 0x30).body
    flags, renderMethod, _, _, _, spriteReference = struct.unpack_from("<IIIffi", body, 4)
    textureNames = []
    if spriteReference > 0:
      spriteDefinitionReference = struct.unpack_from("<i", self.fragment(spriteReference, 0x05).body, 4)[0]
      spriteDefinition = self.fragment(spriteDefinitionReference, 0x04).body
      spriteFlags, frameCount = struct.unpack_from("<II", spriteDefinition, 4)
      position = 12 + (4 if spriteFlags & 0x4 else 0) + (4 if spriteFlags & 0x8 else 0)
      for frameIndex in range(frameCount):
        bitmapReference = struct.unpack_from("<i", spriteDefinition, position + 4 * frameIndex)[0]
        textureNames += self.bitmapNames(self.fragment(bitmapReference, 0x03))
    return {"name": self.fragments[materialReference - 1].name, "renderMethod": renderMethod, "textureNames": textureNames}

  def materialList(self, materialListReference):
    body = self.fragment(materialListReference, 0x31).body
    _, materialCount = struct.unpack_from("<II", body, 4)
    materialReferences = struct.unpack_from(f"<{materialCount}i", body, 12)
    return [self.material(reference) for reference in materialReferences]

  def mesh(self, meshFragment):
    body = meshFragment.body
    (_, materialListReference, _, _, _, centerX, centerY, centerZ) = struct.unpack_from("<IiiIIfff", body, 4)
    counts = struct.unpack_from("<10H", body, 76)
    vertexCount, uvCount, normalCount, colorCount, polygonCount, vertexPieceCount, polygonTextureCount, vertexTextureCount, meshOperationCount, scaleShift = counts
    position = 96
    rawVertices = numpy.frombuffer(body, dtype="<i2", count=vertexCount * 3, offset=position).reshape(vertexCount, 3)
    position += vertexCount * 6
    vertices = rawVertices.astype(numpy.float64) / (1 << scaleShift) + (centerX, centerY, centerZ)
    if self.isOldFormat:
      uvs = numpy.frombuffer(body, dtype="<i2", count=uvCount * 2, offset=position).reshape(uvCount, 2) / 256.0
      position += uvCount * 4
    else:
      uvs = numpy.frombuffer(body, dtype="<f4", count=uvCount * 2, offset=position).reshape(uvCount, 2).astype(numpy.float64)
      position += uvCount * 8
    # WLD counts v down from a texture's top row; Blender counts up from its bottom. Measured: a Luclin face draws as the client
    # shows it only flipped.
    uvs = uvs * (1, -1) + (0, 1)
    normals = numpy.frombuffer(body, dtype=numpy.int8, count=normalCount * 3, offset=position).reshape(normalCount, 3) / 127.0
    position += normalCount * 3
    # D3DCOLOR order, blue first: Plane of Knowledge's torch-lit walls bake orange only read this way.
    colors = numpy.frombuffer(body, dtype=numpy.uint8, count=colorCount * 4, offset=position).reshape(colorCount, 4)[:, [2, 1, 0, 3]]
    position += colorCount * 4
    polygons = numpy.frombuffer(body, dtype=numpy.dtype([("flags", "<u2"), ("indices", "<u2", 3)]), count=polygonCount, offset=position)
    position += polygonCount * 8 + vertexPieceCount * 4
    polygonTextures = numpy.frombuffer(body, dtype="<u2", count=polygonTextureCount * 2, offset=position).reshape(polygonTextureCount, 2)
    position += polygonTextureCount * 4 + vertexTextureCount * 4 + meshOperationCount * meshOperationBytes
    if not 0 <= len(body) - position < fragmentAlignment:
      raise ValueError(f"{self.sourceName}: mesh fragment {meshFragment.index} '{meshFragment.name}' used {position} of {len(body)} bytes")
    if int(polygonTextures[:, 0].sum()) != polygonCount:
      raise ValueError(f"{self.sourceName}: mesh '{meshFragment.name}' polygon texture runs cover {int(polygonTextures[:, 0].sum())} of {polygonCount} polygons")
    polygonMaterials = numpy.repeat(polygonTextures[:, 1], polygonTextures[:, 0])
    return {
      "name": meshFragment.name,
      "vertices": vertices,
      "uvs": uvs if uvCount == vertexCount else None,
      "normals": normals if normalCount == vertexCount else None,
      # Per-vertex baked light and the share of scene light received (alpha), as RGBA bytes.
      "colors": colors if colorCount == vertexCount else None,
      # WLD winds clockwise against its stored normals; reorder to counter-clockwise like EQG and Blender.
      "triangles": polygons["indices"][:, [0, 2, 1]].astype(numpy.int64),
      "isPassable": (polygons["flags"] & passablePolygonFlag) != 0,
      "triangleMaterials": polygonMaterials.astype(numpy.int64),
      "materials": self.materialList(materialListReference) if materialListReference > 0 else [],
    }

  def meshes(self):
    return [self.mesh(fragment) for fragment in self.fragmentsOfType(0x36)]

  def lightDefinition(self, definitionFragment):
    """A light source definition (0x1B): its frames' levels and RGB colors (0-1), which the client steps through to flicker."""
    body = definitionFragment.body
    flags, frameCount = struct.unpack_from("<II", body, 4)
    position = 12 + (4 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0)
    levels = None
    if flags & 0x4:
      levels = list(struct.unpack_from(f"<{frameCount}f", body, position))
      position += 4 * frameCount
    if not flags & 0x10:
      raise ValueError(f"{self.sourceName}: light definition {definitionFragment.index} '{definitionFragment.name}' stores no colors")
    colors = [tuple(struct.unpack_from("<3f", body, position + 12 * frame)) for frame in range(frameCount)]
    position += 12 * frameCount
    if position != len(body):
      raise ValueError(f"{self.sourceName}: light definition {definitionFragment.index} used {position} of {len(body)} bytes")
    return {"name": definitionFragment.name, "levels": levels, "colors": colors}

  def pointLights(self):
    """lights.wld's point lights (0x28 through 0x1C to 0x1B): position, radius, and the definition's frames."""
    lights = []
    for fragment in self.fragmentsOfType(0x28):
      sourceReference, _, x, y, z, radius = struct.unpack_from("<iI4f", fragment.body, 4)
      definitionReference = struct.unpack_from("<i", self.fragment(sourceReference, 0x1C).body, 4)[0]
      definition = self.lightDefinition(self.fragment(definitionReference, 0x1B))
      lights.append({"name": definition["name"], "position": (x, y, z), "radius": radius, "color": definition["colors"][0], "frames": len(definition["colors"])})
    return lights
