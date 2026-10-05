import json
import math

import numpy

from conftest import writePNG
from testCaves import caveCanyon, checkCave, borderEdges

caveMaterials = {"wallMaterial": "caveRock", "floorMaterial": "caveFloor", "worldUnitsPerRepeat": 48}
breakup = {"featureSize": 40, "amplitude": 5, "seed": 11}
# Level from the graded approach into the cliff, then point 2 without a height (the even grade between 2 and 30), segment 3 climbing
# at a chosen 10 degrees and segment 4 falling at 5, setting the blind end's height.
graded = {
  "objectName": "ground", "name": "graded", "path": [[0, -60, 2], [0, 10, 2], [0, 110], [0, 180, 30], [0, 240], [0, 300]], "grades": [None, None, None, 10, -5],
  "widths": [40] * 6, "heights": [40] * 6, "breakup": breakup,
} | caveMaterials
gradedFloors = [2.0, 2.0, 2 + 28 * 100 / 170, 30.0, 30 + math.tan(math.radians(10)) * 60]
gradedFloors.append(gradedFloors[4] - math.tan(math.radians(5)) * 60)
# Level into the cliff, then a climb at the even grade to a landing where it turns east, level at 30 to its blind end. The bend turns
# on an arc of radius 40 round [40, 100], from [0, 100] to [40, 140].
landed = {
  "objectName": "ground", "name": "landed", "path": [[0, -60, 2], [0, 10, 2], [0, 140, 30], [80, 140, 30]], "landings": [2],
  "widths": [40] * 4, "heights": [40] * 4, "breakup": breakup,
} | caveMaterials
# The cut floor's height under each point, cast down from 5 over the given height.
floorsUnder = r"""
import mathutils
depsgraph = bpy.context.evaluated_depsgraph_get()
result = []
for x, y, z in points:
  hit, location, _, _, _, _ = bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((x, y, z + 5)), mathutils.Vector((0, 0, -1)))
  result.append(location.z if hit else None)
"""


def floorHeights(points):
  return f"points = {json.dumps([list(map(float, point)) for point in points])}\n" + floorsUnder


# A straight cave from the approach into a room 120 wide and 70 tall, level at 2: point 2 at along 120, 3 at 150, 4 at 310. Along it
# is y + 60, and across it (positive to the right looking along it) is x.
room = {
  "objectName": "ground", "name": "room", "path": [[0, -60, 2], [0, 10, 2], [0, 60, 2], [0, 90, 2], [0, 250, 2]],
  "widths": [40, 40, 40, 120, 120], "heights": [45, 45, 45, 70, 70], "breakup": breakup,
} | caveMaterials
way = {"kind": "level", "name": "way", "run": "main", "from": 160, "to": 300, "across": [-10, 14], "material": "pathStone"}
pad = {"kind": "pad", "name": "dais", "run": "main", "from": 200, "to": 260, "across": [20, 50], "rise": 3, "edge": 0.5}
rubble = {"kind": "rough", "name": "rubble", "run": "main", "outline": [[10, 120], [70, 120], [70, 220], [10, 220]], "rise": 6, "edge": 6, "breakup": {"featureSize": 32, "amplitude": 3, "seed": 3}}
# Each lining vertex of a cave, shown: [x, y, z].
readLiningOf = r"""
import numpy
import bridgeMeshAccess
ground = bpy.data.objects['ground']
mask = bridgeMeshAccess.evaluateSelector({'cave': name}, ground, 'vertices')
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
result = shown[mask].tolist()
"""
# Each lining face's middle and normal, and the lining's integrity problems as the cave checks find them.
readLiningFaces = r"""
import numpy
import bridgeCaves, bridgeMeshAccess
ground = bpy.data.objects['ground']
mask = bridgeMeshAccess.evaluateSelector({'cave': name}, ground, 'faces')
centers, normals, _ = bridgeMeshAccess.readFaceArrays(ground)
result = {'centers': centers[mask].tolist(), 'normals': normals[mask].tolist(), 'problems': bridgeCaves.integrityProblems(ground)}
"""


def lining(name):
  return f"name = {name!r}\n" + readLiningOf


