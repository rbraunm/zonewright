"""Texture bytes as the client stores them, made readable by Blender."""
import struct


def repairDDS(ddsBytes):
  """EQ's DDS headers declare mipmaps with a count of 0; set the count to the levels actually present so readers accept them."""
  data = bytearray(ddsBytes)
  if data[:4] != b"DDS " or struct.unpack_from("<I", data, 28)[0] != 0:
    return bytes(data)
  height, width = struct.unpack_from("<II", data, 12)
  blockBytes = 8 if data[84:88] == b"DXT1" else 16
  remaining = len(data) - 128
  levels = 0
  while remaining > 0 and width >= 1 and height >= 1:
    levelBytes = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * blockBytes
    if levelBytes > remaining:
      break
    remaining -= levelBytes
    levels += 1
    width, height = width // 2, height // 2
  if levels == 0:
    raise ValueError("DDS data is smaller than its top level")
  struct.pack_into("<I", data, 28, levels)
  return bytes(data)


def readableTexture(textureName, textureBytes):
  return repairDDS(textureBytes) if textureName.endswith(".dds") else textureBytes
