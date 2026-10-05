import json
import struct
import sys
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqModels
import eqRaces
import eqZones
import zoneGeometry
import zoneSources

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])


def groundUnder(vertices, triangles, point):
  """The terrain height at a point's [x, y] nearest the point's own height, or None off the terrain."""
  corners = vertices[triangles]
  low, high = corners[:, :, :2].min(1), corners[:, :, :2].max(1)
  best = None
  for index in numpy.flatnonzero((low[:, 0] <= point[0]) & (high[:, 0] >= point[0]) & (low[:, 1] <= point[1]) & (high[:, 1] >= point[1])):
    a, b, c = corners[index]
    v0, v1, v2 = b[:2] - a[:2], c[:2] - a[:2], point[:2] - a[:2]
    denominator = v0[0] * v1[1] - v1[0] * v0[1]
    if abs(denominator) < 1e-9:
      continue
    u = (v2[0] * v1[1] - v1[0] * v2[1]) / denominator
    v = (v0[0] * v2[1] - v2[0] * v0[1]) / denominator
    if u >= -1e-6 and v >= -1e-6 and u + v <= 1 + 1e-6:
      z = a[2] + u * (b[2] - a[2]) + v * (c[2] - a[2])
      best = z if best is None or abs(z - point[2]) < abs(best - point[2]) else best
  return best


@pytest.mark.clientData("clientFiles")
def testClientTerrainStandsWhereItsVerticesAre():
  # Broodlands places its terrain turned a quarter and lowered 52.64; the client draws it where its vertices are, which is where the
  # trees, crates, and barrels placed on it stand.
  archive = eqArchive.EQArchive(everquestClient / "broodlands.eqg")
  zone = eqgFiles.parseZone(archive.read(next(name for name in archive.entries if name.endswith(".zon"))), "broodlands")
  terrain = next(placement for placement in zone["placements"] if placement["model"].endswith(".ter"))
  assert numpy.allclose(terrain["position"], (0, 0, -52.64), atol=0.01) and abs(terrain["rotation"][0] + numpy.pi / 2) < 1e-3
  matrix, offset = eqgFiles.drawnTransform(terrain)
  assert numpy.array_equal(matrix, numpy.identity(3)) and numpy.array_equal(offset, numpy.zeros(3))
  model = eqgFiles.parseModel(archive.read(terrain["model"]), terrain["model"])
  vertices = eqgFiles.placeVertices(model["vertices"], terrain)
  standing = [placement for placement in zone["placements"] if any(key in placement["model"] for key in ("tree", "crate", "barrel"))][:120]
  gaps = [abs(height - placement["position"][2]) for placement in standing if (height := groundUnder(vertices, model["triangles"], numpy.array(placement["position"]))) is not None]
  assert len(gaps) == len(standing) and sum(gap < 3 for gap in gaps) >= 0.75 * len(gaps)
  # Objects are still turned and moved by their placements.
  tree = standing[0]
  matrix, offset = eqgFiles.drawnTransform(tree)
  assert numpy.allclose(offset, tree["position"]) and numpy.allclose(matrix, eqgFiles.placementMatrix(tree))


def testClientWaterMaterialsReadAsLiquids():
  archive = eqArchive.EQArchive(everquestClient / "highpasshold.eqg")
  model = eqgFiles.parseModel(archive.read("ter_highpass05.ter"), "ter_highpass05.ter")
  liquids = {material["name"]: eqModels.eqgLiquid(material) for material in model["materials"]}
  water, falls = liquids["water"], liquids["waterfalls"]
  assert water["liquid"] == "water" and water["textures"] == {"normal": "rc_cavewater_n.dds", "environment": "env_tutorialb_noswap.dds"}
  # e_fWaterColor1 0xFF1A2433 and e_fWaterColor2 0xFF00457F, as 0-1 RGB.
  assert numpy.allclose(water["values"]["waterColor1"], [0x1A / 255, 0x24 / 255, 0x33 / 255])
  assert numpy.allclose(water["values"]["waterColor2"], [0, 0x45 / 255, 0x7F / 255])
  assert numpy.isclose(water["values"]["fresnelBias"], 0.3) and water["values"]["fresnelPower"] == 8.0
  assert falls == {"liquid": "waterfall", "values": {"slides": falls["values"]["slides"]}, "textures": {}} and numpy.allclose(falls["values"]["slides"], [-0.12, -0.32, 0.0, -0.5])
  assert all(liquid is None for name, liquid in liquids.items() if name not in ("water", "waterfalls"))


