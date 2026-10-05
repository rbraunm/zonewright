"""Test plots for the structures toolset, built through the real bridge with the toolkit's own tools."""
import math

from conftest import writePNG

slopeDegrees = 15.0
cliffTop = 60.0
gorgeFloor = -40.0


def slopeHeight(y):
  """The test plot's slope south of y -40, rising toward -y."""
  return (-40.0 - y) * math.tan(math.radians(slopeDegrees))


async def testPlot(session, folder):
  """A 240 x 240 terrain `ground` at spacing 4 centered on the origin, in `terrain`: a gorge 80 wide at its rim (x -100 to -20) and 40
  deep, its floor x -80 to -40, the full length in y; a cliff 60 tall facing south for x 0 to 120, its face from y 12 (z 0) to y 20
  (z 60), its top running to y 120; a 15-degree slope rising toward -y south of y -40 for x 0 to 110; flat at z 0 elsewhere; no
  breakup; one solid material. Saved as folder/testPlot.blend."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createMaterial", {"name": "plotGround", "diffuseTexture": str(writePNG(folder / "plotGround.png", 4, 4, (120, 105, 85, 255)))})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [240, 240], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "plotGround"})
  await session.expectSuccess("sculptOutline", {
    "objectName": "ground", "mode": "carve", "outline": [[-100, -400], [-20, -400], [-20, 400], [-100, 400]], "base": gorgeFloor,
    "profile": [[0, -gorgeFloor], [20, 0], [600, 0]],
  })
  await session.expectSuccess("sculptOutline", {
    "objectName": "ground", "mode": "fill", "outline": [[0, 12], [400, 12], [400, 400], [0, 400]], "base": 0, "profile": [[0, 0], [8, cliffTop], [600, cliffTop]],
  })
  await session.expectSuccess("sculptAlongPath", {
    "objectName": "ground", "mode": "fill", "path": [[55, -40, 0], [55, -400, slopeHeight(-400)]], "radius": 55, "strength": 1, "profile": [[0, 0], [1, 0]],
  })
  await session.expectSuccess("saveFile", {"path": str(folder / "testPlot.blend")})
