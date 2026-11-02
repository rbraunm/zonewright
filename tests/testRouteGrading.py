import math
import re
import sys
from pathlib import Path

import numpy
import pytest

from conftest import writePNG, walkOf
from testModelsAndDressing import readShapedMesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import playerScale

# The medium body group, which holds the walked human male, is the one these tests size their courses by.
medium = playerScale.groups["medium"]
steepestWalkableDegrees = medium.steepestWalkableDegrees
# A whole degree past the steepest face players walk, which a grade limit may not reach.
pastWalkableDegrees = math.floor(steepestWalkableDegrees) + 1

# A three-leg switchback climbing 100 up caveCanyon's 45-degree hillside to the entrance of a plot on the rim: legs 24 degrees off the
# contour, two hairpins, its ends fixed (an end left free takes the ground there each time it is graded, and moves with it).
switchback = {
  "objectName": "ground", "name": "switchback", "width": 20,
  "points": [[210, -95, 20], [296.8, -56.4], [187.2, -7.6], [274.0, 31.0, 120], [270, 75, 120]],
}
plot = {"address": "1 Rim Walk", "center": [270, 115], "facingDegrees": 180, "size": [80, 80], "height": 120}
# A stair cut up caveCanyon's cliff on the slant, its batters near vertical: grading it slides vertices onto its edges where the cliff's
# own were slid onto its breaks, so the cells there are skewed, and the diagonal each is split on decides what folds.
cleft = {
  "objectName": "ground", "name": "cleft", "points": [[-120, -45, 0], [0, -10], [130, 25, 120]], "width": 18, "maximumGradeDegrees": 30,
  "cutBatterDegrees": 85, "fillBatterDegrees": 70,
}
readGround = "objectName = 'ground'" + readShapedMesh
readDefinitions = "import bridgePasses\nresult = bridgePasses.passDefinitions(bpy.data.objects['ground'])"


async def caveCanyon(session, tmp_path):
  """caveCanyon: a 640 x 640 grid of 8-unit cells; a cliff 120 high at about 80 degrees along y = 0, the low river side to the south; a
  45-degree hillside 160 long at its east end; warp and roughen over it all; grass, and rock painted on slopes of 40 degrees or more."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  for name, color in (("grass", (90, 120, 60, 255)), ("rock", (110, 105, 100, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [640, 640], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "cliff"})
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-400, 0], [180, 0], [180, 400], [-400, 400]], "base": 0, "profile": [[-21, 0], [0, 120], [400, 120]]})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[160, 0], [400, 0], [400, 400], [160, 400]], "base": 0, "profile": [[-120, 0], [0, 120], [400, 120]]})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "breakup"})
  await session.expectSuccess("warp", {"objectName": "ground", "featureSize": 80, "amplitude": 12, "seed": 4, "plane": "horizontal"})
  await session.expectSuccess("roughen", {"objectName": "ground", "featureSize": 20, "amplitude": 2.5, "octaves": 3, "roughness": 0.5, "seed": 5, "direction": "normal"})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "rock"})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "rock", "material": "rock", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})


def benchSamples(centerline):
  """Spots along the straight middles of a route's legs, at least a cell from where a leg meets a landing or a bend, with the heights
  the route grades them to (a straight leg's grade is even)."""
  samples = []
  for start, end in zip(centerline[:-1], centerline[1:]):
    if math.dist(start[:2], end[:2]) > 30:
      for share in (0.25, 0.5, 0.75):
        samples.append([start[axis] + share * (end[axis] - start[axis]) for axis in range(3)])
  return samples


async def surfaceUnder(session, samples):
  measured = await session.expectSuccess("measure", {"points": [[x, y, z + 40] for x, y, z in samples], "snapToSurface": True})
  return [point[2] for point in measured["points"]]


def largestMiss(samples, heights):
  return max(abs(height - sample[2]) for sample, height in zip(samples, heights))


def testAnEndWithoutZStandsOnTheHighestFootingThere(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    # A stone 6 high on the flat south ground, its top over the roughened ground there, and bare ground 40 south of it.
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "stone", "size": [24, 24, 6], "location": [-200, -260, -1], "collection": "structures"})
    measured = await session.expectSuccess("measure", {"points": [[-200, -260], [-200, -300]], "snapToSurface": True})
    onStone = await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "fromStone", "width": 20, "points": [[-200, -260], [-100, -260, 0]]})
    bare = await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "bare", "width": 20, "points": [[-200, -300], [-100, -300, 0]]})
    return measured["points"], onStone["centerline"][0], bare["centerline"][0]

  (stoneTop, bareGround), fromStone, fromGround = stageBlenderServer.session(steps)
  # The start on the stone takes the stone's top, as measure finds it, not the ground face buried under the stone; the bare start the ground.
  assert stoneTop[2] == 5 and fromStone[2] == 5
  assert fromGround[2] == pytest.approx(bareGround[2], abs=0.01) and abs(fromGround[2]) < 3


