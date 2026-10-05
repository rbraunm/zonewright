import io
import json
import math
import struct
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
from conftest import writePNG
from testCaves import caveCanyon, checkCave, borderEdges

caveMaterials = {"wallMaterial": "caveRock", "floorMaterial": "caveFloor", "worldUnitsPerRepeat": 48}
# What a view needs of the zone: its light, fog, and clip.
zone = {
  "ambientColor": [0.42, 0.42, 0.44], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.12], "sunColor": [0.62, 0.57, 0.48],
  "sunAzimuthDegrees": 150, "sunElevationDegrees": 40, "fogColor": [0.6, 0.65, 0.75], "fogStart": 800, "fogEnd": 5000, "fogDensity": 0.1,
  "fogOn": True, "maxClip": 6000, "newEngineZone": True,
}
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
# Each lining face's middle, normal, and material, and the lining's integrity problems as the cave checks find them.
readLiningFaces = r"""
import numpy
import bridgeCaves, bridgeMeshAccess
ground = bpy.data.objects['ground']
mask = bridgeMeshAccess.evaluateSelector({'cave': name}, ground, 'faces')
centers, normals, materials = bridgeMeshAccess.readFaceArrays(ground)
names = [slot.material.name for slot in ground.material_slots]
result = {
  'centers': centers[mask].tolist(), 'normals': normals[mask].tolist(), 'materials': [names[material] for material in materials[mask]],
  'problems': bridgeCaves.integrityProblems(ground),
}
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
    faces = (await session.expectSuccess("runPython", {"code": "name = 'room'\n" + readLiningFaces}))["result"]
    return cut, vertices, faces

  cut, vertices, faces = stageBlenderServer.session(steps)
  # The way's floor carries its own material, bordered on its exact edges; the floor round it keeps the cave's.
  centers, materials = numpy.array(faces["centers"]), numpy.array(faces["materials"])
  wayFaces = (centers[:, 1] + 60 > 160) & (centers[:, 1] + 60 < 300) & (centers[:, 0] > -10) & (centers[:, 0] < 14) & (centers[:, 2] < 2 + 1e-3)
  assert wayFaces.sum() > 0 and set(materials[wayFaces]) == {"pathStone"}
  assert "pathStone" not in set(materials[(centers[:, 0] < -10 - 1e-3) | (centers[:, 0] > 14 + 1e-3)])
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


def testRoughGroundStaysInsideItsOutlineAndBanksWhereAskedWithoutFolding(stageBlenderServer, tmp_path):
  banked = rubble | {"bank": 12, "material": "caveRock"}
  # On the room's straight line, finer than any cut: where the rubble's foot comes down past its outline's south side (y 120), on a line
  # every unit across it, how far out its ground reaches there; and its relief beside the way, every quarter unit out from the way's
  # side (14 across) at three places along it.
  besideTheWay = r"""
import numpy
import bridgeCaveRuns
strokes = bridgeCaveRuns.strokeDefinitions([way, rubble], ['main'], 8.0)
line = bridgeCaveRuns.CaveLine([[0, -60], [0, 250]], [120, 120], [70, 70]).setFloors([2, 2])
relief = bridgeCaveRuns.FloorRelief(strokes, line)
reaches = []
for x in numpy.arange(12.0, 69.0):
  ys = numpy.arange(120.0, 112.0, -0.05)
  weights = relief.roughWeights(relief.rough[0], numpy.column_stack([numpy.full(len(ys), x), ys]))
  reaches.append(float(120 - ys[numpy.flatnonzero(weights > 0)].min()))
