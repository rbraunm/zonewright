import io
import math
import sys
from pathlib import Path

import numpy
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import planDrawing
from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials

environment = {
  "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000, "newEngineZone": False,
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
    await session.expectSuccess("sketch", {"sheet": "spare", "shapes": [{"name": "well", "kind": "point", "at": [0, 100]}]})
    wholly = await session.expectSuccess("eraseSketch", {"sheet": "camp", "wholeSheet": True})
    collections = (await session.expectSuccess("runPython", {"code": "result = sorted(c.name for c in bpy.data.collections if c.name.startswith('sketch'))"}))["result"]
    gone = await session.expectError("getSketch", {"sheet": "camp"})
    remaining = await session.expectSuccess("getSketch", {})
    return drawn, taller, objects["result"], walk, crossing, unsure, erased, listed, (wholly, collections, gone, remaining)

  drawn, taller, objects, walk, crossing, unsure, erased, listed, (wholly, collections, gone, remaining) = stageBlenderServer.session(steps)
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
  # Erased whole, a sheet takes its collection with it and leaves the other sheets.
  assert wholly == {"sheet": "camp", "erased": 5, "remaining": 0} and collections == ["sketch spare"]
  assert "No sketch sheet 'camp'; sheets: ['spare']" in gone and [sheet["sheet"] for sheet in remaining["sheets"]] == ["spare"]


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

  # Inside the tavern (clear of its name, set just above the spot height at its middle) the sheet's red fill shows over the grey
  # relief; over the pool, the water's blue.
  tavern, pool, ground = at(140, -12), at(-40, 30), at(-200, -100)
  assert tavern[0] > tavern[1] + 40 and tavern[0] > tavern[2] + 40
  assert pool[2] > pool[0] + 30
  assert abs(ground[0] - ground[1]) < 12 and abs(ground[1] - ground[2]) < 12


def testPlanNamesSitBesideTheSpotHeightsNotOnThem():
  image = Image.new("RGBA", (720, 405), (0, 0, 0, 0))
  board = planDrawing.LabelBoard(ImageDraw.Draw(image), (720, 405))
  frame = planDrawing.PlanFrame([0, 0], 300, (720, 405))
  marks = planDrawing.spotMarks(board, frame, [{"at": [x, y], "height": 23.0} for x in (-25, 0, 25) for y in (-25, 0, 25)])
  spotBoxes = [box for dot, number, _, _ in marks for box in (dot, number)]
  for box in spotBoxes:
    board.avoid(box)
  middle = planDrawing.centroidOf([frame.pixel(point) for point in [[-40, -40], [40, -40], [40, 40], [-40, 40]]])
  board.place(middle, "hollow", planDrawing.regionColor, 13)
  label = board.taken[-1]
  # The region's middle is a grid crossing with a spot height on it: the name there would cover the spot, so it moves beside it,
  # within a grid step of the middle and over no spot's dot or number.
  atMiddle = board.textBox(middle, "hollow", 13)
  assert any(planDrawing.boxesOverlap(atMiddle, box) for box in spotBoxes)
  assert len(board.taken) == 1 and not any(planDrawing.boxesOverlap(label, box) for box in spotBoxes)
  step = frame.length(25)
  assert abs((label[0] + label[2]) / 2 - middle[0]) < step and abs((label[1] + label[3]) / 2 - middle[1]) < step
  # So no spot height gives way to it: every one is drawn, its dot and its number.
  planDrawing.drawSpots(board, marks)
  assert len(board.taken) == 1 + len(spotBoxes)


def testASpotHeightGivesWayToANameWithNowhereElseToGo():
  image = Image.new("RGBA", (720, 405), (0, 0, 0, 0))
  board = planDrawing.LabelBoard(ImageDraw.Draw(image), (720, 405))
  frame = planDrawing.PlanFrame([0, 0], 300, (720, 405))
  marks = planDrawing.spotMarks(board, frame, [{"at": [0, 0], "height": 23.0}])
  for dot, number, _, _ in marks:
    board.avoid(dot)
    board.avoid(number)
  # The name may not leave a box hugging it; the spot height inside the box gives way rather than the name being left out.
  middle = frame.pixel((0, 0))
  around = board.textBox(middle, "hollow", 13)
  spot = board.clearSpot(middle, "hollow", 13, within=around)
  assert spot == middle
  board.write(spot, "hollow", planDrawing.regionColor, 13)
  planDrawing.drawSpots(board, marks)
  assert len(board.taken) == 1


def testAPlanDrawsShapesInsideAnAreaOverItsFill(stageBlenderServer):
  # The yard's name sorts after the house and the note inside it, and another sheet's footprint lies under it too.
  yard = [
    {"name": "yard", "kind": "area", "outline": [[-100, -60], [100, -60], [100, 60], [-100, 60]]},
    {"name": "aHouse", "kind": "footprint", "rectangle": {"center": [-50, 0], "size": [30, 30], "headingDegrees": 0}, "height": 10},
    {"name": "bNote", "kind": "note", "at": [40, 30], "label": "a note"},
  ]

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 400], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("sketch", {"sheet": "camp", "shapes": yard})
    await session.expectSuccess("sketch", {"sheet": "other", "shapes": [{"name": "under", "kind": "footprint", "rectangle": {"center": [50, -25], "size": [24, 24], "headingDegrees": 0}}]})
    return await session.expectImage("renderSketch", {"center": [0, 0], "width": 300, "layers": [], "spotHeights": False})

  image, description = stageBlenderServer.session(steps)
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)
  assert description["sheetColors"] == {"camp": [200, 40, 40], "other": [30, 90, 200]}

  def at(x, y):
    return pixels[round(405 - y * 4.8), round(720 + x * 4.8)]

  # The house's fill shows red over the yard's faint tint, the other sheet's footprint blue, and the note's words are written.
  house, under, bare = at(-58, -8), at(56, -31), at(-80, 40)
  assert house[0] > house[1] + 60 and house[0] > house[2] + 60 and bare[0] - bare[1] < 40
  assert under[2] > under[0] + 40
  note = pixels[round(405 - 30 * 4.8) - 12:round(405 - 30 * 4.8) + 12, round(720 + 40 * 4.8) - 50:round(720 + 40 * 4.8) + 50]
  assert (numpy.abs(note - numpy.array([200, 40, 40])).max(axis=-1) <= 35).sum() > 20