def testASwitchbackUpTheHillsideIsBenchedOnItsGradeAndWalked(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    graded = await session.expectSuccess("gradeRoute", switchback)
    heights = await surfaceUnder(session, benchSamples(graded["centerline"]))
    return graded, heights

  graded, heights = stageBlenderServer.session(steps)
  samples = benchSamples(graded["centerline"])
  assert len(samples) == 9
  assert largestMiss(samples, heights) <= 0.05
  # The three legs climb at one grade under the limit; the two hairpins are flat landings over their turns.
  grades = [segment["gradeDegrees"] for segment in graded["segments"]]
  assert 25 < grades[0] < 26 and grades[1] == grades[2] == grades[0] and grades[3] == 0.0
  assert [landing["point"] for landing in graded["landings"]] == [1, 2] and all(landing["turnDegrees"] > 90 for landing in graded["landings"])
  assert graded["pass"] == "route switchback" and graded["foldedFaces"] == 0
  walk = graded["walk"]
  assert walkOf(walk)["walkable"] is True and walkOf(walk)["problems"] == []
  assert walkOf(walk)["steepest"]["slopeDegrees"] <= 26 + 1
  assert walkOf(walk)["narrowest"]["width"] >= switchback["width"] - 1


def testRefusalsNameTheRunTheClashAndTheLedgeSpan(stageBlenderServer, tmp_path):
  hollow = {"objectName": "ground", "mode": "lower", "center": [-200, -250, 0], "radius": 40, "strength": 12, "falloff": "smooth", "direction": [0, 0, 1]}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    steep = await session.expectError("gradeRoute", {"objectName": "ground", "name": "steep", "width": 20, "points": [[-200, -260, 0], [-100, -260, 70]]})
    overWalkable = await session.expectError("gradeRoute", {"objectName": "ground", "name": "overWalkable", "width": 20, "points": [[-200, -260, 0], [-100, -260, 70]], "maximumGradeDegrees": pastWalkableDegrees})
    # Two legs from one hairpin, 30 apart at their far ends (their benches 10 apart), and 45 apart in height there.
    clash = await session.expectError("gradeRoute", {"objectName": "ground", "name": "clash", "width": 20, "points": [[-250, -205, 0], [-100, -190], [-250, -175, 45]]})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hollow"})
    await session.expectSuccess("sculptAtPoint", hollow)
    ledge = await session.expectError("gradeRoute", {"objectName": "ground", "name": "ledge", "width": 20, "points": [[-300, -250, 0], [-100, -250, 0]], "fillBatterDegrees": None})
    passes = await session.expectSuccess("runPython", {"code": "result = [key.name for key in bpy.data.objects['ground'].data.shape_keys.key_blocks]"})
    return steep, overWalkable, clash, ledge, passes["result"]

  steep, overWalkable, clash, ledge, passes = stageBlenderServer.session(steps)
  assert "rises 70.0 from point 0 to point 1 over a run of 100.0, 35.0 degrees, steeper than 26" in steep
  assert f"needs a run of {70 / math.tan(math.radians(26)):.1f}" in steep
  # A route is walked, so its grade limit runs up to the steepest face players walk (playerScale).
  assert f"maximumGradeDegrees is above 0 and at most {steepestWalkableDegrees:.1f} (the steepest face every body group the route's start is built for walks, ['small', 'medium', 'large'], playerScale), got {pastWalkableDegrees}" in overWalkable
  found = re.search(r"stands (-?[\d.]+) high and at \[[^\]]*\], ([\d.]+) away in plan, (-?[\d.]+);.*need ([\d.]+) between them", clash)
  assert "clashes with itself" in clash and found is not None
  first, apart, second, needed = (float(value) for value in found.groups())
  # Heights are named to a tenth, so the separation they need is checked to within what that rounding allows.
  assert abs(needed - abs(first - second) / math.tan(math.radians(70))) <= 0.1 and needed > apart
  spans = [(float(start), float(end), float(height)) for start, end, height in re.findall(r"from \[(-?[\d.]+), -?[\d.]+\] to \[(-?[\d.]+), -?[\d.]+\] \([^)]*\) up to (\d+\.\d)", ledge)]
  assert "is a ledge" in ledge and spans
  # One span runs across the hollow, standing as high over it as the hollow is deep, and stops short of the route's ends.
  hollowSpan = next(span for span in spans if span[0] < -200 < span[1])
  assert -250 < hollowSpan[0] and hollowSpan[1] < -150 and hollowSpan[2] > 8
  # A refused route leaves no pass behind.
  assert passes == ["base", "cliff", "hill", "breakup", "hollow"]


def testARouteIsGradedAsSteeplyAsPlayersWalk(stageBlenderServer, tmp_path):
  rise, run = 18.5, 10

  async def steps(session):
    await caveCanyon(session, tmp_path)
    # Its fill batter is steeper than its grade: past its top end the bench runs on at its grade, so a gentler batter would never meet
    # the ground there.
    return await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "ramp", "width": 20, "points": [[-200, -260, 0], [-200 + run, -260, rise]], "maximumGradeDegrees": 65, "fillBatterDegrees": 80})

  graded = stageBlenderServer.session(steps)
  # A ramp past 60 degrees, under the steepest face players walk (playerScale), is graded and walked.
  grade = math.degrees(math.atan2(rise, run))
  assert grade > 60 and abs(graded["segments"][0]["gradeDegrees"] - grade) <= 0.05
  assert walkOf(graded["walk"])["walkable"] and walkOf(graded["walk"])["problems"] == [] and abs(walkOf(graded["walk"])["steepest"]["slopeDegrees"] - grade) <= 0.1


