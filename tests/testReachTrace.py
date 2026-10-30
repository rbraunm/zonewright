import base64
import io
import json
import math
import sys
from pathlib import Path

import numpy
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import buildTolerances
import laneDrawing
import laneViews
import planDrawing
import playerReach
import playerScale

# The medium body group, which holds the walked human male, is the one these tests size their courses by.
medium = playerScale.groups["medium"]
eyeHeight, playerHeight, stepHeight = medium.eyeHeight, medium.playerHeight, medium.stepHeight
from testCaves import caveCanyon, hall
from testModelsAndDressing import freshScene
from testReach import cellHeight, cellSize, containmentHeadroom, cube, heightMargin, otherZone, probes, rampAlongY, reachCourse, steepDegrees
from testReachPictures import colorShare, pixelsAround, planFrame
from testWalkRoute import rampArguments
from testWater import environment

# The trace course: a corridor along +y between boundary walls, its ground slabs 20 thick. Players arrive at its south end, step up two
# risers onto a block whose far side drops more than a step, drop into a trench's water and climb out over its far bank, pass a cliff,
# a slab hung lower over the ground than the containment headroom, and a canopy they walk under, and drop into a pit whose far bank
# stands higher over its water than they climb out.
corridorHalf = 8.0
groundEdge = 12.0
wallHeight = 20.0
slabBottom = -20.0
# On the lane's height grid, a margin or more under the step.
riser = math.floor((stepHeight - heightMargin) / cellHeight) * cellHeight
# The second riser stands back from the first by less than a player's height of tread: the two are one climb run.
riserB = -10 + playerHeight / 2
bed = -(stepHeight + playerHeight)
waterTop = -heightMargin
trench = (40.0, 52.0)
pit = (100.0, 112.0)
farEnd = 130.0
highBank = waterTop + stepHeight + 2 * heightMargin
cliffHeight = 2 * stepHeight + 2 * heightMargin
lidGap = containmentHeadroom / 2
canopyBottom = playerHeight + 2 * heightMargin
canopyTop = canopyBottom + 2
canopy = ((-corridorHalf + 1, 60.0), (-3.0, 70.0))
safePoint = [0, -30, 0, 0]
gate = [0, 90]
routeTarget = [0, 70]
# The ground's slabs are grey and the blocks on them tan, each checkered, so a view shows each face's extent and its edges.
groundSlabs = ("groundA", "trenchBed", "groundB", "pitBed", "groundC")
blocks = ("riserA", "riserB", "cliff", "lowLid", "canopy")
checkerRepeat = 4.0
raisedHeight = 20.0


def checker(path, light, dark):
  image = Image.new("RGB", (64, 64), light)
  draw = ImageDraw.Draw(image)
  draw.rectangle([0, 0, 31, 31], fill=dark)
  draw.rectangle([32, 32, 63, 63], fill=dark)
  image.save(path)
  return path


async def traceCourse(session, folder):
  await freshScene(session)
  await cube(session, "groundA", [-groundEdge, -40, slabBottom], [groundEdge, trench[0], 0])
  await cube(session, "trenchBed", [-groundEdge, trench[0], slabBottom], [groundEdge, trench[1], bed])
  await cube(session, "groundB", [-groundEdge, trench[1], slabBottom], [groundEdge, pit[0], 0])
  await cube(session, "pitBed", [-groundEdge, pit[0], slabBottom], [groundEdge, pit[1], bed])
  await cube(session, "groundC", [-groundEdge, pit[1], slabBottom], [groundEdge, farEnd, highBank])
  await cube(session, "riserA", [-groundEdge, -10, 0], [groundEdge, 10, riser])
  await cube(session, "riserB", [-groundEdge, riserB, riser], [groundEdge, 10, 2 * riser])
  # Blocks stand a column inside the walls: a wall follows the highest ground it meets, so over a block's top its foot would leave a gap.
  await cube(session, "cliff", [2, 75, 0], [corridorHalf - 1, 85, cliffHeight])
  await cube(session, "lowLid", [-corridorHalf + 1, 85, lidGap], [-2, 95, wallHeight])
  await cube(session, "canopy", [canopy[0][0], canopy[0][1], canopyBottom], [canopy[1][0], canopy[1][1], canopyTop])
  for material, names, light, dark in (("courseGround", groundSlabs, (150, 150, 145), (115, 115, 110)), ("courseBlock", blocks, (200, 170, 120), (160, 130, 85))):
    await session.expectSuccess("createMaterial", {"name": material, "diffuseTexture": str(checker(folder / f"{material}.png", light, dark))})
    for name in names:
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": material})
      await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": checkerRepeat})
  await session.expectSuccess("placeSwimVolume", {"name": "trench", "liquid": "water", "minimum": [-groundEdge, trench[0], bed], "maximum": [groundEdge, trench[1], waterTop]})
  await session.expectSuccess("placeSwimVolume", {"name": "pit", "liquid": "water", "minimum": [-groundEdge, pit[0], bed], "maximum": [groundEdge, pit[1], waterTop]})
  # The wall follows the ground between samples, so a sample stands just inside each bank: one on the ground beside a bank would carry
  # the wall's foot over the water's bed, and the lane would find the gap under it.
  banks = [trench[0] + 0.5, trench[1] - 0.5, pit[0] + 0.5, pit[1] - 0.5]
  side = [[corridorHalf, y] for y in [-38, *banks, farEnd - 2]]
  ring = side + [[-x, y] for x, y in side[::-1]] + [side[0]]
  await session.expectSuccess("placeBoundaryWall", {"name": "corridorWall", "path": ring, "height": wallHeight})
  await session.expectSuccess("setZoneProperties", environment | {"safePoint": safePoint})
  await session.expectImage("placeEntry", {"name": "gate", "at": gate, "headingDegrees": 180, "kind": "landing"})


def figureAside(trace):
  """Where a trace view's scale figure stands across its frame, as a share of the way from its middle to its edge at her distance (the
  view's right positive; the view's heading and her place are given to hundredths and thousandths), and how far off her nearest place's
  floor."""
  view, figure, nearest = trace["view"]["view"], trace["view"]["figure"], trace["nearest"]["at"]
  heading = math.radians(view["headingDegrees"])
  offset = numpy.array(figure[:2]) - view["standAt"][:2]
  along, across = offset @ [math.sin(heading), math.cos(heading)], offset @ [math.cos(heading), -math.sin(heading)]
  return float(across / (along * math.tan(math.radians(laneViews.viewHalfField)))), abs(figure[2] - nearest[2])