def testMeasuresAndSpotHeightsNeverShowNegativeZero(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, -0.01]})
    return await session.expectSuccess("sketch", {"sheet": "flat", "shapes": [
      {"name": "court", "kind": "area", "outline": [[-50, -50], [50, -50], [50, 50], [-50, 50]], "floor": 0},
      {"name": "lane", "kind": "path", "points": [[-80, 0], [80, 0]]},
      {"name": "well", "kind": "point", "at": [0, 0]},
    ]})

  drawn = stageBlenderServer.session(steps)

  def numbers(value):
    if isinstance(value, float):
      yield value
    elif isinstance(value, dict):
      for item in value.values():
        yield from numbers(item)
    elif isinstance(value, list):
      for item in value:
        yield from numbers(item)

  # The ground at -0.01 measures 0 to a tenth: never -0.0, nor a spot height of -0.
  zeros = [number for number in numbers(drawn) if number == 0]
  assert len(zeros) > 5 and all(math.copysign(1.0, number) == 1.0 for number in zeros)
  assert planDrawing.heightLabel(-0.3) == "0" and planDrawing.heightLabel(-0.6) == "-1"


def testASectionDrawsAreaFloorsAndAStairAlongIt(stageBlenderServer):
  court = [
    {"name": "lowerCourt", "kind": "area", "rectangle": {"center": [0, -30], "size": [100, 80], "headingDegrees": 0}, "floor": 0},
    {"name": "upperTerrace", "kind": "area", "rectangle": {"center": [0, 110], "size": [100, 60], "headingDegrees": 0}, "floor": 40},
    {"name": "stair", "kind": "path", "points": [[30, 10, 0], [30, 80, 40]], "width": 12},
    {"name": "crossing", "kind": "path", "points": [[-60, 50, 20], [60, 50, 20]], "width": 10},
  ]

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [300, 300], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sketch", {"sheet": "court", "shapes": court})
    return await session.expectImage("renderSection", {"start": [30, -60], "end": [30, 140], "bottom": -20, "top": 60})

  image, cut = stageBlenderServer.session(steps)
  # Along x 30 from y -60: the lower court's floor from the line's start to its north edge (s 70), the stair rising along the line
  # from there to the terrace's floor at 40, and the path crossing at y 50 level across its 10 width.
  assert {entry["shape"]: entry["pieces"] for entry in cut["sketch"]} == {
    "crossing": [[105.0, 20.0, 115.0, 20.0]], "lowerCourt": [[0.0, 0.0, 70.0, 0.0]], "stair": [[70.0, 0.0, 140.0, 40.0]],
    "upperTerrace": [[140.0, 40.0, 200.0, 40.0]],
  }
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)
  # 200 along and 80 up at 6.6 pixels a unit; the stair drawn in the sheet's red halfway up.
  column, row = round(60 + 6.6 * 87.5), round(669 - (10 + 20) * 6.6)
  assert (numpy.abs(pixels[row - 3:row + 4, column - 3:column + 4] - numpy.array([200, 40, 40])).max(axis=-1) <= 30).any()


def testAPlanNamesOnlyTheAreasThatReachIntoIt(stageBlenderServer):
  areas = [
    {"name": "farNorth", "kind": "area", "rectangle": {"center": [-100, 600], "size": [100, 100], "headingDegrees": 0}},
    {"name": "edge", "kind": "area", "rectangle": {"center": [80, 100], "size": [100, 80], "headingDegrees": 0}},
  ]

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [1600, 1600], "spacing": 20, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("sketch", {"sheet": "camp", "shapes": areas})
    return await session.expectImage("renderSketch", {"center": [0, 0], "width": 300, "layers": [], "spotHeights": False})

  image, _ = stageBlenderServer.session(steps)
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)

  def red(rows, columns):
    return (numpy.abs(pixels[rows, columns] - numpy.array([200, 40, 40])).max(axis=-1) <= 35).sum()

  # 4.8 pixels a unit: the frame reaches y 84.4. The area crossing its top edge (x 30..130, columns 864..1344) is named just inside
  # the top; the one wholly north of it (x -150..-50, columns 0..480) is not named at all.
  assert red(slice(0, 60), slice(954, 1254)) > 20
  assert red(slice(0, 60), slice(0, 480)) == 0
