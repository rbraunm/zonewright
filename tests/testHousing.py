import io
import json
import math

import numpy
from PIL import Image

from conftest import writePNG
from testModelsAndDressing import freshScene, readShapedMesh
from testSketch import environment

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
    guide = (await session.expectSuccess("runPython", {"code": "result = [list(vertex.co) for vertex in bpy.data.objects['103 Test Street'].data.vertices]"}))["result"]
    border = (await session.expectSuccess("runPython", {"code": "plotName = '101 Test Street'\n" + readBorder}))["result"]
    overridden = await session.expectSuccess("editPlot", {"address": "101 Test Street", "pricePlatinum": 200})
    restored = await session.expectSuccess("editPlot", {"address": "101 Test Street", "pricePlatinum": 0})
    smaller = await session.expectSuccess("editPlot", {"address": "101 Test Street", "size": [120, 120]})
    smallerBorder = (await session.expectSuccess("runPython", {"code": "plotName = '101 Test Street'\n" + readBorder}))["result"]
    overlapping = await session.expectSuccess("placePlot", {"address": "102 Test Street", "center": [60, 0], "facingDegrees": 90})
    return placed, lowTier, guide, border, overridden, restored, smaller, smallerBorder, overlapping

  placed, lowTier, guide, border, overridden, restored, smaller, smallerBorder, overlapping = stageBlenderServer.session(steps)
  # The guide stays inside the plot: its entrance mark is a chevron with its tip on the middle of the entrance side (local +y),
  # pointing out, and its arms within 9 of that side.
  guide = numpy.array(guide)
  assert numpy.isclose(guide[:, 1].max(), 170.1 / 2) and numpy.isclose(numpy.abs(guide[:, 0]).max(), 169.1 / 2) and len(guide) == 14
  chevron = guide[8:]
  assert numpy.isclose(chevron[numpy.isclose(chevron[:, 0], 0), 1].max(), 170.1 / 2) and 170.1 / 2 - 9 < chevron[:, 1].min() < 170.1 / 2 - 6
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


def testTheEntranceMarkShowsFromTheStreetAndStaysOutOfTheViewFromTheEntrance(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("setZoneHousing", decision)
    await session.expectSuccess("placePlot", {"address": "101 Test Street", "center": [0, 0], "facingDegrees": 0})
    entrance, _ = await session.expectImage("renderView", {"view": {"standAt": [0, 84.5], "headingDegrees": 180, "pitchDegrees": -6}})
    street, _ = await session.expectImage("renderView", {"view": {"standAt": [0, 110], "headingDegrees": 180, "pitchDegrees": -8}})
    return entrance, street

  entrance, street = stageBlenderServer.session(steps)

  def guideCyan(image):
    pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)
    return (pixels[..., 0] < 90) & (pixels[..., 1] > 170) & (pixels[..., 2] > 200)

  # Standing on the middle of the entrance looking in, no part of the guide is in view.
  assert not guideCyan(entrance).any()
  # From the street 25 out, the entrance side runs across the view and the chevron rises from its middle toward the plot's back.
  cyan = guideCyan(street)
  bandRows = numpy.flatnonzero(cyan.mean(axis=1) > 0.9)
  assert len(bandRows) and cyan[bandRows.min() - 40:bandRows.min() - 4, 360:600].sum() > 200
  assert not cyan[bandRows.min() - 40:bandRows.min() - 4, :300].any() and not cyan[bandRows.min() - 40:bandRows.min() - 4, 660:].any()


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
    second = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    floating = await session.expectSuccess("placePlot", {"address": "102 Hill Street", "center": [0, 300], "facingDegrees": 0, "height": 900})
    tooHigh = await session.expectError("gradePlot", {"address": "102 Hill Street", "objectName": "ground"})
    removed = await session.expectSuccess("removePlot", {"address": "101 Hill Street"})
    third = await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh})
    return before, placed, graded, first, assessed, moved, second, floating, tooHigh, removed, third

  before, placed, graded, first, assessed, moved, second, floating, tooHigh, removed, third = stageBlenderServer.session(steps)
  original = numpy.array(before["result"]["vertices"])

  def inside(vertices, center, half=84):
    return (numpy.abs(vertices[:, 0] - center[0]) <= half) & (numpy.abs(vertices[:, 1] - center[1]) <= half)

  height = placed["center"][2]
  firstVertices = numpy.array(first["result"]["vertices"])
  assert numpy.allclose(firstVertices[inside(firstVertices, (100, 0)), 2], height, atol=1e-3)
  assert graded["pass"] == "grade 101 Hill Street" and graded["deepestCut"] > 5 and graded["highestFill"] > 5
  assert assessed["under"]["unevenness"] < 1e-3 and assessed["under"]["cutToLevel"] < 1e-3
  # Moved, it is graded again where it now lies: the new place is level, and the old one is back to its own slope.
  assert moved["grading"]["pass"] == "grade 101 Hill Street" and moved["grading"]["height"] == moved["center"][2]
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



