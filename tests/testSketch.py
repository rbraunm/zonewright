import io

import numpy
from PIL import Image

from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials

environment = {
  "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "newEngineZone": False,
}
# A tavern facing east (+X): 30 deep along its facing and 40 across, so it spans x 135..165 and y -20..20; a shed 15 south of it; a yard
# 20 north; a road from the west edge through the basin into the tavern; the zone-in north of the basin.
campShapes = [
  {"name": "tavern", "kind": "footprint", "rectangle": {"center": [150, 0], "size": [40, 30], "headingDegrees": 90}, "height": 20, "label": "tavern"},
  {"name": "shed", "kind": "footprint", "rectangle": {"center": [150, -40], "size": [20, 10], "headingDegrees": 0}},
  {"name": "yard", "kind": "area", "outline": [[120, 40], [180, 40], [180, 100], [120, 100]], "floor": 0},
  {"name": "road", "kind": "path", "points": [[-150, 0], [150, 0]], "width": 10},
  {"name": "zoneIn", "kind": "point", "at": [0, 150], "facingDegrees": 180},
  {"name": "idea", "kind": "note", "at": [0, -150], "label": "market stalls along the road?"},
]


async def campScene(session, tmp_path):
  await freshScene(session)
  await basin(session)
  await liquidMaterials(session, tmp_path)
  await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
  await session.expectSuccess("setZoneProperties", environment)


def testASketchIsMeasuredAgainstTheGroundUnderIt(stageBlenderServer, tmp_path):
  async def steps(session):
    await campScene(session, tmp_path)
    drawn = await session.expectSuccess("sketch", {"sheet": "camp", "shapes": campShapes})
    taller = await session.expectSuccess("sketch", {"sheet": "camp", "shapes": [campShapes[0] | {"height": 30}]})
    objects = await session.expectSuccess("runPython", {"code": "result = sorted((o.name, len(o.data.vertices), len(o.data.polygons)) for o in bpy.data.objects if 'zonewrightSketch' in o)"})
    walk = await session.expectSuccess("walkRoute", {"path": [[120, 0, 0], [180, 0, 0]]})
    crossing = await session.expectError("sketch", {"sheet": "camp", "shapes": [{"name": "bad", "kind": "area", "outline": [[0, 0], [10, 10], [10, 0], [0, 10]]}]})
    unsure = await session.expectError("eraseSketch", {"sheet": "camp"})
    erased = await session.expectSuccess("eraseSketch", {"sheet": "camp", "names": ["shed"]})
    listed = await session.expectSuccess("getSketch", {"sheet": "camp"})
    return drawn, taller, objects["result"], walk, crossing, unsure, erased, listed

  drawn, taller, objects, walk, crossing, unsure, erased, listed = stageBlenderServer.session(steps)
  shapes = {shape["name"]: shape for shape in drawn["shapes"]}
  tavern = shapes["tavern"]
  assert tavern["area"] == 1200.0 and tavern["bounds"] == [[135.0, -20.0], [165.0, 20.0]] and tavern["facingDegrees"] == 90.0
  assert tavern["ground"]["lowest"] == 0.0 and tavern["ground"]["highest"] == 0.0 and tavern["ground"]["steepestDegrees"] == 0.0
  assert tavern["overlaps"] == [] and tavern["nearest"] == {"shape": "shed", "gap": 15.0}
  assert shapes["yard"]["ground"]["cut"] == 0.0 and shapes["yard"]["ground"]["fill"] == 0.0 and shapes["yard"]["nearest"]["gap"] == 20.0
  road = shapes["road"]
  # The road dips through the basin to its bed, 20 down, under the water.
  assert road["length"] == 300.0 and road["ground"]["lowest"] <= -19.5 and road["crosses"] == ["tavern"]
  assert road["ground"]["steepestDegrees"] < 15
  assert shapes["zoneIn"]["ground"] == 0.0 and shapes["idea"]["label"] == "market stalls along the road?"
  # Redrawn by name, the tavern is replaced, not doubled; with a height it stands as a block, the rest as outlines.
  assert taller["shapes"][0]["height"] == 30.0
  assert objects == [
    ["sketch.camp.idea", 1, 0], ["sketch.camp.road", 2, 0], ["sketch.camp.shed", 4, 0], ["sketch.camp.tavern", 8, 6],
    ["sketch.camp.yard", 4, 0], ["sketch.camp.zoneIn", 1, 0],
  ]
  # Players walk through a sketch: the route across the tavern's block keeps to the ground.
  assert walk["walkable"] and max(row["at"][2] for row in walk["profile"]) == 0.0
  assert "crosses itself" in crossing
  assert "Give names, the shapes to erase, or wholeSheet true" in unsure
  assert erased == {"sheet": "camp", "erased": 1, "remaining": 5}
  assert [shape["name"] for shape in listed["sheets"][0]["shapes"]] == ["idea", "road", "tavern", "yard", "zoneIn"]
  assert listed["sheets"][0]["shapes"][2]["nearest"]["shape"] == "yard"


def testAPlanDrawsSketchesOverAReliefMap(stageBlenderServer, tmp_path):
  async def steps(session):
    await campScene(session, tmp_path)
    await session.expectSuccess("sketch", {"sheet": "camp", "shapes": campShapes})
    return await session.expectImage("renderSketch", {"center": [0, 0], "width": 500})

  image, description = stageBlenderServer.session(steps)
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)
  assert pixels.shape == (810, 1440, 3) and description["sheetColors"] == {"camp": [200, 40, 40]}
  scale = 1440 / 500

  def at(x, y):
    return pixels[round(405 - y * scale), round(720 + x * scale)]

  # Inside the tavern the sheet's red fill shows over the grey relief; over the pool, the water's blue.
  tavern, pool, ground = at(150, 8), at(-40, 30), at(-200, -100)
  assert tavern[0] > tavern[1] + 40 and tavern[0] > tavern[2] + 40
  assert pool[2] > pool[0] + 30
  assert abs(ground[0] - ground[1]) < 12 and abs(ground[1] - ground[2]) < 12
