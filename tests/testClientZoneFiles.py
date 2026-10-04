import json
import sys
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqModels
import zoneGeometry

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