def pathMaterial(session, tmp_path):
  return session.expectSuccess("createMaterial", {"name": "pathStone", "diffuseTexture": str(writePNG(tmp_path / "pathStone.png", 4, 4, (150, 140, 120, 255)))})


def testALevelStrokeHoldsTheRunsFloorAndItsEdgesAreCut(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await pathMaterial(session, tmp_path)
    cut = await session.expectSuccess("cutCave", room | {"floor": [way, rubble | {"outline": [[-70, 110], [70, 110], [70, 250], [-70, 250]]}]})
    vertices = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    return cut, vertices

  cut, vertices = stageBlenderServer.session(steps)
  vertices = numpy.array(vertices)
  along, across = vertices[:, 1] + 60, vertices[:, 0]
  inside = (along >= 160 - 1e-4) & (along <= 300 + 1e-4) & (across >= -10 - 1e-4) & (across <= 14 + 1e-4) & (vertices[:, 2] < 10)
  # Every floor vertex inside the way stands exactly at the run's floor, though rubble is painted all round it.
  assert inside.sum() >= 3 * 9 and numpy.abs(vertices[inside, 2] - 2).max() <= 1e-4
  # Its ends are rows of the tube and its sides floor points in every row between them: lines of vertices exactly there.
  floorInRange = (vertices[:, 2] < 2 + 1e-4) & (along >= 160 - 1e-4) & (along <= 300 + 1e-4)
  for offset in (-10, 14):
    onSide = floorInRange & (numpy.abs(across - offset) <= 1e-4)
    assert onSide.sum() >= (300 - 160) / 16
  for end in (160, 300):
    onEnd = (numpy.abs(along - end) <= 1e-4) & (across >= -10 - 1e-4) & (across <= 14 + 1e-4) & (vertices[:, 2] < 2 + 1e-4)
    assert onEnd.sum() >= 3
  # Outside it the rubble rises over the floor.
  assert vertices[(along > 180) & (along < 280) & (numpy.abs(across) < 50) & ~inside, 2].max() > 2 + 3
  assert [(stroke["name"], stroke["kind"]) for stroke in cut["floorStrokes"]] == [("way", "level"), ("rubble", "rough")]


def testAPadIsLevelAtItsTopWithStraightEdges(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", room | {"floor": [pad]})
    vertices = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    return cut, vertices

  cut, vertices = stageBlenderServer.session(steps)
  vertices = numpy.array(vertices)
  along, across, heights = vertices[:, 1] + 60, vertices[:, 0], vertices[:, 2]
  floor = heights < 2 + 3 + 1e-3
  top = floor & (along >= 200 - 1e-4) & (along <= 260 + 1e-4) & (across >= 20 - 1e-4) & (across <= 50 + 1e-4)
  assert cut["floorStrokes"][0]["top"] == 5.0
  assert top.sum() >= 3 * 4 and numpy.abs(heights[top] - 5).max() <= 1e-4
  # Its riser runs between its side and the side's foot half a unit out: floor points at both, the top at the side and the floor at the foot.
  rows = floor & (along > 200 + 1e-3) & (along < 260 - 1e-3)
  for side, foot in ((20, 19.5), (50, 50.5)):
    assert (rows & (numpy.abs(across - side) <= 1e-4) & (numpy.abs(heights - 5) <= 1e-4)).sum() >= 3
    assert (rows & (numpy.abs(across - foot) <= 1e-4) & (numpy.abs(heights - 2) <= 1e-4)).sum() >= 3
  # Beyond its foot, the floor is the run's.
  beyond = floor & (along > 150) & (along < 310) & (numpy.abs(across) < 59.5) & ((across < 19.5 - 1e-3) | (across > 50.5 + 1e-3) | (along < 199.5 - 1e-3) | (along > 260.5 + 1e-3))
  assert numpy.abs(heights[beyond] - 2).max() <= 1e-4


def testRoughGroundStaysInsideItsOutlineAndBanksWithoutFolding(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await pathMaterial(session, tmp_path)
    cut = await session.expectSuccess("cutCave", room | {"breakup": None, "floor": [way, rubble]})
    vertices = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    faces = (await session.expectSuccess("runPython", {"code": "name = 'room'\n" + readLiningFaces}))["result"]
    return cut, vertices, faces

  cut, vertices, faces = stageBlenderServer.session(steps)
  vertices = numpy.array(vertices)
  along, across, heights = vertices[:, 1] + 60, vertices[:, 0], vertices[:, 2]
  floor = heights < 2 + 2 * 6 + 1e-3
  points = vertices[:, :2]
  # The outline's own distance: inside it relief is at most its rise but where it banks up the wall foot (60 across), at most twice it
  # there; beyond its edge none at all; inside the way none at all.
  insideOutline = (points[:, 0] >= 10) & (points[:, 0] <= 70) & (points[:, 1] >= 120) & (points[:, 1] <= 220)
  dx = numpy.maximum(numpy.maximum(10 - points[:, 0], points[:, 0] - 70), 0)
  dy = numpy.maximum(numpy.maximum(120 - points[:, 1], points[:, 1] - 220), 0)
  beyond = floor & (numpy.hypot(dx, dy) > 6 + 1e-3) & (along > 150) & (along < 310) & (numpy.abs(across) < 59.5)
  assert beyond.sum() > 50 and numpy.abs(heights[beyond] - 2).max() <= 1e-4
  awayFromWall = floor & insideOutline & (across < 60 - 2 * 6)
  assert (heights[awayFromWall] - 2).max() <= 6 + 1e-4 and (heights[awayFromWall] - 2).max() > 3
  atWall = floor & insideOutline & (numpy.abs(across - 60) <= 1e-3)
  assert numpy.abs(heights[atWall] - 2 - 12).max() <= 1e-3
  inWay = floor & (along >= 160) & (along <= 300) & (across >= -10) & (across <= 14)
  assert numpy.abs(heights[inWay] - 2).max() <= 1e-4
  # The wall rising from the bank never folds: no lining face of the room (short of its blind end's dome, which closes over its floor)
  # below where its walls stop rising straight faces down, and the cave's own checks find nothing wrong.
  centers, normals = numpy.array(faces["centers"]), numpy.array(faces["normals"])
  low = (centers[:, 2] < 2 + 0.35 * 70) & (centers[:, 1] > 90) & (centers[:, 1] < 250)
  assert normals[low, 2].min() >= -1e-3 and faces["problems"] == []


def testFloorStrokesComeBackOnEveryCut(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await pathMaterial(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"floor": [way, pad]})
    edited = await session.expectSuccess("editCave", {"objectName": "ground", "name": "room", "changes": {
      "widths": [40, 40, 40, 128, 128], "breakup": {"featureSize": 30, "amplitude": 4, "seed": 2},
      "floor": {"dais": {"rise": 4}, "rubble": rubble},
    }})
    vertices = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return edited, vertices, detail

  edited, vertices, detail = stageBlenderServer.session(steps)
  vertices = numpy.array(vertices)
  along, across, heights = vertices[:, 1] + 60, vertices[:, 0], vertices[:, 2]
  inWay = (along >= 160) & (along <= 300) & (across >= -10) & (across <= 14) & (heights < 10)
  onTop = (along >= 200) & (along <= 260) & (across >= 20) & (across <= 50) & (heights < 10)
  assert numpy.abs(heights[inWay] - 2).max() <= 1e-4 and numpy.abs(heights[onTop] - 6).max() <= 1e-4
  assert [stroke["name"] for stroke in edited["cut"]["floorStrokes"]] == ["way", "dais", "rubble"]
  assert detail["caves"][0]["stale"] is False


def testFloorStrokeRefusals(stageBlenderServer, tmp_path):
  trim = {"fromFloor": 4, "height": 4, "material": "caveFloor", "worldUnitsPerRepeat": 4}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await pathMaterial(session, tmp_path)
    outside = await session.expectError("cutCave", room | {"floor": [way | {"from": 40, "to": 100, "across": [-30, 10]}]})
    fine = await session.expectError("cutCave", room | {"floor": [rubble | {"breakup": {"featureSize": 20, "amplitude": 3, "seed": 3}}]})
    banded = await session.expectError("cutCave", room | {"trimBands": [trim], "floor": [rubble]})
    twice = await session.expectError("cutCave", room | {"floor": [way, pad | {"name": "way"}]})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return outside, fine, banded, twice, detail

  outside, fine, banded, twice, detail = stageBlenderServer.session(steps)
  assert "Floor stroke 'way' reaches 10.0 past its run's left wall at 40.0 along it, where the floor is 40.0 wide" in outside
  assert "featureSize 20 is under twice the cave's edgeLength (16)" in fine and "edgeLength of 10 or less" in fine
  assert "The cave's floor strokes raise its right wall's foot" in banded and "within a step of the lowest trim band (4.0 over the floor)" in banded
  assert "Two floor strokes are named 'way'" in twice
  assert detail["caves"] == []


def testAPlotOnAPadMeasuresThePad(stageBlenderServer, tmp_path):
  housing = {"role": "featured", "intent": "plots in caves", "placement": "world", "plotBudget": {"player": 1, "guild": 0}}
  plotPad = {"kind": "pad", "name": "plotPad", "run": "main", "from": 190, "to": 290, "across": [-50, 50], "rise": 3, "edge": 4}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"floor": [plotPad]})
    await session.expectSuccess("setZoneHousing", housing)
    placed = await session.expectSuccess("placePlot", {"address": "1 Pad", "center": [0, 180], "facingDegrees": 180, "size": [80, 80], "height": 5})
    assessed = await session.expectSuccess("assessPlot", {"address": "1 Pad"})
    walk = await session.expectSuccess("walkRoute", {"path": [[0, -60, 2], [0, 120, 2], [0, 180, 5]]})
    return placed, assessed, walk

  placed, assessed, walk = stageBlenderServer.session(steps)
  stroke = {"object": "ground", "cave": "room", "run": "main", "stroke": "plotPad", "kind": "pad", "height": 5.0}
  assert placed["caveFloor"] == stroke and assessed["caveFloor"] == stroke
  assert abs(assessed["under"]["lowest"] - 5) <= 0.01 and abs(assessed["under"]["highest"] - 5) <= 0.01
  assert walk["walkable"] is True and walk["problems"] == []


def testPointsWithoutHeightsTakeTheEvenGradeAndAGradedSegmentSetsItsEnd(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", graded)
    floors = cut["runs"]["main"]["floors"]
    stations = [[0, y, floor] for (_, y, *_), floor in zip(graded["path"], floors)]
    measured = (await session.expectSuccess("runPython", {"code": floorHeights(stations)}))["result"]
    return cut, measured

  cut, measured = stageBlenderServer.session(steps)
  assert numpy.allclose(cut["runs"]["main"]["floors"], gradedFloors, atol=0.01)
  # The cut floor stands at each worked-out height, a ray cast down on each point.
  assert numpy.allclose(measured, gradedFloors, atol=0.01)
  grades = [segment["gradeDegrees"] for segment in cut["runs"]["main"]["segments"]]
  even = math.degrees(math.atan2(28, 170))
  assert numpy.allclose(grades, [0.0, even, even, 10.0, -5.0], atol=0.01)
  assert [(end["run"], end["end"], end["kind"]) for end in cut["ends"]] == [("main", "start", "open"), ("main", "end", "blind")]


def testALandingTurnsLevelAndItsStraightsTakeTheGrade(stageBlenderServer, tmp_path):
  arc = [[40 + 40 * math.cos(math.radians(angle)), 100 + 40 * math.sin(math.radians(angle)), 30] for angle in range(175, 90, -10)]

  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", landed)
    measured = (await session.expectSuccess("runPython", {"code": floorHeights(arc)}))["result"]
    climb = (await session.expectSuccess("runPython", {"code": floorHeights([[0, 10, 2], [0, 55, 16], [0, 100, 30]])}))["result"]
    walk = await session.expectSuccess("walkRoute", {"cave": {"objectName": "ground", "name": "landed"}})
    return cut, measured, climb, walk

  cut, measured, climb, walk = stageBlenderServer.session(steps)
  main = cut["runs"]["main"]
  assert main["landings"] == [{"point": 2, "floor": 30.0, "arc": [160.0, round(160 + 40 * math.pi / 2, 1)]}]
  # The floor is level at the landing's height all round its arc, and climbs evenly to the arc's start, the arc left out of the climb.
  assert all(height is not None for height in measured) and numpy.abs(numpy.array(measured) - 30).max() <= 0.01
  assert numpy.allclose(climb, [2, 16, 30], atol=0.01)
  assert numpy.isclose(main["segments"][1]["gradeDegrees"], math.degrees(math.atan2(28, 90)), atol=0.01)
  assert main["segments"][1]["run"] == 90.0 and main["segments"][2]["run"] == 40.0
  # Walked along the run from its mouth to its blind end, never steeper than the floor's maximum.
  assert walk["walkable"] is True and walk["problems"] == [] and walk["steepestGrade"]["degrees"] <= 30


def testGradeRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    twoWays = await session.expectError("cutCave", graded | {"grades": [0, 5, None, 10, -5], "path": [[0, -60, 2], [0, 10], [0, 110, 10], [0, 180, 30], [0, 240], [0, 300]]})
    steep = await session.expectError("cutCave", graded | {"grades": [None, None, None, 35, -5]})
    atEnd = await session.expectError("cutCave", landed | {"landings": [3]})
    straight = await session.expectError("cutCave", graded | {"landings": [2]})
    unset = await session.expectError("cutCave", graded | {"path": [[0, -60, 2], [0, 10, 2], [0, 110], [0, 180], [0, 240, 40], [0, 300]], "grades": [None, None, 10, None, -5]})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return twoWays, steep, atEnd, straight, unset, detail

  twoWays, steep, atEnd, straight, unset, detail = stageBlenderServer.session(steps)
  assert "The cave's point 2 has its height set two ways: given as 10, and by segment 1's grade of 5 degrees" in twoWays
  rise = math.tan(math.radians(35)) * 60
  assert f"rises {rise:.1f} from point 3 to point 4 over a run of 60.0" in steep and f"needs a run of {rise / math.tan(math.radians(30)):.1f}" in steep
  assert "The cave's landing at point 3 is at an end" in atEnd
  assert "The cave's path does not bend at point 2" in straight
  assert "segment 2 is graded 10 degrees from point 2, which has no height yet" in unset
  assert detail["caves"] == []


def testASectionUnrollsAPathAndACaveRun(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    result, _ = await session.call("cutCave", landed)
    bent = await session.expectImage("renderSection", {"path": [[-100, -120], [0, -120], [0, -80]], "bottom": -20, "top": 40})
    run = await session.expectImage("renderSection", {"cave": {"objectName": "ground", "name": "landed"}})
    unknown = await session.expectError("renderSection", {"cave": {"objectName": "ground", "name": "landed", "run": "loft"}})
    twice = await session.expectError("renderSection", {"path": [[0, 0], [0, 0], [10, 0]]})
    return result, bent, run, unknown, twice

  result, (_, bent), (_, run), unknown, twice = stageBlenderServer.session(steps)
  # The cut returns its profile, one panel for its one run.
  assert [content.type for content in result.content] == ["image", "text"]
  assert json.loads(result.content[1].text)["profile"]["panels"] == ["landed: main"]
  # An L of two legs is as long as both, and the ground is one line through the bend: the pieces meeting at it meet at one height.
  assert bent["length"] == 140.0 and [bend["s"] for bend in bent["bends"]] == [0.0, 100.0, 140.0]
  [join] = bent["groundAtJoins"]
  assert join["s"] == 100.0 and len(join["before"]) == 1 and numpy.allclose(join["before"], join["after"], atol=1e-3)
  # The run's own section shows its floor at its stations' heights, each station marked at its distance along the unrolled run.
  floors = numpy.array([segment for entry in run["caves"] if entry["run"] == "main" for segment in entry["floor"]])
  bends = {bend["label"]: bend["s"] for bend in run["bends"]}
  for index, height in enumerate([2.0, 2.0, 30.0, 30.0]):
    s = bends[str(index)]
    touching = floors[(numpy.minimum(floors[:, 0], floors[:, 2]) <= s + 1e-6) & (numpy.maximum(floors[:, 0], floors[:, 2]) >= s - 1e-6)]
    at = [z0 + (z1 - z0) * (s - s0) / (s1 - s0) if s1 != s0 else z0 for s0, z0, s1, z1 in touching]
    assert at and max(abs(value - height) for value in at) <= 0.01
  assert "Cave 'landed' of 'ground' has no run 'loft'; its runs are ['main']" in unknown
  assert "The section's points 0 and 1 stand at one place" in twice