def near(point, expected, tolerance=math.sqrt(2) * playerReach.reachGrid["cellSize"]):
  return math.dist(point[:2], expected[:2]) <= tolerance


def underCanopy(point):
  return canopy[0][0] <= point[0] <= canopy[1][0] and canopy[0][1] <= point[1] <= canopy[1][1]


def pixelsOf(image):
  return numpy.asarray(image.convert("RGB")).astype(int)


def testTraceReachRoutesOverTheDropAndTheSwim(stageBlenderServer, tmp_path):
  async def steps(session):
    await traceCourse(session, tmp_path)
    routed, _ = await session.call("traceReach", {"to": routeTarget, "fromEntry": "safe point"})
    fromGate, _ = await session.call("traceReach", {"to": routeTarget})
    unknown = await session.expectError("traceReach", {"to": routeTarget, "fromEntry": "harbor"})
    outside = await session.expectError("traceReach", {"to": [0, farEnd + 10]})
    return routed, fromGate, unknown, outside

  routed, fromGate, unknown, outside = stageBlenderServer.session(steps)
  assert routed.is_error is False, routed.content[-1].text
  assert [content.type for content in routed.content] == ["image", "image", "text"]
  result = json.loads(routed.content[2].text)
  # A riser players climb rises no more across a column than the steepest face they walk does, so the lane cannot tell the two apart: the
  # risers are walked, no climb of steps.
  assert riser <= stepHeight < playerReach.stepRise(playerReach.Lane(result["laneFile"]))
  assert (result["reached"], result["from"], result["target"]["label"]) == (True, "safe point", "walk")
  legs = result["legs"]
  assert result["legCount"] == len(legs) <= playerReach.routeLegsListed and "legsFile" not in result
  assert [leg["move"] for leg in legs] == ["walk", "drop", "walk", "drop", "swim", "climbOut", "walk"]
  assert [leg["label"] for leg in legs] == ["walk"] * 3 + ["swim", "swim", "walk", "walk"]
  assert all("steps" not in leg for leg in legs)
  rises = [leg["rise"] for leg in legs]
  for index, expected in ((0, 2 * riser), (1, -2 * riser), (3, waterTop), (5, -waterTop)):
    assert abs(rises[index] - expected) <= cellHeight, (index, rises[index], expected)
  # The riser block's far edge and the trench's banks are where the moves start, to a column.
  for index, y in ((1, 10), (3, trench[0]), (5, trench[1])):
    assert abs(legs[index]["start"][1] + 0.5 - y) <= 1, (index, legs[index]["start"], y)
  assert abs(legs[4]["length"] - (trench[1] - trench[0] - 2)) <= 2 and near(legs[-1]["end"], routeTarget)
  assert result["length"] == round(sum(leg["length"] for leg in legs), 1)
  # The drop off the riser block and the drop into the trench lie further apart along the route than decisiveGap: two moves, each one.
  assert [(move["number"], move["move"], move["leg"], move["lastLeg"], move["count"]) for move in result["decisive"]] == [
    (1, "drop", 1, 1, 1), (2, "drop", 3, 3, 1), (3, "climbOut", 5, 5, 1),
  ]
  assert [move["caption"] for move in result["decisive"]] == [f"1 drop {rises[1]:+g}", f"2 drop {rises[3]:+g}", f"3 climbOut {rises[5]:+g}"]
  # Three decisive moves, three frames: each stands on the route 12 before its move, the climb out's on the bank it dropped from, and
  # looks up the corridor: the climb out's at where the move ends, the scale figure standing there, the drops' 12 past their landing,
  # where the floor shows past the brink: past the first on the lower ground, past the trench's on its far bank.
  frames = result["frames"]
  assert [frame["number"] for frame in frames] == [1, 2, 3]
  for frame, move in zip(frames, result["decisive"]):
    view = frame["view"]
    assert set(view) - {"figureAt"} == {"standAt", "headingDegrees", "pitchDegrees", "bodyGroup"} and view["bodyGroup"] == "medium" and abs(view["headingDegrees"]) < 1e-6
    assert abs(view["standAt"][1] - (legs[move["leg"]]["start"][1] - playerReach.frameStandBack)) <= 1, (frame, move)
  # Legs report their points to a tenth.
  assert all(abs(value - end) <= 0.05 + 1e-9 for value, end in zip(frames[2]["view"]["figureAt"], legs[5]["end"])), (frames[2], legs[5])
  for frame, leg, floor in ((frames[0], legs[1], legs[1]["end"][2]), (frames[1], legs[3], legs[5]["end"][2])):
    assert abs(frame["view"]["figureAt"][1] - (leg["end"][1] + playerReach.frameStandBack)) <= 1 and abs(frame["view"]["figureAt"][2] - floor) <= 0.05 + 1e-9, frame
  # The drops' frames look down at where they land.
  assert frames[0]["view"]["pitchDegrees"] < 0 and frames[1]["view"]["pitchDegrees"] < 0
  sheet = Image.open(io.BytesIO(base64.b64decode(routed.content[1].data)))
  assert sheet.size == (result["frameSheet"]["width"], result["frameSheet"]["height"])
  # In plan the ribbon runs along y, across the drawing: a pixel column through it holds the walk green ribbon's width of green and the
  # casing's width less that of the casing, and the decisive moves are ringed white.
  plan = Image.open(io.BytesIO(base64.b64decode(routed.content[0].data)))
  plan.save(tmp_path / "routePlan.png")
  sheet.save(tmp_path / "routeFrames.jpg")
  frame = planFrame(result["plan"])
  x, y = (round(value) for value in frame.pixel([0, 0]))
  across = pixelsOf(plan)[y - laneDrawing.ribbonCasingWidth:y + laneDrawing.ribbonCasingWidth + 1, x]
  greenRows = round(colorShare(across, laneDrawing.labelColors[playerReach.walkLabel]) * len(across))
  casingRows = round(colorShare(across, laneDrawing.ribbonCasing) * len(across))
  assert (greenRows, casingRows) == (laneDrawing.ribbonWidth, laneDrawing.ribbonCasingWidth - laneDrawing.ribbonWidth), (greenRows, casingRows)
  assert colorShare(pixelsAround(plan, result["plan"], legs[1]["start"], 6), laneDrawing.decisiveFill) > 0.2
  # From every arrival, the gate's landing lies nearer: one walk south to the point.
  gated = json.loads(fromGate.content[-1].text)
  assert (gated["from"], [leg["move"] for leg in gated["legs"]], gated["decisive"]) == ("gate", ["walk"], [])
  assert [content.type for content in fromGate.content] == ["image", "text"]
  assert "No arrival named 'harbor'" in unknown and "'gate'" in unknown and "'safe point'" in unknown
  assert "lies outside what the zone ships" in outside


