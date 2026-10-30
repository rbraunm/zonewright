import base64
import io
import json
import math
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import buildTolerances
import laneDrawing
import planDrawing
import playerReach
import playerScale
import slopeBands

# The medium body group, which holds the walked human male, is the one these tests size their courses by.
medium = playerScale.groups["medium"]
playerHeight, stepHeight = medium.playerHeight, medium.stepHeight
from testCaves import caveCanyon, hall
from testModelsAndDressing import freshScene
from testReach import cellHeight, cellSize, cube, heightMargin, probes, reachCourse
from testWater import environment


def planFrame(plan):
  """The frame of a plan's drawing above its legend band, from a result's center, width, and frameSize."""
  return planDrawing.PlanFrame(plan["center"], plan["width"], plan["frameSize"])


def pixelsAround(image, plan, point, radius=4):
  """The drawing's pixels within radius of where a zone point falls in a plan, as an [n, 3] array."""
  x, y = (round(value) for value in planFrame(plan).pixel(point))
  return numpy.asarray(image.convert("RGB"))[y - radius:y + radius + 1, x - radius:x + radius + 1].reshape(-1, 3).astype(int)


def greenShare(pixels):
  """The share of pixels tinted the lane's walk green: green well over red and blue."""
  return float(((pixels[:, 1] > pixels[:, 0] + 30) & (pixels[:, 1] > pixels[:, 2] + 20)).mean())


def blueShare(pixels):
  """The share of pixels tinted the lane's swim blue: blue well over red and green."""
  return float(((pixels[:, 2] > pixels[:, 0] + 40) & (pixels[:, 2] > pixels[:, 1] + 20)).mean())


def orangeShare(pixels):
  """The share of pixels tinted the lane's slide orange: red over green over blue, each well apart."""
  return float(((pixels[:, 0] > pixels[:, 1] + 30) & (pixels[:, 1] > pixels[:, 2] + 30)).mean())


def pinkShare(pixels):
  """The share of pixels tinted the lane's squeeze pink: red well over green, and blue over it."""
  return float(((pixels[:, 0] > pixels[:, 1] + 30) & (pixels[:, 2] > pixels[:, 1] + 10)).mean())


def colorShare(pixels, color):
  return float((numpy.abs(pixels - numpy.array(color)).max(axis=1) <= 40).mean())


def ringShare(image, plan, point, radius=laneDrawing.problemRing):
  """The share of sixteen points on a problem ring's circle about where a zone point falls that are drawn in the ring's color."""
  x, y = planFrame(plan).pixel(point)
  pixels = numpy.asarray(image.convert("RGB")).astype(int)
  angles = numpy.linspace(0, 2 * math.pi, 16, endpoint=False)
  circle = [pixels[round(y + radius * math.sin(angle)), round(x + radius * math.cos(angle))] for angle in angles]
  return colorShare(numpy.array(circle), laneDrawing.problemColor)


def pixelsAlong(image, plan, start, end, half=2):
  """The drawing's pixels within half of the plan line from one zone point to another, as an [n, 3] array."""
  frame = planFrame(plan)
  (x0, y0), (x1, y1) = frame.pixel(start), frame.pixel(end)
  pixels = numpy.asarray(image.convert("RGB")).astype(int)
  rows = slice(round(min(y0, y1)) - half, round(max(y0, y1)) + half + 1)
  columns = slice(round(min(x0, x1)) - half, round(max(x0, x1)) + half + 1)
  return pixels[rows, columns].reshape(-1, 3)


