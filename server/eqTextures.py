"""Texture bytes as the client stores them, made readable by Blender."""
import struct

cubeMapFlag = 0x200
mipmapCountFlag = 0x20000
compressedFormatFlag = 0x4


def isCubeMap(textureBytes):
  """Whether texture bytes are a DDS cube map (DDSCAPS2_CUBEMAP), the only environment map the client's water loads."""
  return textureBytes[:4] == b"DDS " and len(textureBytes) >= 128 and bool(struct.unpack_from("<I", textureBytes, 112)[0] & cubeMapFlag)


def levelSize(data, width, height):
  """The bytes of one mipmap level of a DDS face, by its pixel format: blocks of a block-compressed format, else pixels."""
  if struct.unpack_from("<I", data, 80)[0] & compressedFormatFlag:
    blockBytes = 8 if data[84:88] == b"DXT1" else 16
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * blockBytes
  return width * height * struct.unpack_from("<I", data, 88)[0] // 8


def repairDDS(ddsBytes):
  """EQ's DDS headers declare mipmaps with a count of 0; set the count to the levels actually present so readers accept them."""
  data = bytearray(ddsBytes)
  if data[:4] != b"DDS " or struct.unpack_from("<I", data, 28)[0] != 0:
    return bytes(data)
  height, width = struct.unpack_from("<II", data, 12)
  faces = 6 if struct.unpack_from("<I", data, 112)[0] & cubeMapFlag else 1
  remaining = (len(data) - 128) // faces
  levels = 0
  while remaining > 0 and width >= 1 and height >= 1:
    levelBytes = levelSize(data, width, height)
    if levelBytes > remaining:
      break
    remaining -= levelBytes
    levels += 1
    width, height = width // 2, height // 2
  if levels == 0:
    raise ValueError("DDS data is smaller than its top level")
  struct.pack_into("<I", data, 28, levels)
  return bytes(data)


def clientDDS(ddsBytes):
  """A DDS file as the client's own are stored: the mipmap count 0 wherever the header flags none, which repairDDS fills in for Blender.
  The client's water drew one flat color from a normal map and cube map carrying that count; stored with the client's 0 it ripples."""
  data = bytearray(ddsBytes)
  if data[:4] == b"DDS " and not struct.unpack_from("<I", data, 8)[0] & mipmapCountFlag:
    struct.pack_into("<I", data, 28, 0)
  return bytes(data)


def readableTexture(textureName, textureBytes):
  """The file name and bytes Blender reads a client texture from. DDS data, which the client also keeps under .bmp names, becomes a
  repaired .dds file, since Blender picks a reader by the extension."""
  if textureBytes[:4] == b"DDS ":
    return (textureName if textureName.endswith(".dds") else textureName + ".dds"), repairDDS(textureBytes)
  return textureName, textureBytes