def testTraceReachNamesWhatSeparates(stageBlenderServer, tmp_path):
  targets = {
    "cliff": {"to": [5, 80]}, "boundary": {"to": [10, 58]}, "headroom": {"to": [-5, 90, 0]}, "slabTop": {"to": [-5, 90]}, "canopy": {"to": [-5, 65]},
    "water": {"to": [0, 120]}, "fromGate": {"to": [0, 0], "fromEntry": "gate"},
  }

  async def steps(session):
    await traceCourse(session, tmp_path)
    found = {}
    for name, arguments in targets.items():
      result, _ = await session.call("traceReach", arguments)
      found[name] = result
    return found

  found = stageBlenderServer.session(steps)
  results = {}
  for name, result in found.items():
    assert result.is_error is False, (name, result.content[-1].text)
    assert [content.type for content in result.content] == ["image", "image", "text"], name
    results[name] = json.loads(result.content[2].text)
    assert results[name]["reached"] is False, name
    Image.open(io.BytesIO(base64.b64decode(result.content[0].data))).save(tmp_path / f"{name}Section.png")
    Image.open(io.BytesIO(base64.b64decode(result.content[1].data))).save(tmp_path / f"{name}View.png")
  # The cliff's top rises more than the step from the ground beside it, and the cast across meets the cliff's face.
  cliff = results["cliff"]
  assert near(cliff["nearest"]["at"], [1, 80]) and cliff["nearest"]["label"] == "walk"
  assert [stop["kind"] for stop in cliff["separations"]] == ["rise"]
  rise = cliff["separations"][0]
  assert abs(rise["height"] - cliffHeight) <= cellHeight and rise["limit"] == stepHeight and abs(rise["over"] - (cliffHeight - stepHeight)) <= cellHeight
  assert rise["face"] == "cliff"
  # Outside the corridor, the wall is named.
  boundary = results["boundary"]
  assert near(boundary["nearest"]["at"], [corridorHalf - 1, 58])
  assert [(stop["kind"], stop.get("name")) for stop in boundary["separations"]] == [("boundary", "corridorWall")]
  assert abs(boundary["separations"][0]["at"][0] - corridorHalf) <= 0.01
  # Under the low slab the lane's node is the slab's inside, which no one reaches, and what keeps players out is the gap's clearance, not
  # the slab's height.
  headroom = results["headroom"]
  assert headroom["target"]["label"] == "unreached" and near(headroom["nearest"]["at"], [-1, 90])
  assert [stop["kind"] for stop in headroom["separations"]] == ["headroom"]
  assert abs(headroom["separations"][0]["clearance"] - lidGap) <= 0.02 and headroom["separations"][0]["needs"] == playerHeight
  # The slab's top lies far over that gap: what keeps players off it is the rise to it, whatever the gap under it.
  slabTop = results["slabTop"]
  assert abs(slabTop["target"]["at"][2] - wallHeight) <= cellHeight and near(slabTop["nearest"]["at"], [-1, 90])
  assert [stop["kind"] for stop in slabTop["separations"]] == ["rise"] and abs(slabTop["separations"][0]["height"] - wallHeight) <= cellHeight
  # Over the walked ground under it, the canopy's top is reached from beside it, not from under it: the nearest place lies outside its
  # footprint, a rise of its height away, and the view stands back from there, the scale figure at the nearest place but beside the line
  # of sight, on the view's right, the way to the point clear past her.
  canopyTrace = results["canopy"]
  assert abs(canopyTrace["target"]["at"][2] - canopyTop) <= cellHeight and canopyTrace["target"]["label"] == "unreached"
  assert not underCanopy(canopyTrace["nearest"]["at"]) and near(canopyTrace["nearest"]["at"], [canopy[1][0] + 1, 65])
  assert [stop["kind"] for stop in canopyTrace["separations"]] == ["rise"] and abs(canopyTrace["separations"][0]["height"] - canopyTop) <= cellHeight
  canopyView = canopyTrace["view"]["view"]
  assert not underCanopy(canopyView["standAt"]) and canopyView["standAt"][0] > canopyTrace["nearest"]["at"][0]
  share, offFloor = figureAside(canopyTrace)
  assert abs(share - buildTolerances.figureAside) <= 0.002 and offFloor <= cellHeight, canopyTrace["view"]
  # From the pit's water the far bank stands higher over the box's top than players climb out.
  water = results["water"]
  assert water["nearest"]["label"] == "swim" and near(water["nearest"]["at"], [0, pit[1] - 1])
  assert [stop["kind"] for stop in water["separations"]] == ["water"]
  stop = water["separations"][0]
  assert (stop["swimBox"], stop["boxTop"]) == ("AWT_pit", waterTop)
  assert abs(stop["bank"] - highBank) <= cellHeight and abs(stop["overClimb"] - (highBank - waterTop - stepHeight)) <= cellHeight
  # Each section runs from the reached node to the point, run on past both, with the lane: the water trace's shows the pit swum.
  section = water["section"]
  cut = json.loads(Path(section["laneCut"]).read_text())
  assert any(band["label"] == "swim" and abs(band["z"][1] - waterTop) <= cellHeight for band in cut["bands"])
  assert any(abs(mark["z"] - highBank) <= cellHeight for mark in cut["unreached"])
  # Each view looks at the point from up to 12 back on the lane: across the pit from the bank the pit was dropped into, and at the cliff
  # from the corridor's far wall, the scale figure at the cliff's foot, on the view's right.
  assert near(water["view"]["view"]["standAt"], [0, pit[1] - 1 - 12]) and abs(water["view"]["view"]["headingDegrees"]) < 1e-6
  assert near(cliff["view"]["view"]["standAt"], [-corridorHalf + 1, 80]) and abs(cliff["view"]["view"]["headingDegrees"] - 90) < 1e-6
  share, offFloor = figureAside(cliff)
  assert abs(share - buildTolerances.figureAside) <= 0.002 and offFloor <= cellHeight, cliff["view"]
  assert any(entry.startswith("doors: taken as open") for entry in cliff["notModeled"])
  # From the gate alone, players swim the trench back and climb out, but the riser block's far face rises more than a step; its section
  # draws the gate's lane, in which the block's top is walkable floor never reached.
  gated = results["fromGate"]
  assert gated["nearest"]["from"] == "gate" and near(gated["nearest"]["at"], [0, 11])
  assert [stop["kind"] for stop in gated["separations"]] == ["rise"] and abs(gated["separations"][0]["height"] - 2 * riser) <= cellHeight
  gatedCut = json.loads(Path(gated["section"]["laneCut"]).read_text())
  assert any(abs(mark["z"] - 2 * riser) <= cellHeight for mark in gatedCut["unreached"]) and not any(abs(mark["z"] - 2 * riser) <= cellHeight for mark in gatedCut["bars"])
  # Each section names its separations clear of the reached place's disc and the traced point's ring: no label's pixels lie on them.
  for name in ("cliff", "headroom"):
    traced = results[name]
    image = pixelsOf(Image.open(tmp_path / f"{name}Section.png"))
    summary = {"length": traced["section"]["length"], "bottom": traced["section"]["bottom"], "top": traced["section"]["top"]}
    x, y = (round(value) for value in sectionPixel(summary, 12, traced["nearest"]["at"][2]))
    disc = image[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3)
    assert colorShare(disc, laneDrawing.decisiveFill) > 0.5, (name, disc)