def testARegradePutsTheBenchBackAfterOtherShapingAndRemovingThePassTakesItBack(stageBlenderServer, tmp_path):
  roughen = {"objectName": "ground", "featureSize": 30, "amplitude": 3, "octaves": 2, "seed": 9, "direction": "up"}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    before = (await session.expectSuccess("runPython", {"code": readGround}))["result"]["vertices"]
    await session.expectSuccess("gradeRoute", switchback)
    removed = await session.expectSuccess("removeShapingPass", {"objectName": "ground", "name": "route switchback"})
    afterRemoval = (await session.expectSuccess("runPython", {"code": readGround}))["result"]["vertices"]
    definitions = (await session.expectSuccess("runPython", {"code": readDefinitions}))["result"]
    graded = await session.expectSuccess("gradeRoute", switchback)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "lumps"})
    await session.expectSuccess("roughen", roughen)
    lumpy = await surfaceUnder(session, benchSamples(graded["centerline"]))
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    back = await surfaceUnder(session, benchSamples(graded["centerline"]))
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "route switchback", "makeActive": True})
    intoRoute = await session.expectError("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [240, -80, 40], "radius": 20, "strength": 3})
    return before, removed, afterRemoval, definitions, graded, lumpy, regraded, back, intoRoute

  before, removed, afterRemoval, definitions, graded, lumpy, regraded, back, intoRoute = stageBlenderServer.session(steps)
  # Taken back, the ground is exactly as it was, and the definition went with the pass.
  assert numpy.abs(numpy.array(afterRemoval) - numpy.array(before)).max() == 0.0
  assert removed["removed"] == "route switchback" and definitions == {}
  samples = benchSamples(graded["centerline"])
  assert largestMiss(samples, lumpy) > 1.0
  assert [entry["route"] for entry in regraded["replayed"]] == ["switchback"] and regraded["replayed"][0]["movedVertices"] > 0
  assert largestMiss(samples, back) <= 0.05
  # The route's pass is rebuilt whole from its definition, so shaping put into it would be lost: it is refused.
  assert "'route switchback'" in intoRoute and "rebuilt whole from its definition" in intoRoute


def testRegradingReplaysTheDefinedPassesInTheOrderTheyWereMade(stageBlenderServer, tmp_path):
  bump = {"objectName": "ground", "mode": "raise", "center": [268, 70, 120], "radius": 70, "strength": 14, "falloff": "smooth", "direction": [0, 0, 1]}
  pad = [[270, 115, 120], [250, 95, 120], [290, 135, 120], [250, 135, 120], [290, 95, 120]]

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("setZoneHousing", {"role": "featured", "intent": "plots on the canyon rim", "placement": "world", "plotBudget": {"player": 2, "guild": 0}})
    await session.expectSuccess("placePlot", plot)
    await session.expectSuccess("gradePlot", {"address": plot["address"], "objectName": "ground", "margin": 6})
    graded = await session.expectSuccess("gradeRoute", switchback)
    samples = benchSamples(graded["centerline"])
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "bump"})
    await session.expectSuccess("sculptAtPoint", bump)
    bumped = await surfaceUnder(session, pad + samples)
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    afterRegrade = await surfaceUnder(session, pad + samples)
    moved = await session.expectSuccess("editPlot", {"address": plot["address"], "center": [262, 120], "height": 120})
    afterMove = await surfaceUnder(session, samples)
    regradedPlot = await session.expectSuccess("gradePlot", {"address": plot["address"], "objectName": "ground", "margin": 10})
    afterGrade = await surfaceUnder(session, samples)
    return samples, bumped, regraded, afterRegrade, moved, afterMove, regradedPlot, afterGrade

  samples, bumped, regraded, afterRegrade, moved, afterMove, regradedPlot, afterGrade = stageBlenderServer.session(steps)
  expected = pad + samples
  assert largestMiss(expected, bumped) > 5
  # The plot was graded first and the route to its entrance after it: they are replayed in that order, and both are on target again.
  assert [list(entry)[0] for entry in regraded["replayed"]] == ["plots", "route"]
  assert regraded["replayed"][0]["plots"] == [plot["address"]] and regraded["replayed"][1]["route"] == "switchback"
  assert largestMiss(expected, afterRegrade) <= 0.05
  # Moving the plot grades it again where it now lies and replays the later route over it, whose bench stays on target; grading the
  # plot again does the same.
  assert [entry["route"] for entry in moved["grading"]["replayed"]] == ["switchback"]
  assert largestMiss(samples, afterMove) <= 0.05
  assert [entry["route"] for entry in regradedPlot["replayed"]] == ["switchback"]
  assert largestMiss(samples, afterGrade) <= 0.05