def testAPlotStandsOnGroundMadeJustBeforeIt(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    # A ledge 100 high (y -50..150) at the foot of a cliff 300 high (y 150..350), placed and at once built on.
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ledge", "size": [400, 200, 100], "location": [0, 50, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "cliff", "size": [800, 200, 300], "location": [0, 250, 0]})
    await session.expectSuccess("setZoneHousing", decision)
    return await session.expectSuccess("placePlot", {"address": "101 Ledge Walk", "center": [0, 50], "facingDegrees": 180})

  placed = stageBlenderServer.session(steps)
  assert placed["center"][2] == 100.0


# Two plots facing each other across a street up a slope of 0.18: 1 Low Lane below at -18, 2 Low Lane above at 19.8. Their fronts,
# grown by grading's 10-unit margin, stand 19.9 apart: too close at 35 degrees for the 37.8 between their heights.
lowLane = [("1 Low Lane", [0, -100], 0), ("2 Low Lane", [0, 110], 180)]
readPasses = "keys = bpy.data.objects['ground'].data.shape_keys\nresult = None if keys is None else [key.name for key in keys.key_blocks]"


async def slopedPlots(session, plots):
  await freshScene(session)
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [600, 600], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("runPython", {"code": "mesh = bpy.data.objects['ground'].data\nfor vertex in mesh.vertices:\n  vertex.co.z = 0.18 * vertex.co.y\nmesh.update()"})
  await session.expectSuccess("setZoneHousing", decision)
  for address, center, facing in plots:
    await session.expectSuccess("placePlot", {"address": address, "center": center, "facingDegrees": facing})


async def groundVertices(session):
  return numpy.array((await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh}))["result"]["vertices"])


def testGradingKeepsEveryPadLevelWhateverTheOrder(stageBlenderServer):
  async def steps(session):
    await slopedPlots(session, lowLane)
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground"})
    second = await session.expectSuccess("gradePlot", {"address": "2 Low Lane", "objectName": "ground"})
    assessed = [await session.expectSuccess("assessPlot", {"address": address}) for address, _, _ in lowLane]
    firstThenSecond = await groundVertices(session)
    removed = await session.expectSuccess("removePlot", {"address": "1 Low Lane"})
    remaining = await session.expectSuccess("assessPlot", {"address": "2 Low Lane"})
    afterRemoval = await groundVertices(session)
    await slopedPlots(session, lowLane)
    for address in ("2 Low Lane", "1 Low Lane"):
      await session.expectSuccess("gradePlot", {"address": address, "objectName": "ground"})
    secondThenFirst = await groundVertices(session)
    await slopedPlots(session, lowLane[1:])
    await session.expectSuccess("gradePlot", {"address": "2 Low Lane", "objectName": "ground"})
    alone = await session.expectSuccess("assessPlot", {"address": "2 Low Lane"})
    gradedAlone = await groundVertices(session)
    return second, assessed, firstThenSecond, removed, remaining, afterRemoval, secondThenFirst, alone, gradedAlone

  second, assessed, firstThenSecond, removed, remaining, afterRemoval, secondThenFirst, alone, gradedAlone = stageBlenderServer.session(steps)
  # Grading the upper plot leaves the lower one's pad level and names it as touched, and the ground between their pads runs in one
  # straight bank, steeper than the batters, across the 19.9 between them.
  assert [entry["under"]["unevenness"] for entry in assessed] == [0.0, 0.0] and [entry["under"]["lowest"] for entry in assessed] == [-18.0, 19.8]
  assert [entry["plot"] for entry in second["touched"]] == ["1 Low Lane"]
  assert second["steepBanks"] == [{"plots": ["1 Low Lane", "2 Low Lane"], "steepestDegrees": round(math.degrees(math.atan(37.8 / 19.9)), 1)}]
  # The order of grading changes nothing, and taking the first grading back leaves the ground as grading the second alone does.
  assert numpy.allclose(firstThenSecond, secondThenFirst, atol=1e-3)
  assert removed["grading"]["gradedPlots"] == ["2 Low Lane"] and numpy.allclose(afterRemoval, gradedAlone, atol=1e-3)
  assert remaining["under"]["unevenness"] == 0.0 and abs(remaining["entrance"]["ground"] - alone["entrance"]["ground"]) < 0.01


