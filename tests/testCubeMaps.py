import json
import math
import sys
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqCubeMaps
import eqgWriter
from conftest import writeCubeDDS

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])
faceColors = [(200, 40, 40, 255), (40, 200, 40, 255), (40, 40, 200, 255), (200, 200, 40, 255), (40, 200, 200, 255), (200, 40, 200, 255)]


def lookedUp(image, direction):
  """The texel Blender's Environment Texture node finds along a world direction: u = 0.5 - atan2(y, x) / 2pi, v = 0.5 + latitude / pi."""
  x, y, z = numpy.asarray(direction, dtype=numpy.float64) / numpy.linalg.norm(direction)
  u, v = 0.5 - math.atan2(y, x) / (2 * math.pi), 0.5 + math.asin(z) / math.pi
  height, width = image.shape[:2]
  return tuple(int(value) for value in image[min(int((1 - v) * height), height - 1), min(int(u * width), width - 1)])


def testTheLookupFindsTheFaceTheClientsWaterFindsForTheWorldsDirection(tmp_path):
  image = eqCubeMaps.environmentLookup(eqCubeMaps.cubeFaces(writeCubeDDS(tmp_path / "six_e.dds", 8, faceColors).read_bytes(), "six_e.dds"))
  assert image.shape == (16, 32, 4)
  # RegionWater looks the cube up at (x, z, y) of the world's reflected ray: up finds +Y, north (+Y in the world) finds +Z.
  directions = {(1, 0.1, 0.2): 0, (-1, 0.1, 0.2): 1, (0.1, 0.2, 1): 2, (0.1, 0.2, -1): 3, (0.1, 1, 0.2): 4, (0.1, -1, 0.2): 5}
  assert {direction: lookedUp(image, direction) for direction in directions} == {direction: faceColors[face] for direction, face in directions.items()}


def testAFacesTexelsKeepTheirPlaceInTheLookup(tmp_path):
  path = writeCubeDDS(tmp_path / "plain_e.dds", 8, [(0, 0, 0, 255)] * 6)
  data = bytearray(path.read_bytes())
  positiveX = numpy.frombuffer(data, dtype=numpy.uint8, count=8 * 8 * 4, offset=128).reshape(8, 8, 4).copy()
  positiveX[:4, 4:] = (0, 0, 255, 255)
  data[128:128 + positiveX.nbytes] = positiveX.tobytes()
  image = eqCubeMaps.environmentLookup(eqCubeMaps.cubeFaces(bytes(data), "plain_e.dds"))
  # D3D's +X face runs s along the cube's -z and t down its -y, so its top right quarter (stored blue first, read red) lies toward the
  # world's -y and +z: south and up.
  assert lookedUp(image, (1, -0.5, 0.5))[:3] == (255, 0, 0) and lookedUp(image, (1, 0.5, -0.5))[:3] == (0, 0, 0)


def testOnlyWholeCubeMapsAreRead(tmp_path):
  flat = eqgWriter.ddsBytes(numpy.zeros((8, 8, 4), dtype=numpy.uint8))
  with pytest.raises(ValueError, match="not a DDS cube map"):
    eqCubeMaps.cubeFaces(flat, "flat_e.dds")
  whole = writeCubeDDS(tmp_path / "cube_e.dds", 8, faceColors).read_bytes()
  with pytest.raises(ValueError, match="not six whole mip chains"):
    eqCubeMaps.cubeFaces(whole[:-4], "short_e.dds")
  fiveFaces = bytearray(whole)
  fiveFaces[113] = 0x7E
  with pytest.raises(ValueError, match="without all six faces"):
    eqCubeMaps.cubeFaces(bytes(fiveFaces), "five_e.dds")


@pytest.mark.clientData("clientFiles")
def testClientCubeMapsDecodeAsD3DXLoadsThem():
  # Face means (RGB) of what d3dx9_30's D3DXCreateCubeTextureFromFileInMemoryEx loads with EQGraphicsDX9.dll's arguments
  # (docs/clientRendering.md, Water): Crescent Reach's uncompressed env_noswap_day.dds, exactly, and two DXT5 cube maps, whose texels
  # D3DX rounds a level apart in places: Highpass Hold's, each face only its top level, and Brell's Temple's, each its whole mip chain.
  loaded = {
    ("crescent.eqg", "env_noswap_day.dds", 0.05): [(183.5, 185.0, 203.0), (179.5, 181.4, 200.7), (168.3, 182.2, 208.0), (82.9, 107.2, 137.2), (114.2, 126.3, 139.7), (183.0, 184.3, 202.6)],
    ("highpasshold.eqg", "env_tutorialb_noswap.dds", 1.0): [(83.5, 49.4, 23.6), (125.7, 106.5, 94.8), (148.7, 163.5, 195.1), (78.7, 38.0, 10.9), (83.4, 49.8, 24.2), (85.4, 50.5, 24.2)],
    ("brellstemple.eqg", "env_noswap_temple.dds", 1.0): [(0, 0, 0), (0, 0, 0), (69.9, 114.7, 115.1), (0, 0, 0), (0, 0, 0), (0, 0, 0)],
  }
  for (archiveName, textureName, tolerance), means in loaded.items():
    faces = eqCubeMaps.cubeFaces(eqArchive.EQArchive(everquestClient / archiveName).read(textureName), textureName)
    assert numpy.abs(faces[..., :3].reshape(6, -1, 3).mean(axis=1) - numpy.array(means)).max() <= tolerance, textureName
  # D3DX refuses Broodlands' 2D "environment" map as a cube (E_FAIL), so the client draws that water without a reflection.
  with pytest.raises(ValueError, match="not a DDS cube map"):
    eqCubeMaps.cubeFaces(eqArchive.EQArchive(everquestClient / "broodlands.eqg").read("ra_watertest_e_01.dds"), "ra_watertest_e_01.dds")
