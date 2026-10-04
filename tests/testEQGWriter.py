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
import zoneGeometry

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
  return eqgWriter.modelBytes(kind, materials, positions, normals, uvs, triangles, [0, 1], [0, 0]), positions, normals, uvs, triangles


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


def testLiquidMaterialsCarryTheClientsShaderProperties():
  values = {"fresnelBias": 0.25, "fresnelPower": 8.0, "reflectionAmount": 0.7, "reflectionColor": [1, 1, 1], "waterColor1": [0, 0.04, 0.11], "waterColor2": [0, 0.23, 0.17], "slides": [0.02, 0.02, 0.03, 0.03]}
  materials = [
    {"name": "pond", "diffuseTexture": "water_c.dds", "normalTexture": "water_n.dds", "cutout": False, "liquid": {"liquid": "water", "values": values, "environmentTexture": "water_e.dds"}},
    {"name": "falls", "diffuseTexture": "fall_c.dds", "normalTexture": None, "cutout": False, "liquid": {"liquid": "waterfall", "values": {"slides": [0, 0.3, 0, 0.2]}}},
    {"name": "magma", "diffuseTexture": "lava_c.dds", "normalTexture": "lava_n.dds", "cutout": False, "liquid": {"liquid": "lava", "values": {"slides": [0.01, 0, 0, 0.03]}, "secondDiffuseTexture": "lava2_c.dds"}},
  ]
  data = eqgWriter.modelBytes("mod", materials, [[0, 0, 0]] * 3, [[0, 0, 1]] * 3, [[0, 0]] * 3, [[0, 1, 2]], [0], [0])
  pond, falls, magma = eqgFiles.parseModel(data, "liquids.mod")["materials"]
  assert pond["shader"] == "Opaque_MaxWater.fx" and falls["shader"] == "Opaque_MaxWaterFall.fx" and magma["shader"] == "Opaque_MaxLava.fx"
  # Colors are 0xAARRGGBB, as the client's water materials hold them (its most common first color is 0xFF00191C).
  assert pond["properties"] == pytest.approx({
    "e_TextureDiffuse0": "water_c.dds", "e_TextureNormal0": "water_n.dds", "e_TextureEnvironment0": "water_e.dds", "e_fFresnelBias": 0.25,
    "e_fFresnelPower": 8.0, "e_fWaterColor1": 0xFF000A1C, "e_fWaterColor2": 0xFF003B2B, "e_fReflectionAmount": 0.7, "e_fReflectionColor": 0xFFFFFFFF,
    "e_fSlide1X": 0.02, "e_fSlide1Y": 0.02, "e_fSlide2X": 0.03, "e_fSlide2Y": 0.03,
  })
  assert falls["properties"] == pytest.approx({"e_TextureDiffuse0": "fall_c.dds", "e_fSlide1X": 0, "e_fSlide1Y": 0.3, "e_fSlide2X": 0, "e_fSlide2Y": 0.2})
  assert list(magma["properties"])[:3] == ["e_TextureDiffuse0", "e_TextureDiffuse1", "e_TextureNormal0"] and magma["properties"]["e_TextureDiffuse1"] == "lava2_c.dds"


def testARegionWithoutExtentIsRefused():
  placements = [{"model": "ter_test.ter", "name": "TER_test", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0}]
  with pytest.raises(ValueError, match="positive half extents"):
    eqgWriter.zoneBytes(["ter_test.ter"], placements, [{"name": "AWT_flat", "center": (0, 0, 0), "halfExtents": (4.0, 4.0, 0.0)}], [])


def testACutoutWithANormalMapIsRefused():
  material = {"name": "card", "diffuseTexture": "card_c.dds", "normalTexture": "card_n.dds", "cutout": True}
  with pytest.raises(ValueError, match="cutout with a normal map"):
    eqgWriter.modelBytes("mod", [material], [[0, 0, 0]] * 3, [[0, 0, 1]] * 3, [[0, 0]] * 3, [[0, 1, 2]], [0], [0])


def wallAndCardModel():
  """A terrain with a stone floor, a cutout card players pass through, and an invisible wall: materials 0, 1, and none."""
  materials = [
    {"name": "stone", "diffuseTexture": "stone_c.dds", "normalTexture": None, "cutout": False},
    {"name": "leaves", "diffuseTexture": "leaves_c.dds", "normalTexture": None, "cutout": True},
  ]
  positions = [[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]]
  return materials, positions, [[0, 0, 1]] * 4, [[0, 0]] * 4, [[0, 1, 2], [0, 2, 3], [0, 3, 1]]