def testARenamedGradedPlotKeepsItsGradingAndARefusedRemovalRemovesNothing(stageBlenderServer):
  async def steps(session):
    await slopedPlots(session, lowLane[:1])
    natural = await groundVertices(session)
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground"})
    renamed = await session.expectSuccess("editPlot", {"address": "1 Low Lane", "newAddress": "9 Low Lane"})
    renamedPasses = (await session.expectSuccess("runPython", {"code": readPasses}))["result"]
    removed = await session.expectSuccess("removePlot", {"address": "9 Low Lane"})
    restored = await groundVertices(session)
    restoredPasses = (await session.expectSuccess("runPython", {"code": readPasses}))["result"]
    await session.expectSuccess("placePlot", {"address": "3 Low Lane", "center": [0, -100], "facingDegrees": 0})
    await session.expectSuccess("gradePlot", {"address": "3 Low Lane", "objectName": "ground"})
    graded = await groundVertices(session)
    kept = await session.expectSuccess("removePlot", {"address": "3 Low Lane", "keepGrading": True})
    keptGround = await groundVertices(session)
    keptPasses = (await session.expectSuccess("runPython", {"code": readPasses}))["result"]
    await session.expectSuccess("placePlot", {"address": "4 Low Lane", "center": [0, 120], "facingDegrees": 180})
    await session.expectSuccess("gradePlot", {"address": "4 Low Lane", "objectName": "ground"})
    await session.expectSuccess("collapseShapingPasses", {"objectName": "ground"})
    refused = await session.expectError("removePlot", {"address": "4 Low Lane"})
    housing = await session.expectSuccess("getHousing", {})
    return natural, renamed, renamedPasses, removed, restored, restoredPasses, graded, kept, keptGround, keptPasses, refused, housing

  natural, renamed, renamedPasses, removed, restored, restoredPasses, graded, kept, keptGround, keptPasses, refused, housing = stageBlenderServer.session(steps)
  # Renamed, the plot's pass goes by its new address, and removing it takes its grading back.
  assert renamed["address"] == "9 Low Lane" and renamed["grading"]["pass"] == "grade 9 Low Lane" and renamedPasses == ["base", "grade 9 Low Lane"]
  assert removed["removed"] == ["9 Low Lane", "9 Low Lane border"] and restoredPasses is None and numpy.allclose(restored, natural, atol=1e-3)
  # Kept, the grading stays as ordinary shaping.
  assert kept["grading"]["gradedPlots"] == [] and keptPasses == ["base", "kept grade 3 Low Lane"] and numpy.allclose(keptGround, graded, atol=1e-3)
  # Its passes collapsed, a plot's grading cannot be taken back: the removal is refused and the plot stays.
  assert "to take back" in refused and [plot["address"] for plot in housing["plots"]] == ["4 Low Lane"]