def testAGradingLeavesTheGroundItGradesAgainAsIs(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("gradeRoute", cleft)
    graded = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "lumps"})
    await session.expectSuccess("roughen", {"objectName": "ground", "featureSize": 30, "amplitude": 3, "octaves": 2, "seed": 9, "direction": "up"})
    lumpy = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    regraded = [await session.expectSuccess("regradeTerrain", {"objectName": "ground"}) for _ in range(3)]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "cleft.blend")})
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "cleft.eqg"), "purpose": "test"})
    return graded, lumpy, regraded, detail, checked

  graded, lumpy, regraded, detail, checked = stageBlenderServer.session(steps)
  # Grading triangulates what it moved along the new contours, and grading again on those faces, as a regrade and the export's stale
  # test do, moves nothing: right after gradeRoute, and after the regrade that puts it back on the lumpy ground.
  assert [(entry["route"], entry["stale"], entry["largestChange"]) for entry in graded["definedPasses"]] == [("cleft", False, 0.0)]
  assert [(entry["route"], entry["stale"]) for entry in lumpy["definedPasses"]] == [("cleft", True)]
  assert regraded[0]["replayed"][0]["movedVertices"] > 0
  assert [[(entry["route"], entry["movedVertices"], entry["largestChange"]) for entry in result["replayed"]] for result in regraded[1:]] == [[("cleft", 0, 0.0)]] * 2
  assert [(entry["route"], entry["stale"], entry["largestChange"]) for entry in detail["definedPasses"]] == [("cleft", False, 0.0)]
  assert checked["toConfirm"] == []