def testSeparationsAlongOneFaceAreOne():
  # A steep face crossed over three columns, a rise at its top, then a boundary met twice side by side and another boundary.
  stated = [
    {"kind": "steep", "along": 3.0, "at": [3, 0, 2], "degrees": 75.0, "steepestWalkable": 71.9},
    {"kind": "steep", "along": 4.0, "at": [4, 0, 6], "degrees": None, "why": "no face crosses the column's middle", "steepestWalkable": 71.9},
    {"kind": "steep", "along": 5.0, "at": [5, 0, 10], "degrees": 80.0, "steepestWalkable": 71.9},
    {"kind": "rise", "along": 6.0, "at": [6, 0, 16], "from": [5, 0, 10], "height": 6.0, "limit": 4.5, "over": 1.5},
    {"kind": "rise", "along": 7.0, "at": [7, 0, 23], "from": [6, 0, 16], "height": 7.0, "limit": 4.5, "over": 2.5},
    {"kind": "boundary", "along": 9.0, "at": [9, 0, 26], "name": "rim"},
    {"kind": "boundary", "along": 10.0, "at": [10, 0, 26], "name": "rim"},
    {"kind": "boundary", "along": 11.0, "at": [11, 0, 26], "name": "fence"},
  ]
  merged = playerReach.mergedSeparations(stated, 1.0, stepHeight)
  assert [(stop["kind"], stop["columns"], stop.get("name")) for stop in merged] == [("steep", 3, None), ("rise", 2, None), ("boundary", 2, "rim"), ("boundary", 1, "fence")]
  steep, rise = merged[0], merged[1]
  assert (steep["degrees"], "why" in steep, steep["at"], steep["through"], steep["alongThrough"]) == (80.0, False, [3, 0, 2], [5, 0, 10], 5.0)
  assert (rise["height"], rise["over"], rise["through"]) == (13.0, 8.5, [7, 0, 23])


def testADiagonalLineStepsThroughSideBySideColumns():
  # A line at 45 degrees from one column's center to another's passes through the grid's corners; each step it takes goes to a column
  # sharing a side with the one before, the column between coming at the corner.
  class Grid:
    origin, cellSize = numpy.zeros(3), 1.0

  cells = playerReach.sideBySide(playerReach.lineColumns(Grid, [0.5, 0.5], [3.5, 3.5]))
  assert [(x, z) for x, z, _, _ in cells] == [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2), (3, 3)]
  assert all(abs(x - before[0]) + abs(z - before[1]) == 1 for (x, z, _, _), before in zip(cells[1:], cells))


def decisiveLeg(move, length, rise=0.0):
  return {"move": move, "label": "walk", "start": [0.0, 0.0, 0.0], "end": [0.0, length, rise], "length": length, "moves": 1} | ({"steps": 1} if move == "step" else {})


def testDecisiveMovesOfOneKindCloseTogetherAreOne():
  # Three terraces climbed with less than decisiveGap of walking between, a long walk, a fourth, a slide, then drops apart.
  gap = playerReach.decisiveGap
  legs = [
    decisiveLeg("step", 1, 4), decisiveLeg("walk", gap / 2), decisiveLeg("step", 1, 4), decisiveLeg("walk", gap / 2), decisiveLeg("step", 1, 4),
    decisiveLeg("walk", 2 * gap), decisiveLeg("step", 1, 4), decisiveLeg("slide", 3, -6), decisiveLeg("sink", 0, -3), decisiveLeg("walk", gap),
    decisiveLeg("drop", 1, -8), decisiveLeg("enterWater", 1, -1), decisiveLeg("swim", 5), decisiveLeg("climbOut", 1, 1), decisiveLeg("teleport", 0, 10),
  ]
  decisive = playerReach.decisiveLegs(legs)
  assert [(move["number"], move["move"], move["first"], move["last"], move["count"]) for move in decisive] == [
    (1, "step", 0, 4, 3), (2, "step", 6, 6, 1), (3, "slide", 7, 7, 1), (4, "drop", 10, 10, 1), (5, "enterWater", 11, 11, 1),
    (6, "climbOut", 13, 13, 1), (7, "teleport", 14, 14, 1),
  ]
  assert playerReach.decisiveCaption(legs, decisive[0]) == "1 climb of 3 steps +12 in 3 runs"
  assert [playerReach.decisiveCaption(legs, move) for move in decisive[1:3]] == ["2 step +4", "3 slide -6"]


def mapPixel(description, point):
  """Where a zone point falls in a map view's render, from its center, width, height, and units per pixel."""
  center, size = description["mapCenter"], description["unitsPerPixel"]
  return round((center[1] + description["mapWidth"] / 2 - point[1]) / size - 0.5), round((center[0] + description["mapHeight"] / 2 - point[0]) / size - 0.5)