def testMapReachDrawsTheLaneAndItsProblems(stageBlenderServer, tmp_path):
  async def steps(session):
    await reachCourse(session, tmp_path)
    whole, _ = await session.call("mapReach", {})
    framed = await session.expectImage("mapReach", {"center": [60, 40], "width": 240, "problemViews": False, "bodyGroup": "medium"})
    west = await session.expectImage("mapReach", {"center": [-137, 40], "width": 240, "problemViews": False, "bodyGroup": "medium"})
    edge = await session.expectImage("mapReach", {"center": [-215, 20], "width": 160, "problemViews": False, "bodyGroup": "medium"})
    ford = await session.expectImage("mapReach", {"center": [-110, -40], "width": 100, "problemViews": False, "bodyGroup": "medium"})
    leveled = await session.expectImage("mapReach", {"center": [160, 40], "width": 80, "level": {"bottom": 9, "top": 11}, "problemViews": False, "bodyGroup": "medium"})
    sketched = await session.expectImage("renderSketch", {"center": [60, 40], "width": 240, "layers": ["regions", "lane"]})
    plain = await session.expectImage("renderSketch", {"center": [60, 40], "width": 240, "layers": ["regions"]})
    return whole, framed, west, edge, leveled, sketched, plain, ford

  whole, framed, west, edge, leveled, sketched, plain, ford = stageBlenderServer.session(steps)
  assert whole.is_error is False, whole.content[0].text
  assert [content.type for content in whole.content] == ["image", "image", "text"]
  assert [content.mime_type for content in whole.content[:2]] == ["image/png", "image/jpeg"]
  report = json.loads(whole.content[2].text)
  wholePlan = Image.open(io.BytesIO(base64.b64decode(whole.content[0].data)))
  wholePlan.save(tmp_path / "coursePlan.png")
  (tmp_path / "courseProblems.jpg").write_bytes(base64.b64decode(whole.content[1].data))
  # The problem sheet views the first problem of each kind found while kinds outnumber its eight views, each looking down at its
  # problem's center from a raised eye.
  sheet = report["problemSheet"]
  chosen = playerReach.viewedProblems(report["problems"], 8)
  assert len({problem["kind"] for problem in report["problems"]}) > 8 and len({problem["kind"] for problem in chosen}) == 8
  assert [view["number"] for view in sheet["views"]] == [problem["number"] for problem in chosen]
  for view, problem in zip(sheet["views"], chosen):
    assert set(view["view"]) == {"eye", "target"} and view["view"]["target"] == [*problem["center"][:2], round(problem["center"][2] + buildTolerances.floorSightLift, 2)]
    assert view["view"]["eye"][2] > view["view"]["target"][2]
  assert Image.open(io.BytesIO(base64.b64decode(whole.content[1].data))).size == (sheet["width"], sheet["height"]) and sheet["width"] == 8 + 3 * (640 + 8)
  # Over the whole course, the open stretch of the ground's edge is drawn as a void edge and a walled stretch is not; the legend lies in
  # a band under the plan.
  plan = report["plan"]
  assert colorShare(pixelsAround(wholePlan, plan, [-240.5, -25], 3), laneDrawing.voidColor) > 0.1
  assert colorShare(pixelsAround(wholePlan, plan, [-240.5, 100], 3), laneDrawing.voidColor) == 0
  assert wholePlan.height > plan["frameSize"][1] and wholePlan.convert("RGB").getpixel((wholePlan.width - 4, wholePlan.height - 4)) == laneDrawing.legendBackground

  planImage, planReport = framed
  stations = Image.open(io.BytesIO(planImage))
  stations.save(tmp_path / "courseStations.png")
  # The framed plan draws the medium group's lane, which the stations are sized by. Walked ground and the platform the gentle ramp
  # reaches are green; the tops of the step over a player's step and of the platform past the steep ramp are cross-hatched, walkable floor never reached, and ringed as problems.
  assert greenShare(pixelsAround(stations, planReport["plan"], [20, -10])) > 0.9
  assert greenShare(pixelsAround(stations, planReport["plan"], [80, 45])) > 0.9
  for point in ([40, 70], [110, 43]):
    around = pixelsAround(stations, planReport["plan"], point)
    assert greenShare(around) == 0 and colorShare(around, laneDrawing.unreachedColor) > 0.1
  stepOver = next(problem for problem in planReport["problems"] if problem["kind"] == "unreachedPlayFloor" and abs(problem["center"][1] - 70) < 3)
  assert ringShare(stations, planReport["plan"], stepOver["center"]) > 0.8

  # West of the safe point the pool is swum, the bank over a swimmer's climb is never reached, and the low bank is walked, outlined by
  # the ledge it stands over the ground by; the shallow box is waded, and the bank only a swimmer climbs onto out of it is squeeze; the
  # stacked boxes are one water, swum and not dotted, and the floating box is swum, dotted over the ground it stands clear of. The
  # stranded entry's ring goes round its arrow.
  westImage, westReport = west
  westPlan, westFrame = Image.open(io.BytesIO(westImage)), westReport["plan"]
  westPlan.save(tmp_path / "courseStationsWest.png")
  assert blueShare(pixelsAround(westPlan, westFrame, [-124, 40])) > 0.5
  assert colorShare(pixelsAround(westPlan, westFrame, [-105, 46]), laneDrawing.unreachedColor) > 0.1
  assert greenShare(pixelsAround(westPlan, westFrame, [-105, 34])) > 0.9
  # The bank's north face at x -100 runs through the middles of the columns along it, which take its top, so the ledge runs half a
  # column past it.
  assert colorShare(pixelsAlong(westPlan, westFrame, [-100 + cellSize / 2, 31], [-100 + cellSize / 2, 37]), laneDrawing.ledgeColor) > 0.2
  shallowWater = pixelsAround(westPlan, westFrame, [-120, 10], 6)
  assert greenShare(shallowWater) > 0.4 and blueShare(shallowWater) > 0.2
  assert pinkShare(pixelsAround(westPlan, westFrame, [-105, 10])) > 0.9
  stacked, floating = pixelsAround(westPlan, westFrame, [-150, 10], 6), pixelsAround(westPlan, westFrame, [-170, 40], 6)
  # The large group's unreached swim in the upper stacked box is ringed there, in black and white over the medium group's water.
  stacked = stacked[~((stacked == 15).all(axis=1) | (stacked == 255).all(axis=1))]
  assert blueShare(stacked) > 0.9 and colorShare(stacked, laneDrawing.layeredColor) == 0
  assert blueShare(floating) > 0.8 and colorShare(floating, laneDrawing.layeredColor) > 0
  # Boxes too small in the drawing to hold their names are listed; the pool holds its own.
  assert "AWT_floating" in westFrame["unnamedSwimVolumes"] and "AWT_pool" not in westFrame["unnamedSwimVolumes"]
  stranded = next(problem for problem in westReport["problems"] if problem["kind"] == "disconnectedEntry")
  assert ringShare(westPlan, westFrame, stranded["center"], laneDrawing.ringRadii["disconnectedEntry"]) > 0.8
  edgeImage, edgeReport = edge
  edgePlan = Image.open(io.BytesIO(edgeImage))
  edgePlan.save(tmp_path / "courseStationsEdge.png")
  assert colorShare(pixelsAround(edgePlan, edgeReport["plan"], [-240.5, -30], 2), laneDrawing.voidColor) > 0.2
  # The face sliding off the edge is drawn slide orange, looked at clear of the trap's ring about its middle.
  assert orangeShare(pixelsAround(edgePlan, edgeReport["plan"], [probes["slide off"][0][0], 62], 2)) > 0.2
  # In one swim box, the ford's shelf, under water shallower than players swim in, is walked green lined with the swim blue as water
  # waded, and the deeper water beside it is swum blue; the box's outline, white under its dashes over every fill, shows alike along
  # the shelf and the deep water.
  fordImage, fordReport = ford
  fordPlan = Image.open(io.BytesIO(fordImage))
  fordPlan.save(tmp_path / "courseFord.png")
  shelf = pixelsAround(fordPlan, fordReport["plan"], [-120, -40], 6)
  assert greenShare(shelf) > 0.4 and blueShare(shelf) > 0.2
  assert blueShare(pixelsAround(fordPlan, fordReport["plan"], [-100, -40])) > 0.9
  for low, high in ((-128, -112), (-108, -92)):
    edge = pixelsAlong(fordPlan, fordReport["plan"], [low, -30], [high, -30])
    assert colorShare(edge, laneDrawing.swimBoxHalo) > 0.2 and colorShare(edge, planDrawing.swimColors["water"]) > 0.1

  # Kept to the band from 9 to 11, the plan draws the ledge's top and not the ground beside it.
  levelImage, levelReport = leveled
  levelPlan = Image.open(io.BytesIO(levelImage))
  assert levelReport["plan"]["level"] == {"bottom": 9, "top": 11}
  assert greenShare(pixelsAround(levelPlan, levelReport["plan"], [160, 36])) > 0.9
  assert greenShare(pixelsAround(levelPlan, levelReport["plan"], [185, 36])) == 0

  # renderSketch's lane layer lays the same fill over its plan, only when asked for, with the lane's legend under it and, in its report
  # alone, the constants, what is not modelled, and the limits mapReach states; the corridor past its zone line shows its never reached
  # hatch over the none region's.
  sketchImage, sketchReport = sketched
  plainImage, plainReport = plain
  sketch, bare = Image.open(io.BytesIO(sketchImage)), Image.open(io.BytesIO(plainImage))
  sketch.save(tmp_path / "courseSketchLane.png")
  assert "laneState" not in plainReport and bare.height == plainReport["frameSize"][1] and sketch.height > sketchReport["frameSize"][1]
  laneState = sketchReport["laneState"]
  assert laneState["lane"] == "reused" and {key: laneState[key] for key in ("constants", "notModeled", "limits")} == {key: report[key] for key in ("constants", "notModeled", "limits")}
  assert sorted(sketchReport.inline["laneState"]) == ["bodyGroup", "lane", "seconds"]
  assert greenShare(pixelsAround(sketch, sketchReport, [20, -10])) > 0.9
  assert greenShare(pixelsAround(bare, plainReport, [20, -10])) == 0
  assert colorShare(pixelsAround(sketch, sketchReport, [125, 103]), laneDrawing.unreachedColor) > 0.1