def testClientArtPlayersPassThroughAndCollisionShellsWithoutMaterialsRead():
  # Highpass Hold's lamp posts are solid art the client flags passable (0x1) on every triangle; the survey reads them as passable.
  archive = eqArchive.EQArchive(everquestClient / "highpasshold.eqg")
  lamp = eqgFiles.parseModel(archive.read("obp_lamposta.mod"), "obp_lamposta.mod")
  builder = zoneGeometry.GeometryBuilder()
  zoneGeometry.addEQGModel(builder, lamp, {"model": "obp_lamposta.mod", "position": (0, 0, 0), "rotation": (0, 0, 0), "scale": 1.0}, True)
  assert zoneGeometry.eqgMaterialSurface(lamp["materials"][0]) == zoneGeometry.surfaceCode["solid"] and (lamp["triangleFlags"] & eqgFiles.passableFlag).all()
  assert builder.build()["triangleSurfaces"].tolist() == [zoneGeometry.surfaceCode["passable"]] * 98
  # Ocean of Tears Green Village's hut collision shell has no materials and names material 0 on its 176 triangles: none drawn.
  data = eqArchive.EQArchive(everquestClient / "oceangreenvillage.eqg").read("obj_hutoutside_col.mod")
  raw = numpy.frombuffer(data, dtype=eqgFiles.modelTriangleType, count=176, offset=len(data) - 176 * eqgFiles.modelTriangleType.itemsize)
  shell = eqgFiles.parseModel(data, "obj_hutoutside_col.mod")
  assert raw["material"].tolist() == [0] * 176 and shell["materials"] == [] and shell["triangleMaterials"].tolist() == [-1] * 176


def drawnZoneParts(zoneName):
  """The variant importZone draws: its library, its .zon, and its placements as the build makes them, with the build's details."""
  source = eqZones.zoneSource(everquestClient, zoneName)
  library = zoneSources.ModelLibrary(zoneSources.assetArchivePaths(everquestClient, source)[0], zoneName)
  zone = eqZones.clientEQGZone(source, zoneName)
  return library, zone, *eqZones.eqgZoneParts(everquestClient, library, zone, library.archives[0], zoneName)


@pytest.mark.clientData("clientFiles")
def testShortLitFilesGiveTheColorsTheyHold():
  # Delve's door caps count their model's 1152 vertices in 400 bytes: the client reads the 98 colors the file holds and takes them, the
  # rest of its buffer whatever its memory pool held, which the preview draws as no baked light.
  litBytes = eqArchive.EQArchive(everquestClient / "delvea.eqg").read("obj_doorcaps6.lit")
  count, colors = eqZones.eqgLitColors(litBytes, "obj_doorcaps6.lit")
  assert (count, len(litBytes), len(colors)) == (1152, 400, 98) and colors.tolist() == list(struct.unpack_from("<98I", litBytes, 8))
  _, zone, parts, details = drawnZoneParts("delvea")
  assert details["bakedLightPastFileEnd"] == {"obj_doorcaps.mod": 6, "obp_dragvert.mod": 10, "obp_dragvert_.mod": 1} and details["bakedLightNotFitting"] == {}
  part = parts[next(index for index, placement in enumerate(zone["placements"]) if placement["name"].lower() == "obj_doorcaps6")]
  drawn = part["lighting"]["colors"]
  assert len(drawn) == 1152 and numpy.array_equal(drawn[:98], eqZones.bytesRGBA(colors)) and (drawn[98:] == eqZones.unlitColor).all()
  # Its count fits its model, so it is baked geometry: only the lights marked for it reach it.
  assert part["takesAllLights"] is False