def tintShare(tinted, bare, x, y, label):
  """How far, channel by channel, a pixel moved from a render without the lane toward a label's color, and how far that may stray from
  the exact share by the eight bits each render is stored in."""
  color = numpy.array(laneDrawing.labelColors[label], dtype=float)
  apart = color - bare[y, x]
  return (tinted[y, x] - bare[y, x]) / apart, 1.0 / numpy.abs(apart)


def projected(description, point):
  """Where a zone point falls in a perspective view's render, from its eye, forward, vertical field of view, width, and height."""
  forward = numpy.array(description["forward"], dtype=float)
  right = numpy.cross(forward, [0.0, 0.0, 1.0])
  right /= numpy.linalg.norm(right)
  up = numpy.cross(right, forward)
  offset = numpy.array(point, dtype=float) - numpy.array(description["eye"], dtype=float)
  scale = description["height"] / 2 / math.tan(math.radians(description["verticalFieldOfViewDegrees"]) / 2)
  depth = offset @ forward
  return round(description["width"] / 2 + offset @ right / depth * scale - 0.5), round(description["height"] / 2 - offset @ up / depth * scale - 0.5)


def testRenderViewTintsReachedFloor(stageBlenderServer, tmp_path):
  overhead = {"map": {"center": [0, 92], "width": 70}}
  eyeLevel = {"standAt": [0, 4, 2 * riser], "headingDegrees": 0, "pitchDegrees": -25, "figureAt": [-3, 22]}
  # At eye level the far clip falls short of the corridor's far end, so the tint looks up only what lies within it.
  shortClip = 60.0

  async def steps(session):
    await traceCourse(session, tmp_path)
    plain = await session.expectImage("renderView", {"view": overhead})
    laned = await session.expectImage("renderView", {"view": overhead, "lane": True})
    await session.expectSuccess("setZoneProperties", {"maxClip": shortClip})
    looking = await session.expectImage("renderView", {"view": eyeLevel, "lane": True, "swimVolumes": True})
    lookingPlain = await session.expectImage("renderView", {"view": eyeLevel, "swimVolumes": True})
    # The same camera without the scale figure: the riser block's top is level at its height, so the eye stands eyeHeight over it.
    pitch = math.radians(eyeLevel["pitchDegrees"])
    eye = [eyeLevel["standAt"][0], eyeLevel["standAt"][1], eyeLevel["standAt"][2] + eyeHeight]
    target = [eye[0], eye[1] + math.cos(pitch), eye[2] + math.sin(pitch)]
    alone = await session.expectImage("renderView", {"view": {"eye": eye, "target": target}, "lane": True, "swimVolumes": True})
    return plain, laned, looking, lookingPlain, alone

  (plainImage, plain), (lanedImage, laned), (lookingImage, looking), (lookingPlainImage, lookingPlainResult), (aloneImage, _) = stageBlenderServer.session(steps)
  Image.open(io.BytesIO(lookingImage)).save(tmp_path / "laneTint.png")
  Image.open(io.BytesIO(lanedImage)).save(tmp_path / "laneTintMap.png")
  assert "laneState" not in plain and laned["laneState"]["lane"] in ("computed", "reused")
  # The lane's state comes back without the lane's terms, which the report holds.
  assert sorted(laned.inline["laneState"]) == ["bodyGroup", "designGradeDegrees", "lane", "laneFile", "seconds"] and laned["laneState"]["notModeled"] == playerReach.notModeled
  # The course's reached floors are level and its walls upright, so under the design grade of 35 nothing is tinted climb.
  assert laned["laneState"]["designGradeDegrees"] == 35.0 and laned["laneTint"]["climbShare"] == 0
  # The lane's view carries a legend band under the render, naming the labels it tints.
  lanedPicture, plainPicture = Image.open(io.BytesIO(lanedImage)), Image.open(io.BytesIO(plainImage))
  assert lanedPicture.width == plainPicture.width and lanedPicture.height > plainPicture.height == laned["height"]
  assert laned["laneTint"]["labels"] == [playerReach.walkLabel, playerReach.swimLabel]
  bare, tinted = pixelsOf(plainPicture).astype(float), pixelsOf(lanedPicture).astype(float)

  def unchanged(point):
    x, y = mapPixel(laned, point)
    return numpy.abs(tinted[y, x] - bare[y, x]).max()

  # Open ground walked and the pit's bed swum move toward their labels' colors by the tint's share in every channel; the canopy's top
  # over walked ground hides it, and the cliff's top and the high bank, never reached, are as they were.
  for point, label in (([0, 65], playerReach.walkLabel), ([0, 106], playerReach.swimLabel)):
    share, stray = tintShare(tinted, bare, *mapPixel(laned, point), label)
    assert (numpy.abs(share - laneDrawing.tintAlpha) <= stray).all(), (point, share, stray)
  assert unchanged([-6, 65]) <= 1 and unchanged([5, 80]) <= 1 and unchanged([0, 120]) <= 1
  # At eye level only the reached floor within the far clip and the field is looked up: no more columns than lie within the clip.
  tint = looking["laneTint"]
  assert tint["farClip"] == shortClip
  nodes = playerReach.Nodes(playerReach.Lane(looking["laneState"]["laneFile"]))
  dry = numpy.flatnonzero(nodes.reached & ~nodes.wet)
  planX, planY = nodes.plan(dry)
  eye = numpy.array(lookingPlainResult["eye"])
  reach = numpy.sqrt((planX - eye[0]) ** 2 + (planY - eye[1]) ** 2 + (nodes.height[dry] - eye[2]) ** 2)
  assert 0 < tint["columns"] <= int((reach <= shortClip + math.sqrt(2) * cellSize).sum()) < len(dry)
  # The ground walked ahead, past the riser block's drop, moves toward walk green by the tint's share; the trench's far bank, a wall
  # face, does not.
  height = lookingPlainResult["height"]
  laneView = pixelsOf(Image.open(io.BytesIO(lookingImage))).astype(float)[:height]
  plainView = pixelsOf(Image.open(io.BytesIO(lookingPlainImage))).astype(float)[:height]
  share, stray = tintShare(laneView, plainView, *projected(lookingPlainResult, [2, 30, 0]), playerReach.walkLabel)
  assert (numpy.abs(share - laneDrawing.tintAlpha) <= stray).all(), (share, stray)
  x, y = projected(lookingPlainResult, [2, trench[1], -1])
  assert numpy.abs(laneView[y - 1:y + 2, x - 2:x + 3] - plainView[y - 1:y + 2, x - 2:x + 3]).max() <= 1
  # The scale figure, where the view without her differs, is drawn as without the lane, her feet among the rest: all of her but her
  # outline, whose pixels blend her with the floor behind as the render smooths her edges.
  aloneView = pixelsOf(Image.open(io.BytesIO(aloneImage))).astype(float)[:height]
  figure = numpy.abs(laneView - aloneView).max(axis=2) > 8
  for _ in range(2):
    inner = numpy.zeros_like(figure)
    inner[1:-1, 1:-1] = numpy.logical_and.reduce([figure[1 + down:height - 1 + down, 1 + across:figure.shape[1] - 1 + across] for down in (-1, 0, 1) for across in (-1, 0, 1)])
    figure = inner
  assert figure.sum() > 500 and numpy.abs(laneView - plainView).max(axis=2)[figure].max() <= 1


