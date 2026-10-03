"""EQG zone files as the client reads them: PFS archives, EQGM models and EQGT terrain (version 2), EQGZ zones (version 1, the
version the client loads from inside an archive) with their region boxes, EQGP baked light, and uncompressed DDS textures."""
import struct
import zlib

import numpy

pfsVersion = 0x20000
directoryCRC = 0x61580AC9
blockBytes = 8192
modelVersion = 2
zoneVersion = 1
crcPolynomial = 0x04C11DB7
# Shaders by material: each pairing appears on client zone terrain and objects with exactly these textures (diffuse, normal).
shaderOpaqueBump = "Opaque_MaxCB1.fx"
shaderOpaque = "Opaque_MaxC1.fx"
shaderCutout = "Chroma_MPLBasicAT.fx"
# The client's liquid shaders, each with the properties its zones' materials carry.
liquidShaders = {"water": "Opaque_MaxWater.fx", "waterfall": "Opaque_MaxWaterFall.fx", "lava": "Opaque_MaxLava.fx"}
# Material property value types: a float, a string (a texture's name), and a color (0xAARRGGBB).
propertyFloat, propertyString, propertyColor = 0, 2, 3


def crcTable():
  table = []
  for index in range(256):
    value = index << 24
    for _ in range(8):
      value = ((value << 1) ^ crcPolynomial) if value & 0x80000000 else value << 1
    table.append(value & 0xFFFFFFFF)
  return table


filenameCRCTable = crcTable()