alongs = numpy.array([200.0, 220.0, 240.0])
offsets = numpy.repeat(numpy.arange(14.0, 20.01, 0.25)[None, :], 3, axis=0)
plan = numpy.stack([offsets, numpy.repeat(alongs[:, None] - 60, offsets.shape[1], axis=1)], axis=2)
result = {'reaches': reaches, 'offsets': offsets[0].tolist(), 'relief': relief.relief(alongs, offsets, plan, numpy.full(3, 120.0)).tolist()}
"""

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await pathMaterial(session, tmp_path)
    cut = await session.expectSuccess("cutCave", room | {"breakup": None, "floor": [way, banked]})
    vertices = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    faces = (await session.expectSuccess("runPython", {"code": "name = 'room'\n" + readLiningFaces}))["result"]
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "room"})
    await session.expectSuccess("cutCave", room | {"breakup": None, "floor": [way, rubble]})
    unbanked = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    fine = (await session.expectSuccess("runPython", {"code": f"way = {way!r}\nrubble = {rubble!r}\n" + besideTheWay}))["result"]
    return cut, vertices, faces, unbanked, fine

  cut, vertices, faces, unbanked, fine = stageBlenderServer.session(steps)
  vertices = numpy.array(vertices)
  along, across, heights = vertices[:, 1] + 60, vertices[:, 0], vertices[:, 2]
  floor = heights < 2 + 12 + 1e-3
  points = vertices[:, :2]
  # Inside its outline relief is at most its rise but where it banks up the wall foot (60 across); beyond its edge none at all; inside
  # the way none at all.
  insideOutline = (points[:, 0] >= 10) & (points[:, 0] <= 70) & (points[:, 1] >= 120) & (points[:, 1] <= 220)
  dx = numpy.maximum(numpy.maximum(10 - points[:, 0], points[:, 0] - 70), 0)
  dy = numpy.maximum(numpy.maximum(120 - points[:, 1], points[:, 1] - 220), 0)
  beyond = floor & (numpy.hypot(dx, dy) > 6 + 1e-3) & (along > 150) & (along < 310) & (numpy.abs(across) < 59.5)
  assert beyond.sum() > 50 and numpy.abs(heights[beyond] - 2).max() <= 1e-4
  awayFromWall = floor & insideOutline & (across > 20) & (across < 60 - 12)
  assert (heights[awayFromWall] - 2).max() <= 6 + 1e-4 and (heights[awayFromWall] - 2).max() > 3
  # Its bank heaps up the wall to 12 at most, by its noise: 12 less twice its amplitude at least, and not one straight line along it.
  atWall = floor & insideOutline & (numpy.abs(across - 60) <= 1e-3)
  bank = heights[atWall] - 2
  assert atWall.sum() >= 5 and bank.max() <= 12 + 1e-4 and bank.min() >= 12 - 2 * 3 - 1e-4 and bank.max() - bank.min() > 1
  # Without a bank, the rubble at the wall stands no higher than its rise.
  unbanked = numpy.array(unbanked)
  unbankedAtWall = (unbanked[:, 2] < 2 + 12) & (numpy.abs(unbanked[:, 0] - 60) <= 1e-3) & (unbanked[:, 1] >= 120) & (unbanked[:, 1] <= 220)
  assert unbankedAtWall.sum() >= 5 and (unbanked[unbankedAtWall, 2] - 2).max() <= 6 + 1e-4
  inWay = floor & (along >= 160) & (along <= 300) & (across >= -10) & (across <= 14)
  assert numpy.abs(heights[inWay] - 2).max() <= 1e-4
  # Beside the way, it eases from nothing at the way's side over its edge: at most its rise times the ease that far out, so no blade
  # stands at the way's side.
  share = (numpy.array(fine["offsets"]) - 14) / 6
  besideWay = numpy.array(fine["relief"])
  assert numpy.abs(besideWay[:, 0]).max() == 0 and (besideWay - 6 * share * share * (3 - 2 * share)).max() <= 1e-9 and besideWay[:, -1].max() > 1
  # Its foot runs out past its outline as broken ground: from a quarter of its edge to its edge out, not one distance along the side.
  reaches = fine["reaches"]
  assert min(reaches) >= 6 / 4 - 0.05 and max(reaches) <= 6 + 1e-9 and max(reaches) - min(reaches) > 1
  # Its faces deep inside its outline carry its material; the floor beyond its edge keeps the cave's; the way keeps its own.
  centers, normals, materials = numpy.array(faces["centers"]), numpy.array(faces["normals"]), numpy.array(faces["materials"])
  floorFaces = (normals[:, 2] > 0.7) & (centers[:, 2] < 2 + 12)
  deepInside = floorFaces & (centers[:, 0] > 20) & (centers[:, 0] < 58) & (centers[:, 1] > 128) & (centers[:, 1] < 212)
  pastIt = floorFaces & (centers[:, 0] < -20) & (centers[:, 1] > 100) & (centers[:, 1] < 240)
  assert deepInside.sum() > 0 and set(materials[deepInside]) == {"caveRock"} and set(materials[pastIt]) == {"caveFloor"}
  assert [(stroke["name"], stroke.get("bank"), stroke.get("material")) for stroke in cut["floorStrokes"]] == [("way", None, None), ("rubble", 12.0, "caveRock")]
  # The wall rising from the bank never folds: no lining face of the room (short of its blind end's dome, which closes over its floor)
  # below where its walls stop rising straight faces down, and the cave's own checks find nothing wrong.
  low = (centers[:, 2] < 2 + 0.35 * 70) & (centers[:, 1] > 90) & (centers[:, 1] < 250)
  assert normals[low, 2].min() >= -1e-3 and faces["problems"] == []


def testAPadOverRubbleEasesIntoItWithoutAMoat(stageBlenderServer, tmp_path):
  rubbleEverywhere = {"kind": "rough", "name": "rubble", "run": "main", "outline": [[-60, 110], [60, 110], [60, 250], [-60, 250]], "rise": 6, "edge": 6, "breakup": {"featureSize": 32, "amplitude": 2, "seed": 3}}
  plotPad = {"kind": "pad", "name": "plotPad", "run": "main", "from": 200, "to": 260, "across": [-20, 20], "rise": 2, "edge": 20}
  across = list(range(-56, 57, 2))

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"breakup": None, "floor": [rubbleEverywhere, plotPad]})
    return (await session.expectSuccess("runPython", {"code": floorHeights([[x, 170, 30] for x in across])}))["result"]

  heights = numpy.array(stageBlenderServer.session(steps))
  offsets = numpy.abs(numpy.array(across))
  # Its top is level at 4; its side, 20 wide, runs from its top to the rubble round it (4 to 8), never dipping under both.
  assert numpy.abs(heights[offsets <= 20] - 4).max() <= 1e-3
  side = (offsets > 20) & (offsets < 40)
  assert side.sum() >= 16 and heights[side].min() >= 4 - 1e-3
  assert heights[offsets >= 40].min() >= 4 - 1e-3 and heights[offsets >= 40].max() <= 8 + 1e-3


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
    offRun = await session.expectError("cutCave", room | {"floor": [rubble | {"outline": [[150, 300], [200, 300], [200, 350], [150, 350]]}]})
    malformed = await session.expectError("cutCave", room | {"floor": [pad | {"edge": "wide"}]})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return outside, fine, banded, twice, offRun, malformed, detail

  outside, fine, banded, twice, offRun, malformed, detail = stageBlenderServer.session(steps)
  assert "Floor stroke 'way' reaches 10.0 past its run's left wall at 40.0 along it, where the floor is 40.0 wide" in outside
  assert "featureSize 20 is under twice the cave's edgeLength (16)" in fine and "edgeLength of 10 or less" in fine
  assert "The cave's floor strokes raise its right wall's foot" in banded and "within a step of the lowest trim band (4.0 over the floor)" in banded
  assert "Two floor strokes are named 'way'" in twice
  assert "Floor stroke 'rubble' (rough) lies wholly off its run 'main'" in offRun
  assert "Floor stroke 'dais''s edge is a number, got 'wide'" in malformed
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


# The room with a branch leaving its east wall at 200 (along 260) for a blind alcove, and one leaving its west wall at 160.
side = {"name": "side", "from": "main", "path": [[0, 200, 2], [90, 200], [150, 200]], "grades": [0, 0], "widths": [30, 30, 50], "heights": [30, 30, 40]}
west = {"name": "west", "from": "main", "path": [[0, 160, 2], [-80, 160], [-125, 160]], "grades": [0, 0], "widths": [30, 30, 50], "heights": [30, 30, 40]}
branched = room | {"name": "branched", "branches": [side]}


def checkCaveNamed(name):
  return checkCave.replace("hall", name)


def testABranchIsCutWithItsParentSealedAndWalkedThroughItsJunction(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", branched)
    checked = (await session.expectSuccess("runPython", {"code": checkCaveNamed("branched")}))["result"]
    vertices = (await session.expectSuccess("runPython", {"code": lining("branched")}))["result"]
    walk = await session.expectSuccess("walkRoute", {"cave": {"objectName": "ground", "name": "branched", "run": "side"}})
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "branched"})
    await session.expectSuccess("cutCave", branched | {"breakup": None})
    unbroken = (await session.expectSuccess("runPython", {"code": lining("branched")}))["result"]
    return cut, checked, vertices, walk, unbroken

  cut, checked, vertices, walk, unbroken = stageBlenderServer.session(steps)
  # Sealed: no edge on three or more faces and no open edge but the grid's border.
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges and checked["largestLiningMove"] == 0.0
  # The junction is where the branch leaves the room's east wall, framed facing back into the room.
  assert cut["junctions"] == [{
    "branch": "side", "from": "main", "start": [0.0, 200.0, 2.0], "rise": 0.0, "overlook": False,
    "frame": {"center": [60.0, 200.0, 2.0], "facingDegrees": 270.0, "width": 30.0, "height": 30.0},
  }]
  assert [(end["run"], end["end"], end["kind"]) for end in cut["ends"]] == [("main", "start", "open"), ("main", "end", "blind"), ("side", "start", "junction"), ("side", "end", "blind")]
  # It opens only at its mouth in front of the cliff: the branch breaks through nowhere.
  assert cut["openings"] and all(opening["middle"][1] < 0 for opening in cut["openings"])
  # Within half the fade of the opening the lining stands unbroken: the branch's within 8 of the room's wall, and the room's wall within 8
  # of the branch, are where the same cave cut without breakup puts them.
  vertices, unbroken = numpy.array(vertices), numpy.array(unbroken)
  x, y, z = vertices.T
  nearOpening = (numpy.abs(y - 200) < 15 + 8) & (((x > 60 + 1e-3) & (x < 68)) | ((numpy.abs(x - 60) < 3) & (z < 2 + 0.35 * 70)))
  distances = numpy.linalg.norm(vertices[nearOpening][:, None, :] - unbroken[None, :, :], axis=2).min(axis=1)
  assert nearOpening.sum() >= 10 and distances.max() <= 1e-4
  # Walked from the cave's mouth through the junction to the branch's end.
  assert walk["walkable"] is True and walk["problems"] == [] and walk["length"] > 260 + 150 - 1


def testEditingOneBranchLeavesTheOtherRunsAsCut(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", branched | {"branches": [side, west]})
    before = (await session.expectSuccess("runPython", {"code": lining("branched")}))["result"]
    edited = await session.expectSuccess("editCave", {"objectName": "ground", "name": "branched", "changes": {"branches": {"side": {"widths": [26, 26, 44]}}}})
    after = (await session.expectSuccess("runPython", {"code": lining("branched")}))["result"]
    removed = await session.expectSuccess("editCave", {"objectName": "ground", "name": "branched", "changes": {"branches": {"west": None}}})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return before, edited, after, removed, detail

  before, edited, after, removed, detail = stageBlenderServer.session(steps)
  # Everything west of the room's middle (the west branch and the room's west half), beyond the side branch's reach, is as it was cut.
  def westOf(vertices):
    vertices = numpy.array(vertices)
    kept = vertices[vertices[:, 0] < -10]
    return kept[numpy.lexsort(kept.T[::-1])]
  assert len(westOf(before)) > 100 and numpy.array_equal(westOf(before), westOf(after))
  assert [junction["frame"]["width"] for junction in edited["cut"]["junctions"]] == [26.0, 30.0]
  assert [junction["branch"] for junction in removed["cut"]["junctions"]] == ["side"]
  assert detail["caves"][0]["branches"] == ["side"]


def testTakingABranchedCaveBackLeavesTheGroundAsAnUncutCopyGivenTheSameChange(stageBlenderServer, tmp_path):
  readShown = r"""
