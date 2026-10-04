import io

from PIL import Image

from conftest import writePNG
from testModelsAndDressing import freshScene

environment = {
  "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
  "sunAzimuthDegrees": 135, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 500, "fogEnd": 3000, "fogDensity": 0.33, "fogOn": True, "maxClip": 6000,
  "newEngineZone": False,
}


async def crateScene(session, tmp_path):
  await freshScene(session)
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [10, 10, 10], "location": [40, 40, 0]})
  for name, color in (("ground", (60, 140, 60, 255)), ("crate", (200, 60, 40, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": name})
  await session.expectSuccess("setZoneProperties", environment)


def testAFrameViewCentersItsObjectsAndCompareShowsOnlyWhatMoved(stageBlenderServer, tmp_path):
  async def steps(session):
    await crateScene(session, tmp_path)
    _, framed = await session.expectImage("renderView", {"view": {"frame": {"objects": ["crate"], "headingDegrees": 45, "pitchDegrees": -30}}})
    centered = await session.expectSuccess("pick", {"view": {"frame": {"objects": ["crate"], "headingDegrees": 45, "pitchDegrees": -30}}, "pixel": [480, 270]})
    fixed = {"eye": [0, -40, 40], "target": framed["target"]}
    _, before = await session.expectImage("renderView", {"view": fixed})
    _, again = await session.expectImage("renderView", {"view": fixed})
    await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [0, 0, 6]})
    _, after = await session.expectImage("renderView", {"view": fixed})
    unchanged = await session.expectImage("compareRenders", {"before": before["outputPath"], "after": again["outputPath"]}, mimeType="image/jpeg")
    moved = await session.expectImage("compareRenders", {"before": before["outputPath"], "after": after["outputPath"]}, mimeType="image/jpeg")
    orbit = await session.expectImage("renderOrbit", {"objects": ["crate"], "views": 4}, mimeType="image/jpeg")
    return framed, centered, unchanged, moved, orbit

  framed, centered, unchanged, moved, orbit = stageBlenderServer.session(steps)
  # The frame view looks at the crate's middle (40, 40, 5) from the southwest, down 30 degrees; the middle of the picture is the crate.
  assert framed["target"] == [40.0, 40.0, 5.0] and framed["eye"][0] < 40 and framed["eye"][1] < 40 and framed["eye"][2] > 5
  assert centered["object"] == "crate"
  # The same camera twice changes nothing; with the crate (small in a view from farther off) raised, only the part of the view around
  # it changes.
  assert unchanged[1]["changedShare"] == 0 and unchanged[1]["changedBounds"] is None
  left, top, right, bottom = moved[1]["changedBounds"]
  assert 0 < moved[1]["changedShare"] < 0.5 and left < 480 < right and top < 270 < bottom
  assert Image.open(io.BytesIO(moved[0])).size == (1952, 396)
  assert Image.open(io.BytesIO(orbit[0])).size == (2600, 396) and orbit[1]["views"] == 4