def testGradingFollowsItsGroundWhenRenamedAndADeletedGroundIsRefusedUntilGradedAgain(stageBlenderServer):
  readTerrain = "objectName = 'terrain'" + readShapedMesh

  async def steps(session):
    await slopedPlots(session, lowLane[:1])
    natural = await groundVertices(session)
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground"})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['ground'].name = 'terrain'"})
    renamed = await session.expectSuccess("getHousing", {})
    seated = await session.expectSuccess("placePlot", {"address": "5 Low Lane", "center": [40, -150], "facingDegrees": 0, "size": [30, 30]})
    await session.expectSuccess("removePlot", {"address": "5 Low Lane"})
    removed = await session.expectSuccess("removePlot", {"address": "1 Low Lane"})
    restored = numpy.array((await session.expectSuccess("runPython", {"code": readTerrain}))["result"]["vertices"])
    await slopedPlots(session, lowLane)
    for address, _, _ in lowLane:
      await session.expectSuccess("gradePlot", {"address": address, "objectName": "ground"})
    await session.expectSuccess("deleteObjects", {"names": ["ground"]})
    deleted = await session.expectSuccess("getHousing", {})
    placeRefused = await session.expectError("placePlot", {"address": "3 Low Lane", "center": [300, 0], "facingDegrees": 0})
    removeRefused = await session.expectError("removePlot", {"address": "1 Low Lane"})
    removedAlone = await session.expectSuccess("removePlot", {"address": "2 Low Lane", "keepGrading": True})
    await session.expectSuccess("createTerrainGrid", {"name": "spare", "size": [800, 800], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    regraded = await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "spare"})
    placed = await session.expectSuccess("placePlot", {"address": "3 Low Lane", "center": [300, 0], "facingDegrees": 0})
    return natural, renamed, seated, removed, restored, deleted, placeRefused, removeRefused, removedAlone, regraded, placed

  natural, renamed, seated, removed, restored, deleted, placeRefused, removeRefused, removedAlone, regraded, placed = stageBlenderServer.session(steps)
  # Its ground renamed, the plot is still graded on it: a plot placed inside its pad sits on the slope as it lies under the pad
  # (0.18 * -150), not on the pad at -18, and removing the graded plot takes its pass back off the renamed ground.
  assert renamed["plots"][0]["grading"] == {"object": "terrain", "margin": 10.0, "batterDegrees": 35.0}
  assert seated["center"][2] == -27.0
  assert removed["grading"]["gradedPlots"] == [] and numpy.allclose(restored, natural, atol=1e-3)
  # Its ground deleted, the plot's grading names no ground, and seating any plot or taking its grading back is refused until it is
  # graded on ground again; keepGrading removes such a plot alone.
  assert [plot["grading"]["object"] for plot in deleted["plots"]] == [None, None]
  assert "Plot '1 Low Lane' was graded on ground that no longer exists" in placeRefused and "gradePlot" in placeRefused
  assert "no grading to take back" in removeRefused and "keepGrading true" in removeRefused
  assert removedAlone == {"removed": ["2 Low Lane", "2 Low Lane border"], "grading": None}
  assert regraded["object"] == "spare" and regraded["height"] == -18.0 and "formerGround" not in regraded
  assert placed["center"][2] == 0.0 and placed["grading"] is None


