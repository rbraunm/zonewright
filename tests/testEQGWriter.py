import io
import json
import struct
import sys
from pathlib import Path

import numpy
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqgWriter

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])


def testFilenameCRCsMatchAClientArchivesDirectory():
  data = (everquestClient / "guildlobby.eqg").read_bytes()
  archive = eqArchive.EQArchive(everquestClient / "guildlobby.eqg")
  directoryOffset = struct.unpack_from("<I", data, 0)[0]
  count = struct.unpack_from("<I", data, directoryOffset)[0]
  stored = {struct.unpack_from("<I", data, directoryOffset + 4 + 12 * index)[0] for index in range(count)} - {eqgWriter.directoryCRC}
  assert len(stored) == len(archive.entries) > 100
  assert {eqgWriter.filenameCRC(name) for name in archive.entries} == stored


def testArchiveReadsBackWithItsDirectorySortedLikeTheClients(tmp_path):
  files = {f"file{index:03d}.bin": bytes(range(index % 255 + 1)) * (index * 37 + 1) for index in range(40)}
  files["big.dds"] = numpy.random.default_rng(1).integers(0, 256, 3 * eqgWriter.blockBytes + 5, dtype=numpy.uint8).tobytes()
  path = tmp_path / "test.eqg"
  path.write_bytes(eqgWriter.archiveBytes(files))
  archive = eqArchive.EQArchive(path)
  assert {name: archive.read(name) for name in archive.entries} == files
  data = path.read_bytes()
  directoryOffset = struct.unpack_from("<I", data, 0)[0]
  count = struct.unpack_from("<I", data, directoryOffset)[0]
  crcs = [struct.unpack_from("<I", data, directoryOffset + 4 + 12 * index)[0] for index in range(count)]
  assert count == len(files) + 1 and crcs == sorted(crcs)
  # Nothing follows the directory, as in the client's archives.
  assert len(data) == directoryOffset + 4 + 12 * count


def testArchiveRefusesUppercaseNamesAndEmptyFiles():
  with pytest.raises(ValueError, match="lowercase"):
    eqgWriter.archiveBytes({"Ter_Test.ter": b"x"})
  with pytest.raises(ValueError, match="empty"):
    eqgWriter.archiveBytes({"ter_test.ter": b""})


def triangleModel(kind):
  materials = [
    {"name": "stone", "diffuseTexture": "stone_c.dds", "normalTexture": "stone_n.dds", "cutout": False},
    {"name": "leaves", "diffuseTexture": "leaves_c.dds", "normalTexture": None, "cutout": True},
  ]
  positions = numpy.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [10, 10, 2]], dtype=numpy.float32)
  normals = numpy.array([[0, 0, 1]] * 4, dtype=numpy.float32)
  uvs = numpy.array([[0, 1], [1, 1], [0, 0], [1, 0.25]], dtype=numpy.float32)
  triangles = numpy.array([[0, 1, 2], [1, 3, 2]])
  return eqgWriter.modelBytes(kind, materials, positions, normals, uvs, triangles, [0, 1]), positions, normals, uvs, triangles


@pytest.mark.parametrize("kind", ["mod", "ter"])
def testModelsReadBackThroughTheModelReader(kind):
  data, positions, normals, uvs, triangles = triangleModel(kind)
  assert data[:4] == {"mod": b"EQGM", "ter": b"EQGT"}[kind]
  model = eqgFiles.parseModel(data, f"test.{kind}")
  assert numpy.array_equal(model["vertices"], positions) and numpy.array_equal(model["normals"], normals) and numpy.array_equal(model["uvs"], uvs)
  assert model["triangles"].tolist() == triangles.tolist() and model["triangleMaterials"].tolist() == [0, 1]
  assert model["triangleFlags"].tolist() == [0, 0]
  assert model["materials"] == [
    {"name": "stone", "shader": "Opaque_MaxCB1.fx", "properties": {"e_TextureDiffuse0": "stone_c.dds", "e_TextureNormal0": "stone_n.dds"}},
    {"name": "leaves", "shader": "Chroma_MPLBasicAT.fx", "properties": {"e_TextureDiffuse0": "leaves_c.dds"}},
  ]