import bridgeMeshAccess
result = {name: bridgeMeshAccess.readVertexArrays(bpy.data.objects[name])[0].tolist() for name in ('ground', 'control')}
"""

  async def steps(session):
    await caveCanyon(session, tmp_path)
    copies = await session.expectSuccess("duplicateObjects", {"names": ["ground"], "offset": [0, 0, 0]})
    await session.expectSuccess("organize", {"renames": {copies["ground"]: "control"}})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['control'].hide_render = True"})
    await session.expectSuccess("cutCave", branched | {"branches": [side, west]})
    for name in ("ground", "control"):
      await session.expectSuccess("setShapingPass", {"objectName": name, "name": "cliff", "strength": 1.1})
    removed = await session.expectSuccess("removeCave", {"objectName": "ground", "name": "branched"})
    shown = (await session.expectSuccess("runPython", {"code": readShown}))["result"]
    return removed, shown

  removed, shown = stageBlenderServer.session(steps)
  ground, control = numpy.array(shown["ground"]), numpy.array(shown["control"])
  assert ground.shape == control.shape and numpy.abs(ground - control).max() <= 1e-5
  assert [branch["name"] for branch in removed["definition"]["branches"]] == ["side", "west"]


def testBranchRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    outside = await session.expectError("cutCave", branched | {"branches": [side | {"path": [[80, 200, 2], [120, 200], [150, 200]]}]})
    hole = await session.expectError("cutCave", branched | {"branches": [side | {"path": [[0, 200, -1], [90, 200], [150, 200]]}]})
    raised = await session.expectError("cutCave", branched | {"branches": [side | {"path": [[0, 200, 6], [90, 200], [150, 200]]}]})
    twice = await session.expectError("cutCave", branched | {"branches": [side, side]})
    unknown = await session.expectError("cutCave", branched | {"branches": [side | {"from": "nowhere"}]})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return outside, hole, raised, twice, unknown, detail

  outside, hole, raised, twice, unknown, detail = stageBlenderServer.session(steps)
  assert "Branch 'side' starts at [80.0, 200.0, 2.0] with its first section reaching 20.7 out of its parent 'main''s walls (at [80.0, 200.0, 32.0])" in outside
  assert "Branch 'side''s floor where it starts" in hole and "lies 3.00 under its parent 'main''s floor there" in hole
  assert "Branch 'side' starts 4.0 over its parent 'main''s floor, more than a step (2)" in raised and "overlook" in raised
  assert "Two branches are named 'side'" in twice
  assert "Branch 'side' leaves 'nowhere', which is not the main run or a branch named before it (['main'])" in unknown
  assert detail["caves"] == []


def testABranchLeavesThroughItsParentsStrokesOnlyAsTheAuthorDrewIt(stageBlenderServer, tmp_path):
  # Rubble over the room's east side, which the side branch (at y 185 to 215) leaves through; a threshold held level from the room's
  # middle to its east wall where the branch leaves; a dais the branch can start on; and a ramp climbing at 12 degrees through y 150.
  rubbleEast = {"kind": "rough", "name": "rubbleEast", "run": "main", "outline": [[10, 120], [60, 120], [60, 250], [10, 250]], "rise": 6, "edge": 6, "breakup": {"featureSize": 32, "amplitude": 2, "seed": 3}}
  threshold = {"kind": "level", "name": "threshold", "run": "main", "from": 245, "to": 275, "across": [0, 60]}
  dais = {"kind": "pad", "name": "dais", "run": "main", "from": 230, "to": 290, "across": [-20, 59.5], "rise": 3, "edge": 0.5}
  ramp = {"objectName": "ground", "name": "ramp", "path": [[0, -60, 2], [0, 10, 2], [0, 250]], "grades": [None, 12], "widths": [40, 40, 60], "heights": [45, 45, 60], "breakup": None} | caveMaterials
  landed = ramp | {"path": [[0, -60, 2], [0, 10, 2], [0, 135], [0, 165], [0, 250]], "grades": [None, 12, 0, 12], "widths": [40] * 4 + [60], "heights": [45] * 4 + [60]}
  sideOfRamp = {"name": "side", "from": "main", "path": [[0, 150], [80, 150], [130, 150]], "grades": [0, 0], "widths": [24, 24, 40], "heights": [30, 30, 40]}
  onTheDais = side | {"path": [[0, 200], [90, 200], [150, 200]]}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    trench = await session.expectError("cutCave", branched | {"breakup": None, "floor": [rubbleEast]})
    throughThreshold = await session.expectSuccess("cutCave", branched | {"breakup": None, "floor": [rubbleEast, threshold]})
    walked = await session.expectSuccess("walkRoute", {"cave": {"objectName": "ground", "name": "branched", "run": "side"}})
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "branched"})
    onDais = await session.expectSuccess("cutCave", branched | {"breakup": None, "floor": [dais], "branches": [onTheDais]})
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "branched"})
    sloped = await session.expectError("cutCave", ramp | {"branches": [sideOfRamp]})
    level = await session.expectSuccess("cutCave", landed | {"branches": [sideOfRamp]})
    return trench, throughThreshold, walked, onDais, sloped, level

  trench, throughThreshold, walked, onDais, sloped, level = stageBlenderServer.session(steps)
  # Through rubble no one cleared, the branch would cut a trench; refused, naming the rubble and how to clear it.
  assert "Branch 'side' leaves its parent 'main' through its floor stroke(s) ['rubbleEast']" in trench and "Run a level way to where it leaves" in trench
  # Over a threshold the author drew, it leaves cleanly and is walked from the mouth.
  assert [junction["rise"] for junction in throughThreshold["junctions"]] == [0.0] and walked["walkable"] is True and walked["problems"] == []
  # Started without a height on a dais, it takes the dais's top and stands on it, no step over its parent's floor there.
  assert onDais["runs"]["side"]["floors"][0] == 5.0 and onDais["junctions"][0]["rise"] == 0.0
  # Off a ramp's slope it would leave a hole; refused with advice that works: a level stretch, which then cuts.
  assert "Branch 'side''s first section lies across its parent 'main''s floor where it climbs 5.1 over the branch's width" in sloped
  assert "Leave the parent where its floor is level across the branch's width" in sloped
  assert level["junctions"][0]["rise"] == 0.0 and level["runs"]["side"]["floors"][0] == level["runs"]["main"]["floors"][2]


# A branch leaving the room's west wall, climbing south at x -100 to 60, then running east over the tunnel at y 30 (its vault at 47).
over = {"name": "over", "from": "main", "path": [[-40, 150, 2], [-100, 150, 2], [-100, 30, 60], [60, 30, 60]], "widths": [30] * 4, "heights": [30] * 4}
# A second cave from its own approach 150 east, falling under the room (its floor at 2) to end blind beneath it.
cellar = {
  "objectName": "ground", "name": "cellar", "path": [[150, -60, 2], [150, 10, 2], [150, 80], [100, 160], [-40, 160]], "grades": [None, -28, -10, 0],
  "widths": [24] * 5, "heights": [24] * 5,
} | caveMaterials
# What a ray up from a point meets: each surface's height and whether it faces down.
rayUp = r"""
import mathutils
depsgraph = bpy.context.evaluated_depsgraph_get()
hits, origin = [], mathutils.Vector(start)
for _ in range(6):
  hit, location, normal, _, _, _ = bpy.context.scene.ray_cast(depsgraph, origin, mathutils.Vector((0, 0, 1)))
  if not hit:
    break
  hits.append([round(location.z, 3), normal.z < 0])
  origin = location + mathutils.Vector((0, 0, 1e-3))