def testAGradedPlotMovesOntoAnotherGroundMeshWhenToldWhich(stageBlenderServer):
  tilePasses = "result = [None if (keys := bpy.data.objects[name].data.shape_keys) is None else [key.name for key in keys.key_blocks] for name in ('tileWest', 'tileEast')]"

  async def steps(session):
    await freshScene(session)
    for name, x in (("tileWest", -200), ("tileEast", 200)):
      await session.expectSuccess("createTerrainGrid", {"name": name, "size": [400, 400], "spacing": 8, "location": [x, 0, 0], "collection": "terrain"})
      await session.expectSuccess("runPython", {"code": f"mesh = bpy.data.objects['{name}'].data\nfor vertex in mesh.vertices:\n  vertex.co.z = 0.12 * vertex.co.y\nmesh.update()"})
    await session.expectSuccess("setZoneHousing", decision)
    await session.expectSuccess("placePlot", {"address": "1 Tile Way", "center": [-200, 40], "facingDegrees": 180})
    await session.expectSuccess("placePlot", {"address": "2 Tile Way", "center": [-200, -150], "facingDegrees": 0, "size": [100, 60]})
    notGraded = await session.expectError("editPlot", {"address": "2 Tile Way", "objectName": "tileEast"})
    await session.expectSuccess("gradePlot", {"address": "1 Tile Way", "objectName": "tileWest"})
    offItsGround = await session.expectError("editPlot", {"address": "1 Tile Way", "center": [200, 40]})
    moved = await session.expectSuccess("editPlot", {"address": "1 Tile Way", "center": [200, 40], "objectName": "tileEast"})
    passes = (await session.expectSuccess("runPython", {"code": tilePasses}))["result"]
    west = (await session.expectSuccess("runPython", {"code": "objectName = 'tileWest'" + readShapedMesh}))["result"]["vertices"]
    assessed = await session.expectSuccess("assessPlot", {"address": "1 Tile Way"})
    housing = await session.expectSuccess("getHousing", {})
    return notGraded, offItsGround, moved, passes, west, assessed, housing

  notGraded, offItsGround, moved, passes, west, assessed, housing = stageBlenderServer.session(steps)
  assert "Plot '2 Tile Way' is not graded" in notGraded
  # Moved off the mesh it is graded on, it is refused with the way on; told the other mesh, its grading moves there.
  assert "No vertex of 'tileWest' lies under or near plot '1 Tile Way'" in offItsGround and "(objectName)" in offItsGround
  assert moved["center"] == [200.0, 40.0, 4.8] and moved["grading"]["object"] == "tileEast" and moved["grading"]["height"] == 4.8
  assert moved["grading"]["formerGround"]["object"] == "tileWest" and moved["grading"]["formerGround"]["gradedPlots"] == []
  assert passes == [None, ["base", "grade 1 Tile Way"]] and assessed["under"]["unevenness"] == 0.0
  west = numpy.array(west)
  assert numpy.allclose(west[:, 2], 0.12 * west[:, 1], atol=1e-3)
  assert housing["plots"][0]["grading"] == {"object": "tileEast", "margin": 10.0, "batterDegrees": 35.0}


def testGradingKeepsShapingOutOfThePlotsPasses(stageBlenderServer):
  readPassState = "keys = bpy.data.objects['ground'].data.shape_keys\nresult = [[key.name for key in keys.key_blocks], bpy.data.objects['ground'].active_shape_key.name]"
  bump = {"objectName": "ground", "mode": "raise", "center": [204, 204, 36.72], "radius": 30, "strength": 10, "falloff": "smooth", "direction": [0, 0, 1]}
  readBump = "import bridgeWater\nresult = bridgeWater.Ground().heightBelow(204, 204, 500)"

  async def steps(session):
    await slopedPlots(session, lowLane[:1])
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground"})
    noPass = await session.expectError("sculptAtPoint", bump)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "ridges"})
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground", "margin": 12})
    afterGrading = (await session.expectSuccess("runPython", {"code": readPassState}))["result"]
    await session.expectSuccess("sculptAtPoint", bump)
    raised = (await session.expectSuccess("runPython", {"code": readBump}))["result"]
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground", "margin": 8})
    regraded = (await session.expectSuccess("runPython", {"code": readBump}))["result"]
    return noPass, afterGrading, raised, regraded

  noPass, afterGrading, raised, regraded = stageBlenderServer.session(steps)
  # On ground that had no passes, grading leaves none active, so shaping is refused until it has a pass of its own.
  assert "none is active" in noPass and "addShapingPass" in noPass
  # The pass active before grading stays active, so the bump goes into it and grading again keeps it (0.18 * 204 + 10 at the vertex).
  assert afterGrading == [["base", "grade 1 Low Lane", "ridges"], "ridges"]
  assert abs(raised - 46.72) < 0.01 and abs(regraded - raised) < 1e-3