def testACutoutWithANormalMapIsRefused():
  material = {"name": "card", "diffuseTexture": "card_c.dds", "normalTexture": "card_n.dds", "cutout": True}
  with pytest.raises(ValueError, match="cutout with a normal map"):
    eqgWriter.modelBytes("mod", [material], [[0, 0, 0]] * 3, [[0, 0, 1]] * 3, [[0, 0]] * 3, [[0, 1, 2]], [0])


def testZoneReadsBackThroughTheZoneReader():
  placements = [
    {"model": "ter_test.ter", "name": "TER_test", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0},
    {"model": "obj_rock.mod", "name": "OBJ_rock01", "position": (12.5, -3.0, 4.0), "rotation": (1.5, 0.25, -0.5), "scale": 2.0},
  ]
  lights = [{"name": "LIT_torch01", "position": (5.0, 6.0, 7.5), "color": (1.0, 0.5, 0.25), "radius": 60.0}]
  zone = eqgFiles.parseZone(eqgWriter.zoneBytes(["ter_test.ter", "obj_rock.mod"], placements, lights), "test.zon")
  assert zone["modelNames"] == ["ter_test.ter", "obj_rock.mod"] and zone["regionNames"] == [] and zone["lights"] == lights
  assert [placement["model"] for placement in zone["placements"]] == ["ter_test.ter", "obj_rock.mod"]
  rock = zone["placements"][1]
  assert rock["name"] == "OBJ_rock01" and rock["position"] == (12.5, -3.0, 4.0) and rock["rotation"] == (1.5, 0.25, -0.5) and rock["scale"] == 2.0
  assert struct.unpack_from("<I", eqgWriter.zoneBytes(["ter_test.ter"], placements[:1], []), 4)[0] == 1


def testALightOutsideTheClientsRangesIsRefused():
  placements = [{"model": "ter_test.ter", "name": "TER_test", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0}]
  with pytest.raises(ValueError, match="positive radius and RGB in 0-1"):
    eqgWriter.zoneBytes(["ter_test.ter"], placements, [{"name": "LIT_sun", "position": (0, 0, 0), "color": (2.0, 1.0, 1.0), "radius": 10.0}])


def testLitIsMagicCountAndOneD3DColorPerVertex():
  data = eqgWriter.litBytes([[0x12, 0x34, 0x56, 0x78], [255, 0, 0, 0]])
  assert data == b"EQGP" + struct.pack("<3I", 2, 0x78123456, 0x00FF0000)


def testDDSDecodesToItsPixelsWithEveryMipmap():
  rgba = numpy.random.default_rng(2).integers(0, 256, (4, 8, 4), dtype=numpy.uint8)
  data = eqgWriter.ddsBytes(rgba)
  assert struct.unpack_from("<I", data, 28)[0] == 4
  # 8x4, 4x2, 2x1 and 1x1 levels of four bytes a texel follow the 128-byte header.
  assert len(data) == 128 + 4 * (32 + 8 + 2 + 1)
  assert numpy.array_equal(numpy.asarray(Image.open(io.BytesIO(data)).convert("RGBA")), rgba)
  secondLevel = numpy.frombuffer(data, dtype=numpy.uint8, count=32, offset=128 + 128).reshape(2, 4, 4)[..., [2, 1, 0, 3]]
  assert numpy.array_equal(secondLevel, numpy.round(rgba.astype(float).reshape(2, 2, 4, 2, 4).mean(axis=(1, 3))).astype(numpy.uint8))


def testDDSRefusesSidesThatAreNotPowersOfTwo():
  with pytest.raises(ValueError, match="power-of-two"):
    eqgWriter.ddsBytes(numpy.zeros((3, 4, 4), dtype=numpy.uint8))
