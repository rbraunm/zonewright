import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqTextures
import eqgExport


def ddsFile(width, height, data, mipmapCount=0, flags=0x81007, fourCC=None, bitCount=0, cube=False):
  pixelFormat = struct.pack("<II4sI", 32, 0x4 if fourCC else 0x40, fourCC or bytes(4), bitCount) + bytes(16)
  caps2 = 0xFE00 if cube else 0
  header = struct.pack("<4s7I", b"DDS ", 124, flags, height, width, 0, 0, mipmapCount) + bytes(44) + pixelFormat
  return header + struct.pack("<4I", 0x1000, caps2, 0, 0) + bytes(4) + data


def mipmapCount(data):
  return struct.unpack_from("<I", data, 28)[0]


def testRepairCountsOneLevelOfAnUncompressedMap():
  assert mipmapCount(eqTextures.repairDDS(ddsFile(8, 8, bytes(8 * 8 * 2), bitCount=16))) == 1


def testRepairCountsEveryLevelOfAnUncompressedMap():
  levels = bytes(8 * 8 * 2 + 4 * 4 * 2 + 2 * 2 * 2 + 1 * 1 * 2)
  assert mipmapCount(eqTextures.repairDDS(ddsFile(8, 8, levels, bitCount=16))) == 4


def testRepairCountsOneLevelOfACubeMap():
  assert mipmapCount(eqTextures.repairDDS(ddsFile(4, 4, bytes(6 * 4 * 4 * 4), bitCount=32, cube=True))) == 1


def testRepairCountsTheBlocksOfACompressedMap():
  assert mipmapCount(eqTextures.repairDDS(ddsFile(8, 8, bytes(4 * 8 + 8), fourCC=b"DXT1"))) == 2
  assert mipmapCount(eqTextures.repairDDS(ddsFile(8, 8, bytes(4 * 16), fourCC=b"DXT5"))) == 1


def testRepairLeavesACountItIsGiven():
  given = ddsFile(8, 8, bytes(8 * 8 * 2), mipmapCount=3, bitCount=16)
  assert eqTextures.repairDDS(given) == given


def testClientFormRestoresTheCountOfARepairedFile():
  original = ddsFile(8, 8, bytes(8 * 8 * 2), bitCount=16)
  assert eqTextures.clientDDS(eqTextures.repairDDS(original)) == original


def testClientFormKeepsTheCountWhereTheHeaderFlagsMipmaps():
  mipped = ddsFile(8, 8, bytes(8 * 8 * 2 + 4 * 4 * 2 + 2 * 2 * 2 + 2), mipmapCount=4, flags=0xA1007, bitCount=16)
  assert eqTextures.clientDDS(mipped) == mipped


def testExportStoresADDSTextureInTheClientForm(tmp_path):
  original = ddsFile(8, 8, bytes(8 * 8 * 2), bitCount=16)
  path = tmp_path / "Water_N.dds"
  path.write_bytes(eqTextures.repairDDS(original))
  assert mipmapCount(path.read_bytes()) == 1
  assert eqgExport.textureEntry(str(path)) == ("water_n.dds", original)