def filenameCRC(name):
  """The CRC a PFS directory keys a file by: over the lowercase name and its terminating zero, unreflected."""
  value = 0
  for byte in name.encode("latin1") + b"\0":
    value = ((value << 8) ^ filenameCRCTable[((value >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
  return value


def deflatedBlocks(data):
  """A file as the archive stores it: blocks of at most blockBytes, each its compressed size, its size, then zlib data."""
  chunks = []
  for start in range(0, len(data), blockBytes):
    block = data[start:start + blockBytes]
    compressed = zlib.compress(block)
    chunks.append(struct.pack("<II", len(compressed), len(block)) + compressed)
  return b"".join(chunks)


def archiveBytes(files):
  """A PFS archive of {name: bytes}: names lowercase; directory entries sorted by unsigned CRC and the filename list in data order,
  as the client's own archives keep them; no footer."""
  names = list(files)
  if any(name != name.lower() for name in names):
    raise ValueError(f"Archive names must be lowercase: {[name for name in names if name != name.lower()]}")
  # The client aborts loading an empty file (EQGraphicsDX9.dll 0x10065c00).
  if any(not files[name] for name in names):
    raise ValueError(f"Archive files must not be empty: {[name for name in names if not files[name]]}")
  crcs = {}
  for name in names:
    crc = filenameCRC(name)
    if crc in crcs or crc == directoryCRC:
      raise ValueError(f"'{name}' has the same CRC as '{crcs.get(crc, 'the filename directory')}'")
    crcs[crc] = name
  body = bytearray(struct.pack("<I4sI", 0, b"PFS ", pfsVersion))
  entries = []
  for name in names:
    entries.append((filenameCRC(name), len(body), len(files[name])))
    body += deflatedBlocks(files[name])
  filenameList = struct.pack("<I", len(names)) + b"".join(struct.pack("<I", len(name) + 1) + name.encode("latin1") + b"\0" for name in names)
  entries.append((directoryCRC, len(body), len(filenameList)))
  body += deflatedBlocks(filenameList)
  directoryOffset = len(body)
  body += struct.pack("<I", len(entries))
  for crc, offset, size in sorted(entries):
    body += struct.pack("<III", crc, offset, size)
  struct.pack_into("<I", body, 0, directoryOffset)
  return bytes(body)


class StringTable:
  """Zero-terminated strings referenced by offset; each distinct string stored once."""

  def __init__(self):
    self.offsets, self.data = {}, bytearray()

  def offset(self, text):
    if text not in self.offsets:
      self.offsets[text] = len(self.data)
      self.data += text.encode("latin1") + b"\0"
    return self.offsets[text]


def materialShader(material):
  if material.get("liquid") is not None:
    return liquidShaders[material["liquid"]["liquid"]]
  if material["cutout"]:
    if material["normalTexture"] is not None:
      raise ValueError(f"Material '{material['name']}' is a cutout with a normal map; the client's cutout shader with normals also needs coverage and fallback textures, so a cutout exports diffuse only")
    return shaderCutout
  return shaderOpaqueBump if material["normalTexture"] is not None else shaderOpaque


def colorValue(color):
  return 0xFF000000 | (round(color[0] * 255) << 16) | (round(color[1] * 255) << 8) | round(color[2] * 255)


def materialProperties(material):
  """A material's shader properties as (name, type, value): its textures, and for a liquid its shader values in the order the client's
  own liquid materials list them."""
  liquid = material.get("liquid")
  properties = [("e_TextureDiffuse0", propertyString, material["diffuseTexture"])]
  if liquid is not None and liquid["liquid"] == "lava":
    properties.append(("e_TextureDiffuse1", propertyString, liquid["secondDiffuseTexture"]))
  if material["normalTexture"] is not None:
    properties.append(("e_TextureNormal0", propertyString, material["normalTexture"]))
  if liquid is None:
    return properties
  missing = [key for key, needed in (("environmentTexture", liquid["liquid"] == "water"), ("secondDiffuseTexture", liquid["liquid"] == "lava")) if needed and not liquid.get(key)]
  if missing or (liquid["liquid"] in ("water", "lava") and material["normalTexture"] is None):
    raise ValueError(f"Liquid material '{material['name']}' ({liquid['liquid']}) lacks {missing or ['normalTexture']}")
  values = liquid["values"]
  if liquid["liquid"] == "water":
    properties += [
      ("e_TextureEnvironment0", propertyString, liquid["environmentTexture"]), ("e_fFresnelBias", propertyFloat, values["fresnelBias"]),
      ("e_fFresnelPower", propertyFloat, values["fresnelPower"]), ("e_fWaterColor1", propertyColor, colorValue(values["waterColor1"])),
      ("e_fWaterColor2", propertyColor, colorValue(values["waterColor2"])), ("e_fReflectionAmount", propertyFloat, values["reflectionAmount"]),
      ("e_fReflectionColor", propertyColor, colorValue(values["reflectionColor"])),
    ]
  return properties + [(name, propertyFloat, value) for name, value in zip(("e_fSlide1X", "e_fSlide1Y", "e_fSlide2X", "e_fSlide2Y"), values["slides"])]


def modelBytes(kind, materials, positions, normals, uvs, triangles, triangleMaterials):
  """An EQGM (.mod) or EQGT (.ter) model, version 2: materials as shader and texture properties, then vertices (position, normal, v-up
  texture coordinate as the file keeps it) and triangles with no flags."""
  magic = {"mod": b"EQGM", "ter": b"EQGT"}[kind]
  positions, normals, uvs = (numpy.asarray(array, dtype=numpy.float32) for array in (positions, normals, uvs))
  triangles = numpy.asarray(triangles, dtype=numpy.uint32)
  triangleMaterials = numpy.asarray(triangleMaterials, dtype=numpy.int32)
  if not (positions.shape == normals.shape and positions.ndim == 2 and positions.shape[1] == 3 and uvs.shape == (len(positions), 2)):
    raise ValueError(f"Model arrays disagree: positions {positions.shape}, normals {normals.shape}, uvs {uvs.shape}")
  if len(triangles) and int(triangles.max()) >= len(positions):
    raise ValueError(f"Triangle index {int(triangles.max())} exceeds {len(positions)} vertices")
  if len(triangleMaterials) != len(triangles) or (len(triangles) and not 0 <= int(triangleMaterials.min()) <= int(triangleMaterials.max()) < len(materials)):
    raise ValueError(f"{len(triangles)} triangles need one material each among {len(materials)}")
  strings = StringTable()
  materialRecords = bytearray()
  for index, material in enumerate(materials):
    properties = materialProperties(material)
    materialRecords += struct.pack("<4I", index, strings.offset(material["name"]), strings.offset(materialShader(material)), len(properties))
    for propertyName, propertyType, value in properties:
      if propertyType == propertyString:
        materialRecords += struct.pack("<3I", strings.offset(propertyName), propertyType, strings.offset(value))
      elif propertyType == propertyFloat:
        materialRecords += struct.pack("<2If", strings.offset(propertyName), propertyType, value)
      else:
        materialRecords += struct.pack("<3I", strings.offset(propertyName), propertyType, value)
  vertexType = numpy.dtype([("position", "<f4", 3), ("normal", "<f4", 3), ("uv", "<f4", 2)])
  vertices = numpy.empty(len(positions), dtype=vertexType)
  vertices["position"], vertices["normal"], vertices["uv"] = positions, normals, uvs
  triangleType = numpy.dtype([("indices", "<u4", 3), ("material", "<i4"), ("flags", "<u4")])
  triangleRecords = numpy.zeros(len(triangles), dtype=triangleType)
  triangleRecords["indices"], triangleRecords["material"] = triangles, triangleMaterials
  counts = (modelVersion, len(strings.data), len(materials), len(positions), len(triangles))
  header = magic + struct.pack("<6I", *counts, 0) if kind == "mod" else magic + struct.pack("<5I", *counts)
  return header + bytes(strings.data) + bytes(materialRecords) + vertices.tobytes() + triangleRecords.tobytes()


def zoneBytes(modelNames, placements, regions, lights):
  """An EQGZ zone, version 1: model file names, then placements (model, name, position, heading about Z, then the turns about Y and X,
  in radians, and one scale), then regions (a name whose prefix says what the region is, such as AWT_ for water, and a box: its center,
  three turns written 0 so it lines up with the zone's axes, and its half extents), then lights (name, position, RGB 0-1, radius)."""
  strings = StringTable()
  modelOffsets = [strings.offset(name) for name in modelNames]
  modelIndex = {name: index for index, name in enumerate(modelNames)}
  records = bytearray()
  for placement in placements:
    records += struct.pack("<iI7f", modelIndex[placement["model"]], strings.offset(placement["name"]), *placement["position"],
      *placement["rotation"], placement["scale"])
  regionRecords = bytearray()
  for region in regions:
    if min(region["halfExtents"]) <= 0:
      raise ValueError(f"Region '{region['name']}' needs positive half extents, got {list(region['halfExtents'])}")
    regionRecords += struct.pack("<I9f", strings.offset(region["name"]), *region["center"], 0.0, 0.0, 0.0, *region["halfExtents"])
  lightRecords = bytearray()
  for light in lights:
    if light["radius"] <= 0 or not all(0 <= component <= 1 for component in light["color"]):
      raise ValueError(f"Light '{light['name']}' needs a positive radius and RGB in 0-1, got {light['radius']} and {list(light['color'])}")
    lightRecords += struct.pack("<I7f", strings.offset(light["name"]), *light["position"], *light["color"], light["radius"])
  header = b"EQGZ" + struct.pack("<6I", zoneVersion, len(strings.data), len(modelNames), len(placements), len(regions), len(lights))
  return header + bytes(strings.data) + struct.pack(f"<{len(modelOffsets)}I", *modelOffsets) + bytes(records) + bytes(regionRecords) + bytes(lightRecords)


def litBytes(colors):
  """A placement's baked light (EQGP): one D3DCOLOR (0xAARRGGBB) per vertex of its model, from RGBA bytes."""
  colors = numpy.asarray(colors, dtype=numpy.uint32)
  packed = (colors[:, 3] << 24) | (colors[:, 0] << 16) | (colors[:, 1] << 8) | colors[:, 2]
  return b"EQGP" + struct.pack("<I", len(packed)) + packed.astype("<u4").tobytes()


def ddsBytes(rgba):
  """An uncompressed 32-bit DDS (A8R8G8B8) with every mipmap level, each halved by averaging, from RGBA rows top first."""
  image = numpy.asarray(rgba, dtype=numpy.uint8)
  height, width = image.shape[:2]
  if image.ndim != 3 or image.shape[2] != 4 or height & (height - 1) or width & (width - 1):
    raise ValueError(f"A DDS texture needs RGBA with power-of-two sides, got {image.shape}")
  levels = [image]
  while levels[-1].shape[0] > 1 or levels[-1].shape[1] > 1:
    level = levels[-1].astype(numpy.float64)
    level = level.reshape(max(level.shape[0] // 2, 1), min(level.shape[0], 2), max(level.shape[1] // 2, 1), min(level.shape[1], 2), 4).mean(axis=(1, 3))
    levels.append(numpy.round(level).astype(numpy.uint8))
  flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000 | 0x20000
  pixelFormat = struct.pack("<8I", 32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
  header = b"DDS " + struct.pack("<7I", 124, flags, height, width, width * 4, 0, len(levels)) + bytes(44) + pixelFormat
  header += struct.pack("<4I", 0x1000 | 0x400000 | 0x8, 0, 0, 0) + bytes(4)
  return header + b"".join(level[..., [2, 1, 0, 3]].tobytes() for level in levels)
