import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import surveyFields


def testIslandsAreFoundAcrossVerticesSplitAtMaterialBorders():
  # Four unit quads in a row, each with its own four vertices, as zone files split vertices along material borders. Quads 0 and 1
  # share a material and so form one region; quads 2 and 3 are each a region of two triangles: islands.
  vertices, triangles = [], []
  for quad in range(4):
    base = len(vertices)
    vertices += [(quad, 0, 0), (quad + 1, 0, 0), (quad + 1, 1, 0), (quad, 1, 0)]
    triangles += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
  materials = numpy.repeat([0, 0, 1, 0], 2)
  assert surveyFields.islandShare(numpy.array(vertices, dtype=float), numpy.array(triangles), materials) == 0.5


def testSceneIsMeasuredAsTheClientsZonesAreAndRankedAmongThem(stageBlenderServer, tmp_path):
  for name, color in (("sand", (200, 180, 140, 255)), ("rock", (120, 80, 60, 255))):
    Image.new("RGBA", (8, 8), color).save(tmp_path / f"{name}.png")

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    for name in ("sand", "rock"):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(tmp_path / f"{name}.png")})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "rock", "selector": {"box": {"minimum": [0, 0, -1], "maximum": [8, 8, 1]}}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [6, 6, 6], "location": [20, 20, 0]})
    return await session.expectSuccess("compareWithClientZones", {"zones": ["nektulosa"]})

  compared = stageBlenderServer.session(steps)
  measures = {measure: entry["zone"] for measure, entry in compared["measures"].items()}
  assert compared["clientZones"] == ["nektulosa"] and compared["textures"] == ["sand", "rock"]
  # An 8 by 8 grid of quads is 128 triangles over 4096 square units; the one rock quad is an island of two triangles; the ground is
  # flat and only the boulder's sides are steep.
  assert measures["construction.terrainTriangles"] == 128 and measures["dimensions.footprint"] == 4096 and measures["construction.terrainTextures"] == 2
  assert measures["construction.terrainIslandTriangleShare"] == round(2 / 128, 4)
  assert measures["construction.terrainSteepShare"] == 0 and measures["construction.steepOnTerrainShare"] == 0 and measures["content.placementCount"] == 1
  for entry in compared["measures"].values():
    if entry["clientZones"]:
      assert entry["clientZones"] == 1 and entry["p10"] == entry["median"] == entry["p90"]
      assert entry["percentile"] == (0 if entry["zone"] < entry["median"] else 100 if entry["zone"] > entry["median"] else 50)
