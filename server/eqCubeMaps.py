"""DDS cube maps as the client loads them for its water's environment, and as the preview looks them up: Blender's DDS reader cannot
read uncompressed cube maps and stacks the faces of compressed ones, so the server decodes them."""
import io
import struct

import numpy
from PIL import Image

import eqTextures

blockBytes = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16}
headerBytes = 128
faceCount = 6
# DDSCAPS2's six face flags, all of which a cube map the client draws carries.
allFaces = 0xFC00
compressedFlag = 0x4
rgbFlag = 0x40


def levelSizes(side, pixelFlags, fourCC, bitCount, sourceName):
  """The bytes of each mip level of one face, top first."""
  if pixelFlags & compressedFlag:
    if fourCC not in blockBytes:
      raise ValueError(f"{sourceName}: compressed as {fourCC!r}; the client's cube maps are DXT1, DXT3, DXT5, or 32-bit uncompressed")
    return [((max(1, side >> level) + 3) // 4) ** 2 * blockBytes[fourCC] for level in range(side.bit_length())]
  if not pixelFlags & rgbFlag or bitCount != 32:
    raise ValueError(f"{sourceName}: {bitCount}-bit pixels with format flags {pixelFlags:#x}; the client's cube maps are DXT1, DXT3, DXT5, or 32-bit uncompressed")
  return [max(1, side >> level) ** 2 * 4 for level in range(side.bit_length())]


def cubeFaces(ddsBytes, sourceName):
  """A DDS cube map's six faces at full size, RGBA rows top first, in the file's order (+X, -X, +Y, -Y, +Z, -Z), as the client loads
  them (EQGraphicsDX9.dll 0x10061610: D3DXCreateCubeTextureFromFileInMemoryEx, one level). Each face holds its whole mip chain; the
  client's files hold one level or all of them, whatever their header's count says."""
  if not eqTextures.isCubeMap(ddsBytes):
    raise ValueError(f"{sourceName} is not a DDS cube map")
  height, width = struct.unpack_from("<II", ddsBytes, 12)
  pixelFlags, fourCC, bitCount = struct.unpack_from("<I4sI", ddsBytes, 80)
  if struct.unpack_from("<I", ddsBytes, 112)[0] & allFaces != allFaces:
    raise ValueError(f"{sourceName}: a cube map without all six faces")
  if width != height or width & (width - 1):
    raise ValueError(f"{sourceName}: cube faces of {width}x{height}; faces are square with power-of-two sides")
  sizes = levelSizes(width, pixelFlags, fourCC, bitCount, sourceName)
  faceBytes, remainder = divmod(len(ddsBytes) - headerBytes, faceCount)
  if remainder or faceBytes not in numpy.cumsum(sizes):
    raise ValueError(f"{sourceName}: {len(ddsBytes) - headerBytes} bytes of faces, not six whole mip chains of {width}x{width} faces")
  header = bytearray(ddsBytes[:headerBytes])
  struct.pack_into("<I", header, 28, 1)
  struct.pack_into("<I", header, 112, 0)
  faces = []
  for face in range(faceCount):
    start = headerBytes + face * faceBytes
    with Image.open(io.BytesIO(bytes(header) + ddsBytes[start:start + sizes[0]])) as image:
      faces.append(numpy.asarray(image.convert("RGBA")))
  return numpy.stack(faces)


def faceCoordinates(x, y, z):
  """Per direction (D3D's cube space): its face and where on it, s across and t down from 0 to 1, by D3D's face table."""
  ax, ay, az = numpy.abs(x), numpy.abs(y), numpy.abs(z)
  onX = (ax >= ay) & (ax >= az)
  onY = ~onX & (ay >= az)
  face = numpy.where(onX, numpy.where(x > 0, 0, 1), numpy.where(onY, numpy.where(y > 0, 2, 3), numpy.where(z > 0, 4, 5)))
  sc = numpy.choose(face, [-z, z, x, x, x, -x])
  tc = numpy.choose(face, [-y, -y, z, -z, -y, -y])
  major = numpy.choose(face, [ax, ax, ay, ay, az, az])
  return face, (sc / major + 1) / 2, (tc / major + 1) / 2


def environmentLookup(faces):
  """The cube map as an equirectangular image, rows top first and twice as wide as tall, for Blender's Environment Texture node: looked
  up along a world direction d (z up), it gives what the client's water gets from the cube for (d.x, d.z, d.y) (RegionWater.fxo's pixel
  shader turns the world's z up into the cube's y), each face filtered bilinearly within itself."""
  side = faces.shape[1]
  height, width = 2 * side, 4 * side
  # Blender's equirectangular mapping (its Environment Texture node): u = 0.5 - atan2(d.y, d.x) / 2pi, v = 0.5 + latitude / pi, v up.
  longitude = (0.5 - (numpy.arange(width) + 0.5) / width) * 2 * numpy.pi
  latitude = (0.5 - (numpy.arange(height) + 0.5) / height) * numpy.pi
  longitude, latitude = numpy.meshgrid(longitude, latitude)
  worldX, worldY, worldZ = numpy.cos(latitude) * numpy.cos(longitude), numpy.cos(latitude) * numpy.sin(longitude), numpy.sin(latitude)
  face, s, t = faceCoordinates(worldX, worldZ, worldY)
  column = numpy.clip(s * side - 0.5, 0, side - 1)
  row = numpy.clip(t * side - 0.5, 0, side - 1)
  left, top = numpy.minimum(column.astype(int), side - 2), numpy.minimum(row.astype(int), side - 2)
  across, down = (column - left)[..., None], (row - top)[..., None]
  texels = faces.astype(numpy.float64)
  upper = texels[face, top, left] * (1 - across) + texels[face, top, left + 1] * across
  lower = texels[face, top + 1, left] * (1 - across) + texels[face, top + 1, left + 1] * across
  return numpy.round(upper * (1 - down) + lower * down).astype(numpy.uint8)


def environmentLookupPNG(ddsBytes, sourceName):
  """environmentLookup of a DDS cube map, as PNG bytes."""
  encoded = io.BytesIO()
  Image.fromarray(environmentLookup(cubeFaces(ddsBytes, sourceName)), "RGBA").save(encoded, "PNG")
  return encoded.getvalue()