result = hits
"""


def testABranchPassesOverItsParentWithTheRockBetween(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", room | {"name": "spiral", "breakup": None, "branches": [over]})
    hits = (await session.expectSuccess("runPython", {"code": "start = [0, 30, 3]\n" + rayUp}))["result"]
    checked = (await session.expectSuccess("runPython", {"code": checkCaveNamed("spiral")}))["result"]
    _, section = await session.expectImage("renderSection", {"start": [-40, -10], "end": [40, 70], "layers": ["ground", "caves"]})
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "spiral"})
    thin = await session.expectError("cutCave", room | {"name": "spiral", "breakup": None, "branches": [over | {"path": [[-40, 150, 2], [-100, 150, 2], [-100, 30, 51], [60, 30, 51]]}]})
    return cut, hits, checked, section, thin

  cut, hits, checked, section, thin = stageBlenderServer.session(steps)
  # Up from the tunnel's floor: its vault, facing down, then the branch's floor over it, met from below, with the rock between.
  (vault, vaultDown), (floor, floorDown) = hits[0], hits[1]
  assert vaultDown and not floorDown and abs(floor - 60) <= 0.01 and floor - vault >= 8
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges
  # Across the crossing the section cuts both as closed shapes, the branch's over the tunnel's.
  assert section["closedSpaces"] == 2
  boxes = {entry["run"]: entry["crossings"][0]["z"] for entry in section["caves"]}
  assert boxes["over"][0] - boxes["main"][1] >= 8
  assert "The cave's runs 'main' and 'over' come within 4." in thin and "less than minimumRock (8)" in thin


def testTwoCavesCrossInsideTheRockAndEachIsTakenBackAlone(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "eastApproach", "points": [[150, -140, 2], [150, -50, 2]], "width": 56})
    await session.expectSuccess("cutCave", room)
    await session.expectSuccess("cutCave", cellar)
    hits = (await session.expectSuccess("runPython", {"code": "start = [-20, 160, -60]\n" + rayUp}))["result"]
    mouths = await session.expectError("cutCave", room | {"name": "beside", "path": [[45, -60, 2], [45, 30, 6], [45, 150, 8]], "widths": [40] * 3, "heights": [45] * 3})
    roomBefore = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "cellar"})
    roomAfter = (await session.expectSuccess("runPython", {"code": lining("room")}))["result"]
    roomChecked = (await session.expectSuccess("runPython", {"code": checkCaveNamed("room")}))["result"]
    await session.expectSuccess("cutCave", cellar)
    cellarBefore = (await session.expectSuccess("runPython", {"code": lining("cellar")}))["result"]
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "room"})
    cellarAfter = (await session.expectSuccess("runPython", {"code": lining("cellar")}))["result"]
    cellarChecked = (await session.expectSuccess("runPython", {"code": checkCaveNamed("cellar")}))["result"]
    return hits, mouths, roomBefore, roomAfter, roomChecked, cellarBefore, cellarAfter, cellarChecked

  hits, mouths, roomBefore, roomAfter, roomChecked, cellarBefore, cellarAfter, cellarChecked = stageBlenderServer.session(steps)
  # Up from under the cellar: the cellar's floor, its vault, then the room's floor over it with the rock between.
  heights = [height for height, _ in hits]
  assert [down for _, down in hits[:3]] == [False, True, False] and heights[2] - heights[1] >= 8 and abs(heights[2] - 2) <= 1e-3
  assert "Cave 'beside' would reach the mouth of cave(s) ['room']" in mouths
  # Taking either back leaves the other's lining exactly as it was cut, and whole.
  def ordered(vertices):
    vertices = numpy.array(vertices)
    return vertices[numpy.lexsort(vertices.T[::-1])]
  assert numpy.array_equal(ordered(roomBefore), ordered(roomAfter)) and numpy.array_equal(ordered(cellarBefore), ordered(cellarAfter))
  for checked in (roomChecked, cellarChecked):
    assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges and checked["largestRingMiss"] <= 1e-4


def testAPlanDrawsACavesRunsWhereTheyRun(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("setZoneProperties", zone)
    await session.expectSuccess("cutCave", branched)
    plain, _ = await session.expectImage("renderSketch", {"center": [60, 120], "width": 400, "layers": [], "spotHeights": False})
    drawn, plan = await session.expectImage("renderSketch", {"center": [60, 120], "width": 400, "layers": ["caves"], "spotHeights": False})
    return plain, drawn, plan

  plain, drawn, plan = stageBlenderServer.session(steps)
  plain, drawn = (numpy.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=numpy.int64) for data in (plain, drawn))
  changed = numpy.abs(plain - drawn).max(axis=2) > 30
  height, width = changed.shape
  units = plan["unitsPerPixel"]

  def pixel(x, y):
    # North (+x) up and east (-y) right, the drawing's middle at the center.
    return int(round(width / 2 + (120 - y) / units)), int(round(height / 2 - (x - 60) / units))

  # The room's east wall (x 60) halfway along it and the branch's south wall (y 185, where it is 30 wide) are drawn; the hill far from
  # both is not.
  for x, y in ((60, 170), (78, 185)):
    column, row = pixel(x, y)
    assert changed[row - 3:row + 4, column - 3:column + 4].any(), (x, y)
  column, row = pixel(160, 20)
  assert not changed[row - 6:row + 7, column - 6:column + 7].any()


def testDaylightReachesExportAsBakedLight(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "daylit.eqg"
  # Each imported vertex's baked share of scene light (the alpha of its baked color) by position.
  readImported = r"""