def testLaneTintTellsClimbFromWalkByTheDesignGrade(stageBlenderServer, tmp_path):
  # A map 40 across about testReach's climbable ramp, 2 degrees under the medium group's steepest walkable face, rising toward +x from
  # x 20 and reached walking; the level ground before it at (10, 0) is walked too.
  overhead = {"map": {"center": [20, 0], "width": 40}}
  cap = playerScale.strictest(playerScale.groupNames).steepestWalkableDegrees
  scramble = {"name": "scramble", "outline": [[15, -15], [30, -15], [30, 15], [15, 15]], "bottom": -5, "top": 20, "intent": "a scramble up the bank", "access": "play"}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "course", "size": [200, 120], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [-90, 0, 0, 90]})
    await session.expectSuccess("createPrimitive", rampArguments("climbable", medium.steepestWalkableDegrees - 2, 0))
    plain = await session.expectImage("renderView", {"view": overhead})
    zoneGrade = await session.expectImage("renderView", {"view": overhead, "lane": True})
    await session.expectSuccess("createRegion", scramble | {"gradeDegrees": cap})
    regionGrade = await session.expectImage("renderView", {"view": overhead, "lane": True})
    overCap = await session.expectError("createRegion", scramble | {"name": "tooSteep", "gradeDegrees": cap + 1})
    return plain, zoneGrade, regionGrade, overCap

  (plainImage, _), (zoneImage, zoneGrade), (regionImage, regionGrade), overCap = stageBlenderServer.session(steps)
  Image.open(io.BytesIO(zoneImage)).save(tmp_path / "climbTint.png")
  bare = pixelsOf(Image.open(io.BytesIO(plainImage))).astype(float)
  ramp, ground = [20.5, 0], [10, 0]

  def moved(image, description, point, label):
    tinted = pixelsOf(Image.open(io.BytesIO(image))).astype(float)
    return tintShare(tinted, bare, *mapPixel(description, point), label)

  # Under the zone's design grade of 35, the ramp, reached walking, is tinted climb and the level ground walk (the ground under the
  # ramp's plane, which only a smaller body fits under, is squeeze either way).
  assert zoneGrade["laneState"]["designGradeDegrees"] == buildTolerances.designGradeDegrees == 35.0
  assert zoneGrade["laneTint"]["labels"] == [playerReach.walkLabel, playerReach.squeezeLabel, laneDrawing.climbTint] and zoneGrade["laneTint"]["climbShare"] > 0
  for point, label in ((ramp, laneDrawing.climbTint), (ground, playerReach.walkLabel)):
    share, stray = moved(zoneImage, zoneGrade, point, label)
    assert (numpy.abs(share - laneDrawing.tintAlpha) <= stray).all(), (point, share, stray)
  # A region over the ramp designed to be walked at the groups' steepest face tints it walk again.
  assert regionGrade["laneTint"]["labels"] == [playerReach.walkLabel, playerReach.squeezeLabel] and regionGrade["laneTint"]["climbShare"] == 0
  share, stray = moved(regionImage, regionGrade, ramp, playerReach.walkLabel)
  assert (numpy.abs(share - laneDrawing.tintAlpha) <= stray).all(), (share, stray)
  # A grade past what every group the region is built for walks is refused, naming the cap.
  assert f"gradeDegrees is above 0 and at most {cap:.1f} (the steepest face every body group the region is built for walks, ['small', 'medium', 'large'], playerScale), got {cap + 1}" in overCap


def sectionPixel(summary, s, z):
  return planDrawing.sectionPlace(summary, planDrawing.sectionSize, planDrawing.sectionFit(summary, planDrawing.sectionSize))(s, z)


