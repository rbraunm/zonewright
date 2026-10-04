import math
import re

import numpy

from conftest import writePNG
from testModelsAndDressing import readShapedMesh

# A three-leg switchback climbing 100 up caveCanyon's 45-degree hillside to the entrance of a plot on the rim: legs 24 degrees off the
# contour, two hairpins, its ends fixed (an end left free takes the ground there each time it is graded, and moves with it).
switchback = {
  "objectName": "ground", "name": "switchback", "width": 20,
  "points": [[210, -95, 20], [296.8, -56.4], [187.2, -7.6], [274.0, 31.0, 120], [270, 75, 120]],
}
plot = {"address": "1 Rim Walk", "center": [270, 115], "facingDegrees": 180, "size": [80, 80], "height": 120}
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
  assert walk["walkable"] is True and walk["problems"] == []
  assert walk["steepest"]["slopeDegrees"] <= 26 + 1
  assert walk["narrowest"]["width"] >= switchback["width"] - 1


def testRefusalsNameTheRunTheClashAndTheLedgeSpan(stageBlenderServer, tmp_path):
  hollow = {"objectName": "ground", "mode": "lower", "center": [-200, -250, 0], "radius": 40, "strength": 12, "falloff": "smooth", "direction": [0, 0, 1]}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    steep = await session.expectError("gradeRoute", {"objectName": "ground", "name": "steep", "width": 20, "points": [[-200, -260, 0], [-100, -260, 70]]})
    # Two legs from one hairpin, 30 apart at their far ends (their benches 10 apart), and 45 apart in height there.
    clash = await session.expectError("gradeRoute", {"objectName": "ground", "name": "clash", "width": 20, "points": [[-250, -205, 0], [-100, -190], [-250, -175, 45]]})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hollow"})
    await session.expectSuccess("sculptAtPoint", hollow)
    ledge = await session.expectError("gradeRoute", {"objectName": "ground", "name": "ledge", "width": 20, "points": [[-300, -250, 0], [-100, -250, 0]], "fillBatterDegrees": None})
    passes = await session.expectSuccess("runPython", {"code": "result = [key.name for key in bpy.data.objects['ground'].data.shape_keys.key_blocks]"})
    return steep, clash, ledge, passes["result"]

  steep, clash, ledge, passes = stageBlenderServer.session(steps)
  assert "rises 70.0 from point 0 to point 1 over a run of 100.0, 35.0 degrees, steeper than 26" in steep
  assert f"needs a run of {70 / math.tan(math.radians(26)):.1f}" in steep
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