def testAMovedGradedPlotSitsOnTheUngradedGroundAndTakesItsGradingAlong(stageBlenderServer):
  async def steps(session):
    await slopedPlots(session, lowLane[1:])
    natural = await groundVertices(session)
    await session.expectSuccess("gradePlot", {"address": "2 Low Lane", "objectName": "ground"})
    graded = await groundVertices(session)
    moved = await session.expectSuccess("editPlot", {"address": "2 Low Lane", "center": [0, 150]})
    assessed = await session.expectSuccess("assessPlot", {"address": "2 Low Lane"})
    after = await groundVertices(session)
    return natural, graded, moved, assessed, after

  natural, graded, moved, assessed, after = stageBlenderServer.session(steps)
  # Up the slope it sits on the ground as it lay before any grading (0.18 * 150), not on its own old pad at 19.8, and is level there.
  assert moved["center"][2] == 27.0 and moved["grading"]["height"] == 27.0
  assert assessed["under"]["unevenness"] == 0.0 and assessed["under"]["lowest"] == 27.0
  # The old pad's fill in front of it, which the new grading does not reach, is gone.
  front = (numpy.abs(natural[:, 0]) <= 80) & (numpy.abs(natural[:, 1]) <= 8)
  assert numpy.abs(graded[front, 2] - natural[front, 2]).max() > 5 and numpy.allclose(after[front, 2], natural[front, 2], atol=1e-3)


def testATurnedOrResizedGradedPlotIsGradedUnderItsNewFootprint(stageBlenderServer):
  async def steps(session):
    await slopedPlots(session, [("1 Low Lane", [0, 0], 0)])
    natural = await groundVertices(session)
    await session.expectSuccess("gradePlot", {"address": "1 Low Lane", "objectName": "ground"})
    turned = await session.expectSuccess("editPlot", {"address": "1 Low Lane", "facingDegrees": 20})
    turnedAssessed = await session.expectSuccess("assessPlot", {"address": "1 Low Lane"})
    afterTurn = await groundVertices(session)
    resized = await session.expectSuccess("editPlot", {"address": "1 Low Lane", "size": [130, 150]})
    resizedAssessed = await session.expectSuccess("assessPlot", {"address": "1 Low Lane"})
    afterResize = await groundVertices(session)
    return natural, turned, turnedAssessed, afterTurn, resized, resizedAssessed, afterResize

  natural, turned, turnedAssessed, afterTurn, resized, resizedAssessed, afterResize = stageBlenderServer.session(steps)
  # Level under the turned footprint; beyond its corners, its margin, and its batters' reach, the ground is as it was.
  assert turned["grading"]["pass"] == "grade 1 Low Lane" and turnedAssessed["under"]["unevenness"] == 0.0
  far = numpy.hypot(natural[:, 0], natural[:, 1]) > math.hypot(169.1, 170.1) / 2 + 15 + turned["grading"]["slopesReach"] + 8
  assert numpy.allclose(afterTurn[far, 2], natural[far, 2], atol=1e-3)
  assert numpy.abs(afterTurn[~far, 2] - natural[~far, 2]).max() > 5
  # Resized to 130 x 150, its border scales as a door's size does, by the tighter fit, to 77%, and its ground is level under it.
  assert resized["border"] == {"model": "OBP_LOTSQUARE", "size": 77, "borderAcross": 133.7, "borderAlong": 133.0}
  assert resized["size"] == [130.0, 150.0] and resizedAssessed["under"]["unevenness"] == 0.0
  assert numpy.allclose(afterResize[far, 2], natural[far, 2], atol=1e-3)