def testSectionDrawsTheLane(stageBlenderServer, tmp_path):
  start, end = [0, -35], [0, 125]
  # Across the corridor once its wall is gone: the ground's edges at both sides are leak edges.
  acrossStart, acrossEnd = [-15, 20], [15, 20]

  async def steps(session):
    await traceCourse(session, tmp_path)
    laned = await session.expectImage("renderSection", {"start": start, "end": end, "layers": ["ground", "swim", "boundaries", "lane"]})
    plain = await session.expectImage("renderSection", {"start": start, "end": end, "layers": ["ground", "swim", "boundaries"]})
    await session.expectSuccess("deleteObjects", {"names": ["corridorWall"]})
    leaking = await session.expectImage("renderSection", {"start": acrossStart, "end": acrossEnd, "layers": ["ground", "lane"]})
    return laned, plain, leaking

  (lanedImage, laned), (plainImage, plain), (leakingImage, leaking) = stageBlenderServer.session(steps)
  section = Image.open(io.BytesIO(lanedImage))
  section.save(tmp_path / "courseSection.png")
  # Both carry a legend band under the section; the lane's adds its own entries to the section's.
  plainHeight = Image.open(io.BytesIO(plainImage)).height
  assert "laneState" not in plain and plainHeight > planDrawing.sectionSize[1] and section.height > plainHeight
  cut = json.loads(Path(laned["laneState"]["laneCut"]).read_text())
  assert cut["group"] == laned["laneState"]["bodyGroup"] == "large"
  assert laned["laneState"]["counts"] == {kind: len(marks) for kind, marks in cut.items() if kind != "group"}
  # The lane's state comes back without the lane's terms, which the report holds.
  assert sorted(laned.inline["laneState"]) == ["bodyGroup", "counts", "lane", "laneCut", "seconds"]
  assert laned["laneState"]["notModeled"] == playerReach.notModeled and sorted(laned["laneState"]) == sorted([*laned.inline["laneState"], "constants", "limits", "notModeled"])

  def bar(z, label, s0, s1):
    """The bar at height z of a label spanning s0 to s1 along the line, columns centered on whole units."""
    matches = [mark for mark in cut["bars"] if mark["label"] == label and abs(mark["z"] - z) <= cellHeight and abs(mark["s"][0] - s0) <= 1 and abs(mark["s"][1] - s1) <= 1]
    return matches

  along = lambda y: y - start[1]
  assert bar(0, "walk", 0, along(-10.5))
  assert bar(riser, "walk", along(-10.5), along(riserB - 0.5)) and bar(2 * riser, "walk", along(riserB - 0.5), along(10.5))
  assert bar(0, "walk", along(10.5), along(trench[0] + 0.5)) and bar(0, "walk", along(trench[1] - 0.5), along(pit[0] + 0.5))
  # The trench and the pit are swum to their beds, which a sink reaches, so they are floor in a swim box.
  assert bar(bed, "swim", along(trench[0] + 0.5), along(trench[1] - 0.5)) and bar(bed, "swim", along(pit[0] + 0.5), along(pit[1] - 0.5))
  assert [band["label"] for band in cut["bands"]] == ["swim", "swim"]
  for band, (first, last) in zip(cut["bands"], (trench, pit)):
    assert abs(band["z"][0] - bed) <= cellHeight and abs(band["z"][1] - waterTop) <= cellHeight
    assert abs(band["s"][0] - along(first + 0.5)) <= 1 and abs(band["s"][1] - along(last - 0.5)) <= 1
  # Past the pit the high bank is walkable floor never reached, and the walled corridor leaks nowhere.
  highFloors = [mark for mark in cut["unreached"] if abs(mark["z"] - highBank) <= cellHeight]
  assert len(highFloors) == 1 and abs(highFloors[0]["s"][0] - along(pit[1] - 0.5)) <= 1 and abs(highFloors[0]["s"][1] - along(end[1])) <= 1
  assert cut["leaks"] == []
  # The bars stand just over their floors in walk green, the ground's line showing under them, where the section without the lane draws
  # none; the pit's water is a blue band from the swim box's one side to its other.
  pixels = pixelsOf(section)
  plainPixels = pixelsOf(Image.open(io.BytesIO(plainImage)))
  for y, ground in ((0, 2 * riser), (-20, 0)):
    x, floor = (round(value) for value in sectionPixel(laned, along(y), bar(ground, "walk", 0, along(-10.5))[0]["z"] if y < -10 else bar(ground, "walk", along(riserB - 0.5), along(10.5))[0]["z"]))
    bottom = floor - laneDrawing.sectionBarLift
    inside = (slice(bottom - laneDrawing.sectionBarHeight + 1, bottom - 1), slice(x - 3, x + 4))
    assert colorShare(pixels[inside].reshape(-1, 3), laneDrawing.labelColors[playerReach.walkLabel]) == 1.0
    assert colorShare(plainPixels[inside].reshape(-1, 3), laneDrawing.labelColors[playerReach.walkLabel]) == 0
    _, line = (round(value) for value in sectionPixel(laned, along(y), ground))
    assert colorShare(pixels[line, x - 3:x + 4], planDrawing.groundColor) == 1.0
  x, y = (round(value) for value in sectionPixel(laned, along((pit[0] + pit[1]) / 2), (bed + waterTop) / 2))
  assert colorShare(pixels[y, x][None], laneDrawing.bandSwatch(laneDrawing.labelColors[playerReach.swimLabel])) == 1.0
  for s in (along(pit[0]) + 0.5, along(pit[1]) - 0.5):
    edge, _ = (round(value) for value in sectionPixel(laned, s, 0))
    assert colorShare(pixels[y, edge][None], laneDrawing.bandSwatch(laneDrawing.labelColors[playerReach.swimLabel])) == 1.0, s
  # Across the open ground, each edge is a leak, ticked magenta down from the floor where the fall starts, at the ground's edge to a
  # column.
  leakCut = json.loads(Path(leaking["laneState"]["laneCut"]).read_text())
  edges = [acrossEnd[0] - groundEdge, acrossEnd[0] + groundEdge]
  assert len(leakCut["leaks"]) == 2 and all(abs(mark["z"]) <= cellHeight and abs(mark["s"] - s) <= cellSize for mark, s in zip(leakCut["leaks"], edges)), leakCut["leaks"]
  leakPixels = pixelsOf(Image.open(io.BytesIO(leakingImage)))
  for mark in leakCut["leaks"]:
    x, y = (round(value) for value in sectionPixel(leaking, mark["s"], mark["z"]))
    tick = leakPixels[y + 2:y + laneDrawing.sectionLeakTick - 1, x].reshape(-1, 3)
    assert colorShare(tick, laneDrawing.voidColor) == 1.0, mark


def testSectionAlongACaveDrawsItsWalkedFloor(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [0, -100, 2, 0]})
    return await session.expectImage("renderSection", {"cave": {"objectName": "ground", "name": "hall"}, "layers": ["ground", "caves", "lane"]})

  image, summary = stageBlenderServer.session(steps)
  Image.open(io.BytesIO(image)).save(tmp_path / "caveSection.png")
  cut = json.loads(Path(summary["laneState"]["laneCut"]).read_text())
  # Along the run's middle the floor is walked from the mouth to the room's end, every 2 units a bar within the floor's breakup of its
  # designed height (the floor given as the points where its grade turns).
  [floor] = [entry for entry in summary["caves"] if entry["run"] == "main"][0]["floor"]
  stations, heights = numpy.array(floor).T
  walked = [mark for mark in cut["bars"] if mark["label"] == "walk"]
  for middle in numpy.arange(stations[0] + 1, stations[-1], 2):
    designed = float(numpy.interp(middle, stations, heights))
    holding = [mark for mark in walked if mark["s"][0] <= middle <= mark["s"][1]]
    assert any(abs(mark["z"] - designed) <= hall["breakup"]["amplitude"] + cellHeight for mark in holding), (middle, designed, holding)