@pytest.mark.clientData("clientFiles")
def testPlacedSkinnedModelsStandAtTheFirstKeyOfTheirDefaultAnimation():
  library, zone, parts, details = drawnZoneParts("guardian")
  assert details["animatedModels"] == {
    "obj_ceilingfan.mod": {"placements": 17, "animation": "OBJ_CEILINGFAN_DEFAULT"}, "obj_cntrl_panel.mod": {"placements": 12, "animation": "OBJ_CNTRL_PANEL_DEFAULT"},
    "obj_gearfurnacea.mod": {"placements": 9, "animation": "OBJ_GEARFURNACEA_DEFAULT"}, "obj_turbine.mod": {"placements": 2, "animation": "OBJ_TURBINE_DEFAULT"},
  }
  # The gear furnace's animation starts at its bind pose; loading it, the DLL lowers ROOT_BONE's keys by moddat.ini's ROffset for the
  # name's last three letters, [ULT], which the client's moddat.ini lacks: the default 3.125.
  model = library.model("obj_gearfurnacea.mod")
  tracks = library.animationTracks("OBJ_GEARFURNACEA_DEFAULT")
  bones = model["bones"]
  assert all(
    numpy.allclose(tracks[name][0]["position"], bones["position"][index], atol=1e-4) and numpy.allclose(tracks[name][0]["rotation"], bones["rotation"][index], atol=1e-4)
    for index, name in enumerate(bones["names"])
  )
  offsets = eqRaces.avatarOffsets(everquestClient)
  assert "ULT" not in offsets and eqModels.animationRootDrop("OBJ_GEARFURNACEA_DEFAULT", offsets) == 3.125
  index, placement = next((index, placement) for index, placement in enumerate(zone["placements"]) if placement["model"] == "obj_gearfurnacea.mod")
  matrix, offset = eqgFiles.drawnTransform(placement)
  part = parts[index]
  assert numpy.allclose(part["vertices"], (model["vertices"] - (0, 0, 3.125)) @ matrix.T + offset, atol=1e-4)
  # A CHierarchicalActor takes no baked light and every point light; the skinned effects read its texture coordinates unflipped.
  assert part["takesAllLights"] is True and (part["lighting"]["colors"] == eqZones.unlitColor).all() and numpy.array_equal(part["uvs"], model["uvs"])
  # Names holding _MT_, or whose first _IT is followed by a digit, are not lowered; others take their last three letters' ROffset.
  assert offsets["BAS"] == 0.577 and eqModels.animationRootDrop("L01_BA_1_BAS", offsets) == 0.577
  assert eqModels.animationRootDrop("C05_MT_BAS", offsets) == 0.0 and eqModels.animationRootDrop("X_IT7_BAS", offsets) == 0.0
  assert eqModels.animationRootDrop("X_ITA_IT7_BAS", offsets) == 0.577


@pytest.mark.clientData("clientFiles")
def testZoneBuildsCountWhatTheyDrawOtherwiseByModel():
  # Plane of Shadow's 13618 placements whose baked light does not fit come from 13 models; counted by model, the details stay small.
  _, _, _, details = drawnZoneParts("poshadow")
  notFitting = details["bakedLightNotFitting"]
  assert sum(notFitting.values()) == 13618 and len(notFitting) == 13 and notFitting["obp_florab.mod"] == 3703
  assert len(json.dumps(details, indent=2)) < 2000


@pytest.mark.clientData("clientFiles")
def testImportedEmitterListsNameTheLinesTheClientMakesNoEmitterFor(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "discordtower"})

  imported = stageBlenderServer.session(steps)
  # Every line comes in; Discord Tower's three negative indices and five lifespans of 0 make no emitter in the client.
  assert imported["emitters"] == 160
  negative = "the client makes no emitter for a negative definition index"
  assert imported["emittersNotMade"] == [
    {"reason": f"definition -101: {negative}", "count": 1, "emitters": ["brazier32"]},
    {"reason": f"definition -497: {negative}", "count": 1, "emitters": ["brazier02"]},
    {"reason": f"definition -7: {negative}", "count": 1, "emitters": ["brazier35"]},
    {"reason": "lifespan 0: the client makes an emitter only for a lifespan above 0", "count": 5, "emitters": ["brazier37", "brazier34", "brazier33", "brazier31", "brazier17"]},
  ]