def composited(under, color, alpha):
  """Pixels with a color laid over them at alpha, as a plan composites a layer."""
  return under * (1 - alpha / 255) + numpy.array(color) * alpha / 255


def testUnreachedFloorIsDimmedAloneAndShowsTheLevelReachedUnderIt(stageBlenderServer, tmp_path):
  # A canopy hung a player's height and a margin over the walked ground, and a block rising more than the step: the tops of both are
  # walkable floor never reached, the canopy's over a level reached under it.
  canopyBottom = playerHeight + 2 * heightMargin
  canopy, block = ([10, 10, canopyBottom], [30, 30, canopyBottom + 1]), ([10, -30, 0], [30, -10, stepHeight + 2 * heightMargin])

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [96, 96], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [0, -30, 0, 0]})
    await cube(session, "canopy", *canopy)
    await cube(session, "block", *block)
    sketch = {"center": [15, 0], "width": 80, "layers": []}
    return await session.expectImage("renderSketch", sketch | {"layers": ["lane"], "bodyGroup": "medium"}), await session.expectImage("renderSketch", sketch)

  (lanedImage, lanedReport), (plainImage, plainReport) = stageBlenderServer.session(steps)
  assert lanedReport["frameSize"] == plainReport["frameSize"]
  (tmp_path / "unreachedTint.png").write_bytes(lanedImage)
  laned, plain = (numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB")).astype(float) for image in (lanedImage, plainImage))

  def window(point):
    """The pixels round where a zone point falls, halfway between the grid's lines, and which of them the never reached hatch crosses."""
    x, y = (round(value) for value in planFrame(lanedReport).pixel(point))
    down, across = numpy.mgrid[y - 12:y + 13, x - 12:x + 13]
    crossing = ((across - down) % laneDrawing.hatchSpacing == 0) | ((across + down) % laneDrawing.hatchSpacing == 0)
    return (slice(y - 12, y + 13), slice(x - 12, x + 13)), crossing

  # Both tops are cross-hatched. Over the walked ground under the canopy the walk fill shows between the hatch's lines, untouched by the
  # dimming laid between them over the block's top, where no level is reached under the floor never reached.
  between = {
    "canopy": ([25, 15], laneDrawing.labelColors[playerReach.walkLabel], laneDrawing.fillAlpha),
    "block": ([25, -15], laneDrawing.unreachedColor, laneDrawing.unreachedFillAlpha),
  }
  for name, (point, color, alpha) in between.items():
    place, crossing = window(point)
    hatch = composited(plain[place], laneDrawing.unreachedColor, laneDrawing.hatchAlpha)
    assert numpy.abs(laned[place][crossing] - hatch[crossing]).max() <= 2, name
    assert numpy.abs(laned[place][~crossing] - composited(plain[place], color, alpha)[~crossing]).max() <= 2, name


def testALanePlansLegendNamesThePlansOwnLayersInItsFrameAndTheSlopeLayersBands():
  """A plan 100 across about the origin holding a view region, a cave run with its floor heights and a branch's junction, a safe point's
  entry, a plot, and spot heights; a none region, a zone line, and a boundary lie outside it."""
  frame = planDrawing.PlanFrame([0, 0], 100, (960, 540))
  outside = [[400, 400], [420, 400], [420, 420], [400, 420]]
  overlays = {
    "sheets": [], "water": [], "swim": [], "spots": [{"at": [0, 0], "height": 0.0}], "slope": False,
    "regions": [{"name": "hill", "outline": [[-10, -10], [10, -10], [10, 10], [-10, 10]], "access": "view"}, {"name": "rim", "outline": outside, "access": "none"}],
    "caves": [{
      "label": "hall", "left": [[-20, -2], [20, -2]], "right": [[-20, 2], [20, 2]], "middle": [[-20, 0], [20, 0]], "endCap": [], "startCap": [],
      "floors": [{"at": [-20, 0], "floor": 2.0}], "junction": [-20, 0], "opening": None, "strokes": [], "meanFloor": 2.0,
    }],
    "plots": [{"address": "1 Hill Street", "corners": [[5, 5], [15, 5], [15, 15], [5, 15]], "facingDegrees": 0.0}],
    "entries": [{"label": "S", "at": [0, -20], "headingDegrees": 90.0, "isolated": False}],
    "zoneLines": [{"name": "ATP_1_far", "corners": outside}],
    "boundaries": [{"name": "rimWall", "lines": [outside], "areas": []}],
  }
  assert [text for _, _, text in laneDrawing.planLayerLegend(overlays, frame)] == [
    "relief: lighter is higher", "the ground's height at a grid crossing", "region, named", "view region: seen, never entered",
    "cave run: walls at its floor's height, middle dashed, over its pale floor", "cave floor height at a path point",
    "where a cave branch meets its parent", "plot, by address, its mark out of the entrance side", "entry: where players arrive, toward their heading",
  ]
  # With the slope layer on, its five bands follow the spot heights, each filled as it shows over the relief.
  withSlope = laneDrawing.planLayerLegend(overlays | {"slope": True}, frame)
  assert [text for _, _, text in withSlope[:8]] == [
    "relief: lighter is higher", "the ground's height at a grid crossing", "level: 0 to 10 degrees", "gentle: 10 to 25 degrees",
    "steep: 25 to 45 degrees", "scramble: 45 to 80 degrees", "wall or overhang: 80 degrees and steeper", "region, named",
  ]
  assert [(kind, color) for kind, color, _ in withSlope[2:7]] == [("fill", laneDrawing.overRelief(color, planDrawing.slopeAlpha)) for color in slopeBands.colors]


def testSeedAndIsolatedSeedSwatchesDiffer():
  # Inside its arrow a seed's swatch is the seeds' white fill; an isolated seed's shows the walked ground under it, apart from the
  # near-white legend band.
  items = [("arrow", laneDrawing.seedOutline, "seed"), ("hollowArrow", laneDrawing.seedOutline, "isolated seed")]
  sheet = laneDrawing.withLegend(Image.new("RGB", (600, 10), (0, 0, 0)), items)
  places, _ = laneDrawing.legendLayout(600, items)
  inside = [sheet.getpixel((left + 6, 10 + top + laneDrawing.legendSwatch // 2)) for left, top in places]
  walked = laneDrawing.overRelief(laneDrawing.labelColors[playerReach.walkLabel], laneDrawing.fillAlpha)
  assert inside == [laneDrawing.seedFill, walked] and math.dist(walked, laneDrawing.legendBackground) > 100


def testLaneWalksACaveUnderItsVaultAndShowsThroughItsPlanFloor(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [0, -100, 2, 0]})
    _, reached = await session.expectImage("mapReach", {"problemViews": False})
    sketched = await session.expectImage("renderSketch", {"center": [0, 120], "width": 360, "layers": ["caves", "entries", "lane"]})
    plain = await session.expectImage("renderSketch", {"center": [0, 120], "width": 360, "layers": ["caves", "entries"]})
    return reached, sketched, plain

  reached, sketched, plain = stageBlenderServer.session(steps)
  lane = playerReach.Lane(reached["laneFiles"]["medium"])
  nodes = playerReach.Nodes(lane)
  # The room is walked from the safe point on its lining's floor, under its vault (the room's height within its breakup), with the
  # hill's top over it.
  (roomX, roomStart, roomFloor), (_, roomEnd, _) = hall["path"][3:5]
  roomPoint = [roomX, (roomStart + roomEnd) / 2]
  room = nodes.floorNodeAt([*roomPoint, roomFloor])
  assert (playerReach.labelNames[nodes.label[room]], int(lane.read("originSeed")[room])) == ("walk", 0)
  assert abs(float(nodes.height[room]) - roomFloor) <= cellHeight
  vault = float(lane.height(nodes.high[room]))
  assert abs(vault - float(nodes.height[room]) - hall["heights"][3]) <= hall["breakup"]["amplitude"] + cellHeight
  assert float(nodes.height[nodes.floorNodeAt(roomPoint)]) > vault
  # In plan the lane's fill lies over the cave's pale floor and under its walls: the room shows walked green, dotted as a level under a
  # higher one, beside its middle line, where the plan without the lane shows the pale floor alone; its walls stay cave purple.
  sketchImage, sketchReport = sketched
  plainImage, plainReport = plain
  sketch, bare = Image.open(io.BytesIO(sketchImage)), Image.open(io.BytesIO(plainImage))
  sketch.save(tmp_path / "caveSketchLane.png")
  besideMiddle = [roomPoint[0] + hall["widths"][3] / 4, roomPoint[1]]
  inRoom = pixelsAround(sketch, sketchReport, besideMiddle)
  assert greenShare(inRoom) > 0.9 and colorShare(inRoom, laneDrawing.layeredColor) > 0
  assert greenShare(pixelsAround(bare, plainReport, besideMiddle)) == 0
  wall = [roomPoint[0] + hall["widths"][3] / 2, roomPoint[1]]
  assert colorShare(pixelsAround(sketch, sketchReport, wall, 2), planDrawing.caveColor) > 0.2
  assert colorShare(pixelsAround(bare, plainReport, wall, 2), planDrawing.caveColor) > 0.2
  # The room's blind end, which the cut rounds off over half its width beyond its last point, closes in a half circle there, and the
  # walked floor reaches round under it.
  (endX, endY, _), half = hall["path"][-1], hall["widths"][-1] / 2
  capWall = [endX + half * math.cos(math.radians(45)), endY + half * math.sin(math.radians(45))]
  assert sketchReport["caveEndsUnknown"] == [] and colorShare(pixelsAround(sketch, sketchReport, capWall, 2), planDrawing.caveColor) > 0.2
  assert greenShare(pixelsAround(sketch, sketchReport, [endX, endY + half / 2])) > 0.9