def testTraceReachOnTheReachCourse(stageBlenderServer, tmp_path):
  routes = {"tower": {"to": [90, -120, 30]}, "slideFace": {"to": probes["slide face"][0]}, "pool": {"to": [-120, 40, 2]}}
  stops = {
    "steepRamp": {"to": [110, 36]}, "pastZoneLine": {"to": [-160, 173]}, "pastDiagonal": {"to": [234, -235]}, "floating": {"to": [-255, 105]},
    "highBank": {"to": [-105, 46]},
  }

  async def steps(session):
    await reachCourse(session, tmp_path)
    # A platform beyond the walled edge with nothing between: no floor under the way to it.
    await cube(session, "floatingPlatform", [-260, 100, 0], [-250, 110, 1])
    # A raised walk up a ramp, a zone line across it leading away: past the line its far end is never reached.
    await cube(session, "raisedWalk", [-200, 170, 0], [-150, 176, raisedHeight])
    await rampAlongY(session, "raisedRamp", -195, 170 - raisedHeight / math.tan(math.radians(30)), raisedHeight, 30)
    await session.expectSuccess("placeZoneLine", {"number": 6, "label": "walk", "minimum": [-170, 168, raisedHeight - 5], "maximum": [-166, 178, raisedHeight + 20], "target": otherZone})
    found = {}
    for name, arguments in (routes | stops).items():
      result, _ = await session.call("traceReach", arguments)
      found[name] = result
    ford = await session.expectImage("renderSection", {"start": [-138, -40], "end": [-82, -40], "layers": ["ground", "swim", "lane"], "bodyGroup": "medium"})
    return found, ford

  found, (fordImage, ford) = stageBlenderServer.session(steps)
  results = {}
  for name, result in found.items():
    assert result.is_error is False, (name, result.content[-1].text)
    results[name] = json.loads(result.content[-1].text)
    for index, content in enumerate(result.content[:-1]):
      Image.open(io.BytesIO(base64.b64decode(content.data))).save(tmp_path / f"{name}-{index}.{'jpg' if content.mime_type == 'image/jpeg' else 'png'}")
  # A zone line leading home carries players onto the tower, a step off the platform slides them down its steep face, and walking into
  # the pool enters its water: each a decisive move of its own.
  for name, move in (("tower", "teleport"), ("slideFace", "slide"), ("pool", "enterWater")):
    route = results[name]
    assert route["reached"] is True, name
    assert move in [leg["move"] for leg in route["legs"]] and [decisive["move"] for decisive in route["decisive"]][-1:] == [move], (name, route["decisive"])
  # The steep ramp's face, steeper than walkable, rises more across a column than the group steps: the lane meets it as a rise over the
  # step (a face just over the steepest walkable already rises past the step across a column, so a lane at this cell size has no
  # steep face short of one).
  steep = results["steepRamp"]["separations"]
  assert cellSize * math.tan(math.radians(steepDegrees)) > stepHeight
  assert [(stop["kind"], stop["limit"]) for stop in steep] == [("rise", stepHeight)] and steep[0]["height"] > stepHeight, steep
  # Past the raised walk's zone line, the nearest place is short of the line's box, players in it being gone, and the box separates them.
  pastLine = results["pastZoneLine"]
  assert pastLine["nearest"]["at"][0] < -170 and abs(pastLine["nearest"]["at"][2] - raisedHeight) <= cellHeight, pastLine["nearest"]
  assert [(stop["kind"], stop["zoneLine"]) for stop in pastLine["separations"]] == [("zoneLine", "ATP_6_walk")], pastLine["separations"]
  # Straight at 45 degrees through the diagonal wall's corners, the trace steps through the wall's own columns and meets it.
  diagonal = results["pastDiagonal"]
  assert [(stop["kind"], stop.get("name")) for stop in diagonal["separations"]] == [("boundary", "diagonal")], diagonal["separations"]
  # Over the walled edge to the floating platform: the wall, then the columns with nothing at all.
  floating = results["floating"]
  assert [(stop["kind"], stop.get("name")) for stop in floating["separations"]] == [("boundary", "edgeLowC"), ("noFloor", None)], floating["separations"]
  # The pool's high bank rises more than the step over the ground between the banks; its view stands back on the low bank's top (y 30
  # to 38), the level the nearest place lies on, never dropped behind the low bank, whose face would fill the view.
  highBank = results["highBank"]
  assert [(stop["kind"], stop["face"]) for stop in highBank["separations"]] == [("rise", "bankHigh")], highBank["separations"]
  standAt, nearest = highBank["view"]["view"]["standAt"], highBank["nearest"]["at"]
  assert abs(standAt[2] - nearest[2]) <= cellHeight and 30 <= standAt[1] < nearest[1], (standAt, nearest)
  # The nearest place's column middle lies on the low bank's north edge (y 38); the scale figure stands on the bank's top short of it
  # and beside the line of sight, on the view's right, so the ditch and the high bank's face show past her.
  share, offFloor = figureAside(highBank)
  assert abs(share - buildTolerances.figureAside) <= 0.002 and offFloor <= cellHeight and highBank["view"]["figure"][1] < nearest[1], (highBank["view"], nearest)
  # Across the ford (its box from x -130, 8 along the line, to -90; the shelf to -110, 28 along) the shelf's floor is walked under water
  # too shallow to swim in, shown by its bar alone as the plan shows it; the deep water beside it is swum, its band and floor swim blue.
  cut = json.loads(Path(ford["laneState"]["laneCut"]).read_text())
  assert [(band["s"], band["label"]) for band in cut["bands"]] == [([28.5, 48.5], "swim")], cut["bands"]
  assert [(bar["s"], bar["label"]) for bar in cut["bars"]] == [([0.0, 7.5], "walk"), ([7.5, 28.5], "walk"), ([28.5, 48.5], "swim"), ([48.5, 56.0], "walk")], cut["bars"]
  # The deep water's floor runs half a column past the box's far edge in the cut, whose columns are centered on whole units; drawn, its
  # bar ends at the box's edge, as its band does.
  fordSection = Image.open(io.BytesIO(fordImage))
  fordSection.save(tmp_path / "fordSection.png")
  x, y = (round(value) for value in sectionPixel(ford, 48, cut["bars"][2]["z"]))
  row = y - laneDrawing.sectionBarLift - laneDrawing.sectionBarHeight // 2
  inBox, pastBox = (pixelsOf(fordSection)[row, x + offset][None] for offset in (-3, 3))
  swimColor = laneDrawing.labelColors[playerReach.swimLabel]
  assert (colorShare(inBox, swimColor), colorShare(pastBox, swimColor)) == (1.0, 0.0), (inBox, pastBox)