def testInvisibleWallsAndPassableTrianglesReadBack():
  materials, positions, normals, uvs, triangles = wallAndCardModel()
  model = eqgFiles.parseModel(eqgWriter.modelBytes("ter", materials, positions, normals, uvs, triangles, [0, 1, -1], [0, eqgFiles.passableFlag, 0]), "walls.ter")
  assert model["triangleMaterials"].tolist() == [0, 1, -1] and model["triangleFlags"].tolist() == [0, 1, 0]
  # The survey reads the stone as solid, the card as cutout, and the wall as invisible; a solid triangle flagged passable is passable.
  builder = zoneGeometry.GeometryBuilder()
  zoneGeometry.addEQGModel(builder, model, {"model": "walls.ter", "position": (0, 0, 0), "rotation": (0, 0, 0), "scale": 1.0}, False)
  flaggedStone = eqgFiles.parseModel(eqgWriter.modelBytes("ter", materials, positions, normals, uvs, triangles, [0, 1, -1], [1, 1, 0]), "flagged.ter")
  zoneGeometry.addEQGModel(builder, flaggedStone, {"model": "flagged.ter", "position": (0, 0, 0), "rotation": (0, 0, 0), "scale": 1.0}, False)
  kinds = [zoneGeometry.surfaceKinds[code] for code in builder.build()["triangleSurfaces"]]
  assert kinds == ["solid", "cutout", "invisible", "passable", "cutout", "invisible"]


def testAWallPlayersPassThroughAndUnknownFlagsAreRefused():
  materials, positions, normals, uvs, triangles = wallAndCardModel()
  with pytest.raises(ValueError, match="invisible wall"):
    eqgWriter.modelBytes("ter", materials, positions, normals, uvs, triangles, [0, 1, -1], [0, 0, eqgFiles.passableFlag])
  with pytest.raises(ValueError, match="flags 0 or 1"):
    eqgWriter.modelBytes("ter", materials, positions, normals, uvs, triangles, [0, 1, 0], [0, 2, 0])
  with pytest.raises(ValueError, match="or -1 for none"):
    eqgWriter.modelBytes("ter", materials, positions, normals, uvs, triangles, [0, 1, -2], [0, 0, 0])


def testACollisionModelWithoutMaterialsReadsAsInvisible():
  _, positions, normals, uvs, triangles = wallAndCardModel()
  data = bytearray(eqgWriter.modelBytes("mod", [], positions, normals, uvs, triangles, [-1, -1, -1], [0, 0, 0]))
  # Collision models the client ships without materials name material 0 on every triangle; the client draws none of them.
  records = numpy.frombuffer(data, dtype=eqgFiles.modelTriangleType, count=3, offset=len(data) - 3 * eqgFiles.modelTriangleType.itemsize).copy()
  records["material"] = 0
  data[len(data) - records.nbytes:] = records.tobytes()
  model = eqgFiles.parseModel(bytes(data), "shell_col.mod")
  assert model["materials"] == [] and model["triangleMaterials"].tolist() == [-1, -1, -1]
  records["material"] = 1
  data[len(data) - records.nbytes:] = records.tobytes()
  with pytest.raises(ValueError, match="with 0 materials"):
    eqgFiles.parseModel(bytes(data), "shell_col.mod")


def testZoneReadsBackThroughTheZoneReader():
  placements = [
    {"model": "ter_test.ter", "name": "TER_test", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0},
    {"model": "obj_rock.mod", "name": "OBJ_rock01", "position": (12.5, -3.0, 4.0), "rotation": (1.5, 0.25, -0.5), "scale": 2.0},
  ]
  lights = [{"name": "LIT_torch01", "position": (5.0, 6.0, 7.5), "color": (1.0, 0.5, 0.25), "radius": 60.0}]
  regions = [{"name": "AWT_pool01", "center": (8.0, -4.0, -6.5), "halfExtents": (16.0, 8.0, 7.5)}]
  zone = eqgFiles.parseZone(eqgWriter.zoneBytes(["ter_test.ter", "obj_rock.mod"], placements, regions, lights), "test.zon")
  assert zone["modelNames"] == ["ter_test.ter", "obj_rock.mod"] and zone["lights"] == lights
  assert zone["regions"] == [regions[0] | {"rotation": (0.0, 0.0, 0.0)}]
  assert [placement["model"] for placement in zone["placements"]] == ["ter_test.ter", "obj_rock.mod"]
  rock = zone["placements"][1]
  assert rock["name"] == "OBJ_rock01" and rock["position"] == (12.5, -3.0, 4.0) and rock["rotation"] == (1.5, 0.25, -0.5) and rock["scale"] == 2.0
  assert struct.unpack_from("<I", eqgWriter.zoneBytes(["ter_test.ter"], placements[:1], [], []), 4)[0] == 1


def testALightOutsideTheClientsRangesIsRefused():
  placements = [{"model": "ter_test.ter", "name": "TER_test", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0}]
  with pytest.raises(ValueError, match="positive radius and RGB in 0-1"):
    eqgWriter.zoneBytes(["ter_test.ter"], placements, [], [{"name": "LIT_sun", "position": (0, 0, 0), "color": (2.0, 1.0, 1.0), "radius": 10.0}])


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
