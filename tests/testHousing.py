import json
import math

import numpy

from conftest import writePNG
from testModelsAndDressing import freshScene, readShapedMesh

decision = {"role": "featured", "intent": "plots along a test street", "placement": "world", "plotBudget": {"player": 6, "guild": 0}}

readBorder = """
import mathutils
border = next(child for child in bpy.data.objects[plotName].children)
matrix = border.matrix_world
corners = [matrix @ mathutils.Vector(corner) for corner in border.bound_box]
local = [mathutils.Vector(vertex.co) for vertex in border.data.vertices]
middle = (mathutils.Vector([min(v[axis] for v in local) for axis in range(3)]) + mathutils.Vector([max(v[axis] for v in local) for axis in range(3)])) / 2
result = {"name": border.name, "openSide": list((matrix.to_3x3() @ mathutils.Vector((-1, 0, 0))).normalized()), "middle": list(matrix @ middle), "scale": list(matrix.to_scale())}
"""


async def flatGround(session, tmp_path):
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [800, 800], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  grass = writePNG(tmp_path / "grass.png", 4, 4, (90, 120, 60, 255))
  await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(grass)})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})


def testHousingStartsWithTheZonesDecision(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    undecided = await session.expectError("placePlot", {"address": "101 Early Street", "center": [0, 0], "facingDegrees": 0})
    incomplete = await session.expectError("setZoneHousing", {"role": "featured", "intent": "a street"})
    decided = await session.expectSuccess("setZoneHousing", decision)
    await session.expectSuccess("placePlot", {"address": "101 Test Street", "center": [0, 0], "facingDegrees": 0})
    stillHasPlots = await session.expectError("setZoneHousing", {"role": "none"})
    unknownFeature = await session.expectError("placePlot", {"address": "102 Test Street", "center": [300, 0], "facingDegrees": 0, "features": ["haunted"]})
    added = await session.expectSuccess("setZoneHousing", {"pricing": {"featureMultipliers": {"haunted": 0.8}}})
    return undecided, incomplete, decided, stillHasPlots, unknownFeature, added

  undecided, incomplete, decided, stillHasPlots, unknownFeature, added = stageBlenderServer.session(steps)
  assert "made no housing decision" in undecided
  assert "needs ['placement', 'plotBudget']" in incomplete
  assert decided["housing"]["role"] == "featured" and decided["plotCounts"] == {"player": 0, "guild": 0}
  assert "remove them before its housing role is none" in stillHasPlots
  assert "Features ['haunted'] are not in the zone's pricing" in unknownFeature
  # Pricing changes keep the rest of the rules.
  assert added["housing"]["pricing"]["featureMultipliers"]["haunted"] == 0.8 and added["housing"]["pricing"]["featureMultipliers"]["view"] == 1.25


def testPlotBorderOpensTowardItsStreetAndItsPriceFollowsTheRules(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneHousing", decision)
    placed = await session.expectSuccess("placePlot", {"address": "101 Test Street", "center": [0, 0], "facingDegrees": 90, "items": 120, "pets": 6, "features": ["view"]})
    lowTier = await session.expectSuccess("placePlot", {"address": "103 Test Street", "center": [0, 300], "facingDegrees": 90, "items": 90, "pets": 4})
    border = (await session.expectSuccess("runPython", {"code": "plotName = '101 Test Street'\n" + readBorder}))["result"]
    overridden = await session.expectSuccess("editPlot", {"address": "101 Test Street", "pricePlatinum": 200})
    restored = await session.expectSuccess("editPlot", {"address": "101 Test Street", "pricePlatinum": 0})
    smaller = await session.expectSuccess("editPlot", {"address": "101 Test Street", "size": [120, 120]})
    smallerBorder = (await session.expectSuccess("runPython", {"code": "plotName = '101 Test Street'\n" + readBorder}))["result"]
    overlapping = await session.expectSuccess("placePlot", {"address": "102 Test Street", "center": [60, 0], "facingDegrees": 90})
    return placed, lowTier, border, overridden, restored, smaller, smallerBorder, overlapping

  placed, lowTier, border, overridden, restored, smaller, smallerBorder, overlapping = stageBlenderServer.session(steps)
  # Facing east (+X), the border's open side (the model's -x wall) opens east, and the border's middle stands on the plot's center.
  assert numpy.allclose(border["openSide"], [1, 0, 0], atol=1e-6)
  assert numpy.allclose(border["middle"][:2], [0, 0], atol=1e-3) and border["name"] == "101 Test Street border"
  # Live's top tier, 120 items and 6 pets, is its 126pp, and the view raises it: 126 * 1.25 = 157.5; its bottom tier, 90 and 4, is 42pp.
  assert placed["pricePlatinum"] == 158 and placed["upkeepPlatinumPerDay"] == 15.8 and [step["step"] for step in placed["steps"]][-1] == "view x1.25"
  assert lowTier["pricePlatinum"] == 42 and lowTier["items"] == 90 and lowTier["pets"] == 4
  assert overridden["pricePlatinum"] == 200 and overridden["derivedPlatinum"] == 158 and overridden["overridden"]
  assert restored["pricePlatinum"] == 158 and not restored["overridden"]
  # 120 x 120: the border scales as a door's size does, in whole percents, and the price by area.
  assert smaller["border"]["size"] == 71 and numpy.allclose(smallerBorder["scale"], 0.71)
  assert smaller["pricePlatinum"] == round((84 * 120 * 120 / (169.1 * 170.1) + 15 * 2.1 + 10.5) * 1.25)
  assert overlapping["overlaps"][0]["plot"] == "101 Test Street"


def testGradePlotLevelsItsGroundInItsOwnPassAndRegradesAfterAMove(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 300, "strength": 30, "falloff": "linear"})
    before = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    await session.expectSuccess("setZoneHousing", decision)
    placed = await session.expectSuccess("placePlot", {"address": "101 Hill Street", "center": [100, 0], "facingDegrees": 0})
    graded = await session.expectSuccess("gradePlot", {"address": "101 Hill Street", "objectName": "ground"})
    first = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    assessed = await session.expectSuccess("assessPlot", {"address": "101 Hill Street"})
    moved = await session.expectSuccess("editPlot", {"address": "101 Hill Street", "center": [-100, 0]})
    regraded = await session.expectSuccess("gradePlot", {"address": "101 Hill Street", "objectName": "ground"})
    second = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    floating = await session.expectSuccess("placePlot", {"address": "102 Hill Street", "center": [0, 300], "facingDegrees": 0, "height": 900})
    tooHigh = await session.expectError("gradePlot", {"address": "102 Hill Street", "objectName": "ground"})
    removed = await session.expectSuccess("removePlot", {"address": "101 Hill Street", "gradedObject": "ground"})
    third = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    return before, placed, graded, first, assessed, moved, regraded, second, floating, tooHigh, removed, third

  before, placed, graded, first, assessed, moved, regraded, second, floating, tooHigh, removed, third = stageBlenderServer.session(steps)
  original = numpy.array(before["result"]["vertices"])

  def inside(vertices, center, half=84):
    return (numpy.abs(vertices[:, 0] - center[0]) <= half) & (numpy.abs(vertices[:, 1] - center[1]) <= half)

  height = placed["center"][2]
  firstVertices = numpy.array(first["result"]["vertices"])
  assert numpy.allclose(firstVertices[inside(firstVertices, (100, 0)), 2], height, atol=1e-3)
  assert graded["pass"] == "grade 101 Hill Street" and graded["deepestCut"] > 5 and graded["highestFill"] > 5
  assert assessed["under"]["unevenness"] < 1e-3 and assessed["under"]["cutToLevel"] < 1e-3
  # Graded again where it moved to: the new place is level, and the old one is back to its own slope.
  secondVertices = numpy.array(second["result"]["vertices"])
  assert numpy.allclose(secondVertices[inside(secondVertices, (-100, 0)), 2], moved["center"][2], atol=1e-3)
  away = inside(original, (100, 0), 60) & ~inside(original, (-100, 0), 160)
  assert numpy.allclose(secondVertices[away, 2], original[away, 2], atol=1e-3)
  assert "stands" in tooHigh and "off the ground around it" in tooHigh
  # Removed with its grading, the ground is as it was.
  assert numpy.allclose(numpy.array(third["result"]["vertices"]), original, atol=1e-3) and removed["removed"] == ["101 Hill Street", "101 Hill Street border"]


def testStreetOfPlotsExportsTheZonesHousingFile(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "teststreet.eqg"

  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneHousing", decision)
    laid = await session.expectSuccess("layOutPlots", {"street": "Main Street", "path": [[-300, 0], [300, 0]], "side": "both"})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath)})
    housingFile = json.loads((tmp_path / "teststreet_housing.json").read_text())
    assets = (tmp_path / "teststreet_assets.txt").read_bytes()
    await session.expectSuccess("placePlot", {"address": "200 Main Street", "center": [-200, 100], "facingDegrees": 0})
    overlapped = await session.expectError("exportZone", {"path": str(archivePath)})
    for plot in ["200 Main Street"] + [entry["address"] for entry in laid["placed"]]:
      await session.expectSuccess("removePlot", {"address": plot})
    await session.expectSuccess("setZoneHousing", {"role": "none"})
    noHousing = await session.expectSuccess("exportZone", {"path": str(archivePath)})
    return laid, exported, housingFile, assets, overlapped, noHousing

  laid, exported, housingFile, assets, overlapped, noHousing = stageBlenderServer.session(steps)
  # Stations a plot's width plus 20 apart along the 600-unit street, left before right at each, each 20 off the street facing it.
  assert [entry["address"] for entry in laid["placed"]] == [f"{number} Main Street" for number in range(101, 107)] and laid["skipped"] == []
  left, right = laid["placed"][0], laid["placed"][1]
  assert numpy.allclose(left["center"][:2], [-300 + 169.1 / 2, 20 + 170.1 / 2]) and left["facingDegrees"] == 180
  assert numpy.allclose(right["center"][:2], [-300 + 169.1 / 2, -20 - 170.1 / 2]) and right["facingDegrees"] == 0
  assert exported["housing"] == {"role": "featured", "plots": 6, "file": str(tmp_path / "teststreet_housing.json"), "assetList": str(tmp_path / "teststreet_assets.txt")}
  plots = {plot["address"]: plot for plot in housingFile["plots"]}
  first = plots["101 Main Street"]
  # The server's axes swap the zone's x and y; facing south (the street) the border's heading is 128 (EQ +x, zone +Y), opening away.
  assert first["center"] == [round(left["center"][1], 3), round(left["center"][0], 3), 0.0] and first["heading"] == 128
  assert plots["102 Main Street"]["heading"] == 384 and first["kind"] == "plot" and first["pricePlatinum"] == 84 and first["capacity"] == 105 and first["pets"] == 5
  doors = {door["door"]: door for door in housingFile["doors"]}
  assert doors[first["door"]]["name"] == "OBP_LOTSQUARE" and doors[first["door"]]["openType"] == 160 and doors[first["door"]]["size"] == 100
  assert math.dist(doors[first["door"]]["position"][:2], first["center"][:2]) < 1.5
  assert housingFile["housing"]["placement"] == "world" and assets == b"stonesquare.eqg\r\n"
  assert "overlap" in overlapped
  assert noHousing["housing"] == {"role": "none", "plots": 0, "file": None, "assetList": None}
  assert not (tmp_path / "teststreet_housing.json").exists() and not (tmp_path / "teststreet_assets.txt").exists()
