import json
import math

import numpy

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