def testASectionDrawsPlotPadsAcrossTheirFootprintsInsideItsFrame(stageBlenderServer):
  async def steps(session):
    await slopedPlots(session, lowLane)
    for address, _, _ in lowLane:
      await session.expectSuccess("gradePlot", {"address": address, "objectName": "ground"})
    return await session.expectImage("renderSection", {"start": [0, -200], "end": [0, 200], "bottom": -70, "top": 70})

  image, cut = stageBlenderServer.session(steps)
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)

  def pixel(s, z):
    # 400 along and 140 up at 3.3 pixels a unit: s from x 60 to 1380, z -70 at y 636.
    return round(60 + 3.3 * s), round(636 - (z + 70) * 3.3)

  def near(colors, target):
    return numpy.abs(colors - numpy.array(target)).max(axis=-1) <= 30

  # Each pad runs level at its plot's height from its back edge to its entrance, 20 units from the street's middle.
  assert cut["plots"] == [
    {"name": "1 Low Lane", "s": [14.95, 185.05], "z": -18.0, "entrance": 185.05}, {"name": "2 Low Lane", "s": [224.95, 395.05], "z": 19.8, "entrance": 224.95},
  ]
  for entry in cut["plots"]:
    (left, row), (right, _) = pixel(entry["s"][0] + 2, entry["z"]), pixel(entry["s"][1] - 2, entry["z"])
    assert near(pixels[row - 2:row + 3, left:right], (150, 90, 20)).any(axis=0).all()
  # The ground runs on past both ends of the line; its profile stops at the frame.
  frame = slice(pixel(0, 70)[1], pixel(0, -70)[1] + 1)
  assert near(pixels[frame, 60:66], (110, 70, 40)).any() and near(pixels[frame, 1374:1380], (110, 70, 40)).any()
  assert not near(pixels[frame, :57], (110, 70, 40)).any() and not near(pixels[frame, 1384:], (110, 70, 40)).any()


def testAPlanMarksPlotEntrancesAndKeepsSpotHeightsOffTheirAddresses(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("setZoneHousing", decision)
    await session.expectSuccess("placePlot", {"address": "101 Test Street", "center": [0, 0], "facingDegrees": 90})
    return await session.expectImage("renderSketch", {"center": [0, 0], "width": 600, "layers": ["plots"]})

  image, _ = stageBlenderServer.session(steps)
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)

  def at(x, y):
    # 2.4 pixels a unit, the plan's center in the middle of its 1440 x 810: the 5 x 5 pixels about [x, y].
    column, row = round(720 + x * 2.4), round(405 - y * 2.4)
    return pixels[row - 2:row + 3, column - 2:column + 3]

  def near(colors, target, within=30):
    return numpy.abs(colors - numpy.array(target)).max(axis=-1) <= within

  # Facing east, its entrance mark points out of the middle of its east side, 84.55 out; nothing marks its west side.
  assert near(at(84.55 + 2.5, 0), (150, 90, 20)).any() and not near(at(-84.55 - 2.5, 0), (150, 90, 20)).any()
  # Its address sits on the grid crossing at its center, whose spot height gives way to it; the next crossing's dot is drawn.
  assert not near(at(0, 0), (0, 0, 0), 40).any() and near(at(100, 100), (0, 0, 0), 40).any()


def testLayOutPlotsNumbersEveryPlaceAlongTheStreet(stageBlenderServer, tmp_path):
  street = {"street": "Hill Road", "path": [[-360, 0], [0, 0], [300, 120]], "side": "both"}

  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneHousing", decision)
    laid = await session.expectSuccess("layOutPlots", street)
    again = await session.expectError("layOutPlots", street)
    haunted = await session.expectError("layOutPlots", {"street": "Low Road", "path": [[-300, -300], [300, -300]], "side": "left", "features": ["haunted"]})
    housing = await session.expectSuccess("getHousing", {})
    return laid, again, haunted, housing

  laid, again, haunted, housing = stageBlenderServer.session(steps)
  # On the inside of the bend the fifth place would overlap 103: it is skipped and its number left free, so each side keeps its own
  # odd or even numbers.
  assert [entry["address"] for entry in laid["placed"]] == ["101 Hill Road", "102 Hill Road", "103 Hill Road", "104 Hill Road", "106 Hill Road"]
  assert laid["skipped"] == [{"address": "105 Hill Road", "center": [56.4, 135.7], "reason": "overlaps ['103 Hill Road']"}]
  assert "Addresses ['101 Hill Road', '102 Hill Road', '103 Hill Road', '104 Hill Road', '106 Hill Road'] are taken" in again
  assert "Features ['haunted'] are not in the zone's pricing" in haunted and not any(plot["address"].endswith("Low Road") for plot in housing["plots"])