import numpy
zone = bpy.data.objects['daylit']
mesh = zone.data
alphas = numpy.empty(len(mesh.vertices) * 4, dtype=numpy.float32)
mesh.color_attributes['eqColor'].data.foreach_get('color', alphas)
positions = numpy.empty(len(mesh.vertices) * 3)
mesh.vertices.foreach_get('co', positions)
positions = positions.reshape(-1, 3)
result = {'positions': numpy.round(positions, 3).tolist(), 'alphas': numpy.round(alphas.reshape(-1, 4)[:, 3], 4).tolist()}
"""

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"breakup": None, "daylight": [1, 1, 0.5, 0, 0]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "daylit.blend")})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    imported = (await session.expectSuccess("runPython", {"code": readImported}))["result"]
    return exported, imported

  exported, imported = stageBlenderServer.session(steps)
  archive = eqArchive.EQArchive(archivePath)
  terrain = eqgFiles.parseModel(archive.read("ter_daylit.ter"), "ter_daylit.ter")
  lit = archive.read("ter_daylit.lit")
  count = struct.unpack_from("<I", lit, 4)[0]
  colors = numpy.frombuffer(lit, dtype="<u4", count=count, offset=8)
  positions = numpy.array(terrain["vertices"])[:, :3]
  # One baked color per terrain vertex: no color, the share of scene light as alpha.
  assert lit[:4] == b"EQGP" and count == len(positions) and (colors & 0xFFFFFF).max() == 0
  alpha = colors >> 24
  # Full at the mouth and on all the ground, half at point 2 (60 north), none from point 3 on; eased between.
  x, y, z = positions.T
  onCenter = numpy.abs(x) < 1e-3
  assert alpha[(y < -21) | (z > 100)].min() == 255
  assert set(alpha[onCenter & (numpy.abs(y - 10) < 1e-3) & (numpy.abs(z - 2) < 1e-3)].tolist()) == {255}
  assert set(alpha[onCenter & (numpy.abs(y - 60) < 1e-3) & (numpy.abs(z - 2) < 1e-3)].tolist()) == {128}
  assert alpha[(y > 90 + 1e-3) & (z < 60)].max() == 0
  between = alpha[onCenter & (y > 10 + 1) & (y < 60 - 1) & (numpy.abs(z - 2) < 1e-3)]
  assert len(between) and between.min() > 128 and between.max() < 255
  assert exported["terrainBakedLight"] == "ter_daylit.lit"
  assert [entry["daylitCaves"] for entry in exported["toConfirm"]] == [["room"]]
  # Imported, the terrain carries the same shares as the share of scene light it takes.
  importedAlphas = {tuple(position): share for position, share in zip(imported["positions"], imported["alphas"])}
  matched = [abs(importedAlphas[tuple(numpy.round(position, 3))] - share / 255) for position, share in zip(positions, alpha) if tuple(numpy.round(position, 3)) in importedAlphas]
  assert len(matched) > 0.9 * len(positions) and max(matched) <= 1 / 255


def testDaylightDarkensTheCaveInTheClientsView(stageBlenderServer, tmp_path):
  deep ={"view": {"eye": [0, 130, 30], "target": [0, 250, 20]}}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("setZoneProperties", zone)
    outOfRange = await session.expectError("cutCave", room | {"daylight": [1, 1, 1.5, 0, 0]})
    miscounted = await session.expectError("cutCave", room | {"daylight": [1, 0]})
    await session.expectSuccess("cutCave", room | {"daylight": [1, 1, 0.5, 0, 0]})
    darkened, described = await session.expectImage("renderView", deep)
    await session.expectSuccess("editCave", {"objectName": "ground", "name": "room", "changes": {"daylight": None}})
    lit, _ = await session.expectImage("renderView", deep)
    return outOfRange, miscounted, darkened, described, lit

  outOfRange, miscounted, darkened, described, lit = stageBlenderServer.session(steps)
  assert "daylight shares" in outOfRange and "lie from 0 to 1" in outOfRange and "are 5 numbers" in miscounted

  def brightness(data):
    return float(numpy.asarray(Image.open(io.BytesIO(data)).convert("L"), dtype=numpy.float64).mean())

  # Deep in the room, past where its daylight falls to nothing, it takes no scene light: black without lamps. Without daylight set, the
  # same view is lit as the cliff outside is.
  assert described["daylight"]["daylitTerrain"] == ["ground"]
  assert brightness(darkened) < 1 and brightness(lit) > 20


def testAnAnchoredLightFollowsItsCave(stageBlenderServer, tmp_path):
  anchor = {"objectName": "ground", "cave": "room", "at": 230, "side": "right", "over": 10, "out": 3}
  readLight = "result = list(bpy.data.objects['LIB_lamp'].location)"

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"breakup": None})
    unseen = await session.expectError("placeLights", {"lights": [{"name": "LIB_lamp", "onCave": anchor, "color": [1, 0.6, 0.3], "radius": 60}]})
    unplaced = (await session.expectSuccess("runPython", {"code": "result = [o.name for o in bpy.data.objects if o.type == 'LIGHT']"}))["result"]
    await session.expectSuccess("setZoneProperties", zone)
    placed = await session.call("placeLights", {"lights": [{"name": "LIB_lamp", "onCave": anchor, "color": [1, 0.6, 0.3], "radius": 60}]})
    first = (await session.expectSuccess("runPython", {"code": readLight}))["result"]
    edited = await session.expectSuccess("editCave", {"objectName": "ground", "name": "room", "changes": {"widths": [40, 40, 40, 130, 130]}})
    second = (await session.expectSuccess("runPython", {"code": readLight}))["result"]
    inRock = await session.expectError("placeLights", {"lights": [{"name": "LIB_buried", "onCave": anchor | {"over": 80}, "color": [1, 1, 1], "radius": 30}]})
    both = await session.expectError("placeLights", {"lights": [{"name": "LIB_both", "onCave": anchor, "position": [0, 0, 0], "color": [1, 1, 1], "radius": 30}]})
    removed = await session.expectSuccess("removeCave", {"objectName": "ground", "name": "room"})
    return unseen, unplaced, placed, first, edited, second, inRock, both, removed

  unseen, unplaced, (placed, _), first, edited, second, inRock, both, removed = stageBlenderServer.session(steps)
  # An anchored light is looked at in a client-shaded view, so a zone that cannot draw one places none.
  assert "Zone properties missing" in unseen and unplaced == []
  # It stands 3 out from the room's east wall (60 east of its middle), 10 over its floor, and returns a view from the floor below it.
  assert [content.type for content in placed.content] == ["image", "text"], [content.text for content in placed.content if content.type == "text"]
  report = json.loads(placed.content[1].text)["anchored"][0]
  assert report["lightsTerrain"] is True and report["floorBelow"] == [0.0, 170.0, 2.0]
  assert numpy.allclose(first, [57, 170, 12], atol=0.01)
  # The room widened to 130, it stands at the new wall.
  assert numpy.allclose(second, [62, 170, 12], atol=0.01) and edited["cut"]["anchoredLights"] == [{"light": "LIB_lamp", "position": [62.0, 170.0, 12.0]}]
  assert "lies in rock or outside the cave" in inRock
  assert "Light 'LIB_both' is placed at a position or anchored on a cave's lining (onCave), one of them" in both
  assert removed["anchoredLightsLeft"] == ["LIB_lamp"]


def testASpiralExportsAndBothLevelsAreWalkedAfterImport(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "spiral.eqg"
  intoTheRoom = [[0, -60, 2], [0, 10, 2], [0, 60, 2], [0, 90, 2], [0, 240, 2]]
  overTheTunnel = [[0, -60, 2], [0, 150, 2], [-40, 150, 2], [-100, 150, 2], [-100, 30, 60], [50, 30, 60]]

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", room | {"name": "spiral", "breakup": None, "branches": [over]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "spiral.blend")})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    imported = await session.expectSuccess("getObjectDetail", {"name": "spiral"})
    walks = [await session.expectSuccess("walkRoute", {"path": path, "sampleSpacing": 4}) for path in (intoTheRoom, overTheTunnel)]
    return exported, imported, walks

  exported, imported, walks = stageBlenderServer.session(steps)
  assert imported["triangles"] == exported["terrainTriangles"]
  # Imported, the room under and the passage over the tunnel are both walked from the mouth.
  for walk in walks:
    assert walk["walkable"] is True and walk["problems"] == []
  assert walks[1]["profile"][-1]["at"][2] > 55


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
    await session.expectSuccess("setZoneProperties", zone)
    _, strip = await session.expectImage("renderRouteStrip", {"cave": {"objectName": "ground", "name": "landed"}, "spacing": 60}, "image/jpeg")
    return cut, measured, climb, walk, strip

  cut, measured, climb, walk, strip = stageBlenderServer.session(steps)
  # The strip stands a frame every 60 along the run, from its mouth at 2 to its landing and on at 30.
  assert strip["walkable"] is True and [frame["distance"] for frame in strip["frames"]] == [0.0, 60.0, 120.0, 180.0, 240.0]
  assert abs(strip["frames"][0]["view"]["standAt"][2] - 2) <= 0.5 and abs(strip["frames"][-1]["view"]["standAt"][2] - 30) <= 0.5
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
    underTheHill = await session.expectError("cutCave", graded | {"path": [[0, -60, 2], [0, 10, 2], [0, 300]], "grades": None, "widths": [40] * 3, "heights": [40] * 3})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    mouth = await session.expectSuccess("cutCave", room | {"path": [[0, -60], [0, 10, 2], [0, 60, 2], [0, 90, 2], [0, 250, 2]]})
    return twoWays, steep, atEnd, straight, unset, underTheHill, detail, mouth

  twoWays, steep, atEnd, straight, unset, underTheHill, detail, mouth = stageBlenderServer.session(steps)
  # An end left without a height under the hill would take the hilltop, the run climbing out through the plateau; a mouth left without
  # one on the approach takes the ground there.
  assert "The cave's end at [0.0, 300.0] has no height, so it took the ground's there" in underTheHill and "a trench" in underTheHill
  assert mouth["runs"]["main"]["floors"][0] == 2.0
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
