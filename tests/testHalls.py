import numpy

import structurePlots
from conftest import writePNG

# A hall 24 wide and 20 tall from the plaza in front of the test plot's cliff (its foot at y 12) 50 into it, ending blind.
hall = {
  "objectName": "ground", "name": "hall", "path": [[60, 0, 0], [60, 50, 0]], "widths": [24, 24], "heights": [20, 20], "wallMaterial": "hallWall",
  "floorMaterial": "hallFloor", "worldUnitsPerRepeat": 12, "wallShare": 1,
}
bands = [
  {"fromFloor": 2, "height": 3, "material": "trimLow", "worldUnitsPerRepeat": 3},
  {"fromFloor": 16, "height": 2, "material": "trimHigh", "worldUnitsPerRepeat": 2},
]
# The face at y 16, where the cliff stands 30, framing the hall's mouth: x 43 to 77, z 0 to 26.
facade = {"objectName": "ground", "cave": "hall", "end": "start", "faceAt": 16, "width": 34, "height": 26, "apron": 10}
faceLine, faceLeft, faceRight, faceTop = 16.0, 43.0, 77.0, 26.0
# Every lining face of the hall: its unit normal, material, corners, and their UVs.
readLining = r"""
import numpy, bridgeMeshAccess, bridgeCaveData, bridgeSurfacing
ground = bpy.data.objects['ground']
mesh = ground.data
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
lining = bridgeCaveData.faceTags(ground, 'hall') == -1
normals = bridgeMeshAccess.faceNormals(ground, shown)
uvs = numpy.empty(len(mesh.loops) * 2)
mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_get('uv', uvs)
uvs = uvs.reshape(-1, 2)
names = [slot.material.name for slot in ground.material_slots]
tags = bridgeCaveData.attributeValues(mesh, 'zonewrightCaveVertex:hall')
result = {
  'faces': [
    {'normal': (normals[polygon.index] / numpy.linalg.norm(normals[polygon.index])).tolist(), 'material': names[polygon.material_index],
     'corners': shown[list(polygon.vertices)].tolist(), 'uvs': uvs[list(polygon.loop_indices)].tolist()}
    for polygon in mesh.polygons if lining[polygon.index]
  ],
  'liningVertices': shown[tags == -2].tolist(),
}
"""
# The ground's own faces (no cave's lining) whose middles lie in front of the cliff where the face is dressed, with unit normals and areas.
readFaceRegion = r"""
import numpy, bridgeMeshAccess, bridgeCaveData
ground = bpy.data.objects['ground']
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
lining = bridgeCaveData.faceTags(ground, 'hall') == -1
normals = bridgeMeshAccess.faceNormals(ground, shown)
result = []
for polygon in ground.data.polygons:
  area = float(numpy.linalg.norm(normals[polygon.index]))
  corners = shown[list(polygon.vertices)]
  middle = corners.mean(axis=0)
  if not lining[polygon.index] and area > 1e-6 and 38 < middle[0] < 82 and 4 < middle[1] < 24 and middle[2] < 40:
    result.append({'normal': (normals[polygon.index] / area).tolist(), 'area': area, 'corners': corners.tolist()})
"""
# Every vertex's place in the mesh as seen and which of the hall's it is (plug above 0, ring -1, lining -2, else 0).
readVertices = r"""
import numpy, bridgeMeshAccess, bridgeCaveData
ground = bpy.data.objects['ground']
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
name = 'zonewrightCaveVertex:hall'
tags = bridgeCaveData.attributeValues(ground.data, name) if name in ground.data.attributes else numpy.zeros(len(shown), dtype=numpy.int32)
result = {'shown': shown.tolist(), 'tags': tags.tolist()}
"""


async def hallPlot(session, folder):
  await structurePlots.testPlot(session, folder)
  for name, color in (("hallWall", (150, 140, 120, 255)), ("hallFloor", (90, 80, 70, 255)), ("trimLow", (40, 60, 120, 255)), ("trimHigh", (140, 40, 40, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(folder / f"{name}.png", 4, 4, color))})


def bandOf(corners):
  """The band all of a face's corners lie within (index), or None."""
  heights = numpy.array(corners)[:, 2]
  for index, band in enumerate(bands):
    if (heights >= band["fromFloor"] - 1e-3).all() and (heights <= band["fromFloor"] + band["height"] + 1e-3).all():
      return index
  return None


def assertBandsHold(lining):
  for face in lining["faces"]:
    index = bandOf(face["corners"])
    expected = bands[index]["material"] if index is not None and abs(face["normal"][2]) < 0.5 else None
    if expected is not None:
      assert face["material"] == expected, face
      band = bands[index]
      for corner, uv in zip(face["corners"], face["uvs"]):
        assert abs(uv[1] - (corner[2] - band["fromFloor"]) / band["worldUnitsPerRepeat"]) <= 1e-4, (face, band)
    else:
      assert face["material"] in ("hallWall", "hallFloor"), face


def testAHallHasStraightWallsAFlatCeilingAndAFlatBlindEnd(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    cut = await session.expectSuccess("cutCave", hall)
    lining = (await session.expectSuccess("runPython", {"code": readLining}))["result"]
    walk = await session.expectSuccess("walkRoute", {"path": [[60, -10, 0], [60, 46, 0]], "sampleSpacing": 2})
    return cut, lining, walk

  cut, lining, walk = stageBlenderServer.session(steps)
  assert [(end["end"], end["kind"], end["rounded"]) for end in cut["ends"]] == [("start", "open", False), ("end", "blind", False)]
  faces = lining["faces"]
  ceiling = [face for face in faces if face["normal"][2] < -0.5]
  floor = [face for face in faces if face["normal"][2] > 0.5]
  walls = [face for face in faces if abs(face["normal"][2]) <= 0.5]
  assert ceiling and floor and walls and len(ceiling) + len(floor) + len(walls) == len(faces)
  # The ceiling flat at the full height and facing straight down, the floor at the floor, every other face upright.
  assert max(abs(face["normal"][2] + 1) for face in ceiling) <= 1e-4 and max(abs(corner[2] - 20) for face in ceiling for corner in face["corners"]) <= 0.01
  assert max(abs(corner[2]) for face in floor for corner in face["corners"]) <= 0.01
  assert max(abs(face["normal"][2]) for face in walls) <= 1e-4
  # The side walls stand on the hall's edges, x 48 and 72; the blind end is one flat wall across at y 50, nothing of it beyond.
  sides = [face for face in walls if abs(face["normal"][0]) > 0.99]
  end = [face for face in walls if abs(face["normal"][1]) > 0.99]
  assert len(sides) + len(end) == len(walls)
  assert all(min(abs(corner[0] - 48), abs(corner[0] - 72)) <= 0.01 for face in sides for corner in face["corners"])
  assert end and all(abs(corner[1] - 50) <= 0.01 for face in end for corner in face["corners"])
  assert max(vertex[1] for vertex in lining["liningVertices"]) <= 50.01
  assert walk["walkable"] is True and walk["problems"] == [] and walk["lowestHeadroom"]["headroom"] == 20.0


def testTrimBandsRunAtTheirHeightsInEveryRow(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    cut = await session.expectSuccess("cutCave", hall | {"heights": [20, 26], "trimBands": bands})
    lining = (await session.expectSuccess("runPython", {"code": readLining}))["result"]
    return cut, lining

  cut, lining = stageBlenderServer.session(steps)
  assert [(band["material"], band["faces"] > 0) for band in cut["trimBands"]] == [("trimLow", True), ("trimHigh", True)]
  # Every row of the tube in the rock holds a vertex on each band edge of each wall, exactly at its height over the floor.
  edges = sorted({band["fromFloor"] + offset for band in bands for offset in (0, band["height"])})
  rows = {}
  for vertex in lining["liningVertices"]:
    for wall in (48, 72):
      if abs(vertex[0] - wall) <= 0.01:
        rows.setdefault((wall, round(vertex[1], 2)), []).append(vertex[2])
  full = {place: heights for place, heights in rows.items() if len(heights) >= len(edges)}
  assert len(full) >= 6
  for heights in full.values():
    assert all(min(abs(height - edge) for height in heights) <= 1e-3 for edge in edges)
  # Band faces carry their band's material and no others do; v runs from 0 at a band's bottom to its height over its repeat at its top.
  assertBandsHold(lining)
  bandFaces = [face for face in lining["faces"] if face["material"] in ("trimLow", "trimHigh")]
  assert sum(band["faces"] for band in cut["trimBands"]) == len(bandFaces)
  for band in bands:
    corners = [(corner, uv) for face in bandFaces if face["material"] == band["material"] for corner, uv in zip(face["corners"], face["uvs"])]
    bottoms = [uv[1] for corner, uv in corners if abs(corner[2] - band["fromFloor"]) <= 1e-3]
    tops = [uv[1] for corner, uv in corners if abs(corner[2] - band["fromFloor"] - band["height"]) <= 1e-3]
    assert bottoms and tops and max(abs(value) for value in bottoms) <= 1e-4
    assert max(abs(value - band["height"] / band["worldUnitsPerRepeat"]) for value in tops) <= 1e-4


def testTrimBandsComeBackOnEveryCut(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("cutCave", hall | {"trimBands": bands})
    widened = await session.expectSuccess("editCave", {"objectName": "ground", "name": "hall", "changes": {"widths": [28, 28]}})
    lining = (await session.expectSuccess("runPython", {"code": readLining}))["result"]
    return widened, lining

  widened, lining = stageBlenderServer.session(steps)
  assert [band["faces"] > 0 for band in widened["cut"]["trimBands"]] == [True, True]
  sides = {round(corner[0], 2) for face in lining["faces"] if abs(face["normal"][0]) > 0.99 for corner in face["corners"]}
  assert sides == {46.0, 74.0}
  assertBandsHold(lining)
  assert {face["material"] for face in lining["faces"]} == {"hallWall", "hallFloor", "trimLow", "trimHigh"}


def testHallRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("runPython", {"code": "bpy.data.materials.new('plainPaint')"})
    before = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    aboveWalls = await session.expectError("cutCave", hall | {"wallShare": 0.35, "trimBands": bands})
    overlapping = await session.expectError("cutCave", hall | {"trimBands": [bands[0], {"fromFloor": 4, "height": 2, "material": "trimHigh", "worldUnitsPerRepeat": 2}]})
    unmade = await session.expectError("cutCave", hall | {"trimBands": [bands[0] | {"material": "plainPaint"}]})
    tooMuchWall = await session.expectError("cutCave", hall | {"wallShare": 1.2})
    partInRock = await session.expectError("cutCave", hall | {"path": [[60, 14, 0], [60, 50, 0]]})
    after = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return before, aboveWalls, overlapping, unmade, tooMuchWall, partInRock, after, detail

  before, aboveWalls, overlapping, unmade, tooMuchWall, partInRock, after, detail = stageBlenderServer.session(steps)
  assert "Trim band 1 (16 to 18 over the floor) reaches above the walls' straight part at path point 0 [60.0, 0.0, 0.0]" in aboveWalls and "rise straight 7" in aboveWalls
  assert "Trim bands 0 (2 to 5 over the floor) and 1 (4 to 6) overlap" in overlapping
  assert "Trim band 0's material 'plainPaint' is not a material createMaterial made" in unmade
  assert "above 0 and at most 1" in tooMuchWall and "got 1.2" in tooMuchWall
  assert "The hall's start at [60.0, 14.0, 0.0] is part in the rock" in partInRock and "a hall's mouth stands in front of the cliff's face, which dressFacade dresses" in partInRock
  assert after == before and detail["caves"] == []


def facePlane(region):
  """The dressed face: the region's faces turned to the plaza (facing -y) whose middles lie within its rectangle."""
  return [
    face for face in region
    if face["normal"][1] < -0.99 and faceLeft < numpy.mean([corner[0] for corner in face["corners"]]) < faceRight
    and numpy.mean([corner[2] for corner in face["corners"]]) < faceTop
  ]


def assertFaceIsDressed(region):
  face = facePlane(region)
  corners = numpy.array([corner for polygon in face for corner in polygon["corners"]])
  # Flat on its plane, from the floor to its top, its sides straight up at its width's edges, and whole but for the hall's 24 x 20 mouth.
  assert numpy.abs(corners[:, 1] - faceLine).max() <= 0.01
  assert abs(corners[:, 2].min()) <= 0.01 and abs(corners[:, 2].max() - faceTop) <= 0.01
  for side in (faceLeft, faceRight):
    edge = corners[numpy.abs(corners[:, 0] - side) <= 0.01]
    assert abs(edge[:, 2].min()) <= 0.01 and abs(edge[:, 2].max() - faceTop) <= 0.01
  assert abs(corners[:, 0].min() - faceLeft) <= 0.01 and abs(corners[:, 0].max() - faceRight) <= 0.01
  assert abs(sum(polygon["area"] for polygon in face) - (34 * 26 - 24 * 20)) <= 0.5


def testAFacadeDressesTheCliffIntoAFlatFaceAndTheHallOpensOnIt(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    dressed = await session.expectSuccess("dressFacade", facade)
    region = (await session.expectSuccess("runPython", {"code": readFaceRegion}))["result"]
    vertices = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    walk = await session.expectSuccess("walkRoute", {"path": [[60, 8, 0], [60, 46, 0]], "sampleSpacing": 2})
    return dressed, region, vertices, detail, walk

  dressed, region, vertices, detail, walk = stageBlenderServer.session(steps)
  assertFaceIsDressed(region)
  assert dressed["frame"] == {"center": [60.0, 16.0, 0.0], "facingDegrees": 180.0, "width": 34.0, "height": 26.0}
  assert dressed["corners"] == [[43.0, 16.0, 0.0], [77.0, 16.0, 0.0], [77.0, 16.0, 26.0], [43.0, 16.0, 26.0]]
  assert dressed["refit"]["cut"]["openings"][0]["middle"][1] == 16.0 and dressed["staleCaves"] == []
  # The apron in front of the face, 10 deep and the face's width, level at the floor.
  shown = numpy.array(vertices["shown"])
  apron = shown[(shown[:, 0] >= faceLeft) & (shown[:, 0] <= faceRight) & (shown[:, 1] >= faceLine - 10) & (shown[:, 1] < faceLine - 0.01)]
  assert len(apron) and numpy.abs(apron[:, 2]).max() <= 0.01
  assert [(cave["name"], cave["stale"]) for cave in detail["caves"]] == [("hall", False)]
  assert [(entry["facade"], entry["stale"]) for entry in detail["definedPasses"]] == [("hall start", False)]
  assert walk["walkable"] is True and walk["problems"] == []


def testRegradeReplaysAFacadeAndRefitsTheHall(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("dressFacade", facade)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "bump"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [74, 14, 0], "radius": 10, "strength": 3, "direction": [0, 0, 1]})
    bumped = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    region = (await session.expectSuccess("runPython", {"code": readFaceRegion}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return bumped, regraded, region, detail

  bumped, regraded, region, detail = stageBlenderServer.session(steps)
  assert [(entry["facade"], entry["stale"]) for entry in bumped["definedPasses"]] == [("hall start", True)]
  assert [(cave["name"], cave["stale"]) for cave in bumped["caves"]] == [("hall", True)]
  assert [summary["facade"] for summary in regraded["replayed"]] == ["hall start"] and [entry["cave"] for entry in regraded["refittedCaves"]] == ["hall"]
  assertFaceIsDressed(region)
  assert [(entry["facade"], entry["stale"]) for entry in detail["definedPasses"]] == [("hall start", False)]
  assert [(cave["name"], cave["stale"]) for cave in detail["caves"]] == [("hall", False)]


def testRemovingTheFacadePassTakesItBack(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    before = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    await session.expectSuccess("dressFacade", facade)
    removed = await session.expectSuccess("removeShapingPass", {"objectName": "ground", "name": "facade hall start"})
    after = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return before, removed, after, detail

  before, removed, after, detail = stageBlenderServer.session(steps)
  # The ground's own vertices (the cave's plug among them) stand where they stood before the facade; the hall is cut again to fit them.
  ground = numpy.flatnonzero(numpy.array(before["tags"]) >= 0)
  assert numpy.abs(numpy.array(after["shown"])[ground] - numpy.array(before["shown"])[ground]).max() <= 1e-4
  assert removed["removed"] == "facade hall start" and removed["refit"]["restored"]["restoredFaces"] > 0 and removed["staleCaves"] == []
  assert detail["definedPasses"] == [] and [(cave["name"], cave["stale"]) for cave in detail["caves"]] == [("hall", False)]


def testFacadeRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await hallPlot(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    before = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    blind = await session.expectError("dressFacade", facade | {"end": "end"})
    inFront = await session.expectError("dressFacade", facade | {"faceAt": 4})
    narrow = await session.expectError("dressFacade", facade | {"width": 25})
    low = await session.expectError("dressFacade", facade | {"height": 20.5})
    after = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return before, blind, inFront, narrow, low, after, detail

  before, blind, inFront, narrow, low, after, detail = stageBlenderServer.session(steps)
  assert "The cave's end at [60.0, 50.0, 0.0] is blind, not open" in blind
  assert "There is nothing to dress at [" in inFront and ", 8.0, 0.0]: the ground just behind the facade's line stands 0.0 there, under its top at 26.0" in inFront
  assert "width 25 does not frame the cave, 24.0 wide where the face stands: give at least 26.0" in narrow
  assert "height 20.5 does not frame the cave, 20.0 tall where the face stands: give at least 21.0" in low
  assert after == before and detail["definedPasses"] == []


# A cliff 100 tall rising from y 88 to y 100 across a 400-square grid of 4-unit cells; halls 24 wide and 20 tall run north into it from
# the open ground at y 70, each face dressed at y 92 where the cliff stands 33.
cliffBands = [{"fromFloor": 2, "height": 2.5, "material": "trimLow", "worldUnitsPerRepeat": 2.5}]
# Open edges and loose vertices away from the grid's border, and pairs of faces on the same corners.
readSoundness = r"""
import bmesh
ground = bpy.data.objects['ground']
editor = bmesh.new()
editor.from_mesh(ground.data)
inner = lambda vertex: abs(vertex.co.x) < 195 and abs(vertex.co.y) < 195
corners = {}
for face in editor.faces:
  corners.setdefault(frozenset(face.verts), []).append(face)
result = {
  'openEdges': sum(1 for edge in editor.edges if len(edge.link_faces) == 1 and all(inner(vertex) for vertex in edge.verts)),
  'looseVertices': sum(1 for vertex in editor.verts if not vertex.link_faces), 'doubledFaces': sum(1 for faces in corners.values() if len(faces) > 1),
}
editor.free()
"""
# The hall floor's faces: how many are not a cave's lining, and the lowest y of the named cave's lining floor faces' middles.
readHallFloor = r"""
import numpy, bridgeMeshAccess, bridgeCaveData
ground = bpy.data.objects['ground']
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
names = [slot.material.name for slot in ground.material_slots]
lining = bridgeCaveData.liningSelection(ground, True, 'faces')
own = bridgeCaveData.faceTags(ground, name) == -1
floors = [polygon for polygon in ground.data.polygons if names[polygon.material_index] == 'hallFloor']
result = {
  'notLining': sum(1 for polygon in floors if not lining[polygon.index]),
  'nearestLiningFloor': min(float(shown[list(polygon.vertices)].mean(axis=0)[1]) for polygon in floors if own[polygon.index]),
}
"""


async def cliffPlot(session, folder):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 400], "spacing": 4, "location": [100, 0, 0], "collection": "terrain"})
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-60, 100], [400, 100], [400, 400], [-60, 400]], "base": 0, "profile": [[-12, 0], [0, 100], [600, 100]]})
  for name, color in (("cliffGround", (120, 110, 90, 255)), ("hallWall", (150, 140, 120, 255)), ("hallFloor", (90, 80, 70, 255)), ("trimLow", (40, 60, 120, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(folder / f"{name}.png", 4, 4, color))})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "cliffGround"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "box", "worldUnitsPerRepeat": 12})


def cliffHall(name, x, **changes):
  return {
    "objectName": "ground", "name": name, "path": [[x, 70, 0], [x, 130, 0]], "widths": [24, 24], "heights": [20, 20], "wallMaterial": "hallWall",
    "floorMaterial": "hallFloor", "worldUnitsPerRepeat": 12, "wallShare": 1, "trimBands": cliffBands,
  } | changes


def cliffFacade(cave, **changes):
  return {"objectName": "ground", "cave": cave, "end": "start", "faceAt": 22, "width": 40, "height": 30, "apron": 14} | changes


def testTwoHallsStayFreshThroughEachOthersCutsAndRegrades(stageBlenderServer, tmp_path):
  async def steps(session):
    await cliffPlot(session, tmp_path)
    await session.expectSuccess("cutCave", cliffHall("hallP", 20))
    await session.expectSuccess("cutCave", cliffHall("hallQ", 140))
    cut = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    regrades = [await session.expectSuccess("regradeTerrain", {"objectName": "ground"}) for _ in range(2)]
    game = await session.expectSuccess("checkExport", {"path": str(tmp_path / "halls.eqg"), "purpose": "game"})
    overlap = await session.expectError("cutCave", cliffHall("hallOver", 40))
    floor = (await session.expectSuccess("runPython", {"code": "name = 'hallP'\n" + readHallFloor}))["result"]
    return cut, regrades, game, overlap, floor

  cut, regrades, game, overlap, floor = stageBlenderServer.session(steps)
  # Each cut leaves the other's ground as that one was cut: neither is stale, and regrading has nothing to refit.
  assert [(cave["name"], cave["stale"], cave["groundMoved"]["largest"]) for cave in cut["caves"]] == [("hallP", False, 0.0), ("hallQ", False, 0.0)]
  assert [regraded["refittedCaves"] for regraded in regrades] == [[], []]
  assert not [failure for failure in game["failures"] if failure["failure"] == "stale"]
  assert "Cave 'hallOver' would overlap cave(s) ['hallP'] in plan" in overlap
  # In front of the cliff the hall's floor runs level with the open ground there, which stays ground.
  assert floor["notLining"] == 0 and floor["nearestLiningFloor"] >= 88 - 4


def testTakingAFacadeBackLeavesTheGroundWholeToDressAgain(stageBlenderServer, tmp_path):
  async def steps(session):
    await cliffPlot(session, tmp_path)
    await session.expectSuccess("cutCave", cliffHall("hallA", 20))
    dressed = await session.expectSuccess("dressFacade", cliffFacade("hallA"))
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "halls.eqg"), "purpose": "test"})
    removed = await session.expectSuccess("removeShapingPass", {"objectName": "ground", "name": "facade hallA start"})
    sound = (await session.expectSuccess("runPython", {"code": readSoundness}))["result"]
    again = await session.expectSuccess("dressFacade", cliffFacade("hallA"))
    return dressed, checked, removed, sound, again

  dressed, checked, removed, sound, again = stageBlenderServer.session(steps)
  assert dressed["foldedFaces"] == 0 and dressed["remappedFaces"] > 0 and dressed["apron"]["deepestCut"] == 0.0
  # The dressed face and its returns are mapped where they stand; the hall's lining is surfaced by its cave, neither base nor a border.
  kinds = {finding["finding"] for finding in checked["findings"]}
  assert not kinds & {"texture stretched or squeezed", "zero texture area", "base material showing", "border without a transition"}, checked["findings"]
  assert removed["remappedFaces"] > 0 and removed["refit"] is not None
  assert sound == {"openEdges": 0, "looseVertices": 0, "doubledFaces": 0}
  assert again["foldedFaces"] == 0 and again["refit"] is not None


def testAFacadeBehindTheCliffIsRefusedAndOneTurnedToTheCliffFitsAnObliqueHall(stageBlenderServer, tmp_path):
  # A hall entering the cliff 22.7 degrees off square: a face square to it stands deep in the rock on its west side.
  oblique = {"path": [[100, 64, 0], [136, 150, 0]], "heights": [20, 26]}

  async def steps(session):
    await cliffPlot(session, tmp_path)
    await session.expectSuccess("cutCave", cliffHall("hallB", 100, **oblique))
    square = await session.expectError("dressFacade", cliffFacade("hallB", faceAt=39, height=34))
    turned = await session.expectSuccess("dressFacade", cliffFacade("hallB", faceAt=30, turnDegrees=-22.71))
    narrow = await session.expectError("dressFacade", cliffFacade("hallB", faceAt=30, turnDegrees=-22.71, width=27))
    return square, turned, narrow

  square, turned, narrow = stageBlenderServer.session(steps)
  assert "The ground in front of the face rises to" in square and "over the face's top at 34.0: the face stands behind the cliff's face there" in square
  assert round(turned["frame"]["facingDegrees"], 2) == 180.0 and turned["foldedFaces"] == 0
  # Turned to the cliff's line, the face runs along it, every corner at one y.
  corners = turned["corners"]
  assert max(corner[1] for corner in corners) - min(corner[1] for corner in corners) <= 0.01, corners
  assert "crossing the face turned -22.71 degrees on 26.0: give at least 28.0" in narrow


def testEditingADressedHallDressesItsFacadeAgainOrRefuses(stageBlenderServer, tmp_path):
  async def steps(session):
    await cliffPlot(session, tmp_path)
    await session.expectSuccess("cutCave", cliffHall("hallA", 20))
    await session.expectSuccess("dressFacade", cliffFacade("hallA"))
    wide = await session.expectError("editCave", {"objectName": "ground", "name": "hallA", "changes": {"widths": [42, 42]}})
    tall = await session.expectError("editCave", {"objectName": "ground", "name": "hallA", "changes": {"heights": [34, 34]}})
    moved = await session.expectSuccess("editCave", {"objectName": "ground", "name": "hallA", "changes": {"path": [[32, 70, 0], [32, 130, 0]]}})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return wide, tall, moved, detail

  wide, tall, moved, detail = stageBlenderServer.session(steps)
  assert "The facade at the cave's start would no longer frame it: width 40 does not frame the cave, 42.0 wide where the face stands" in wide
  assert "removeShapingPass 'facade hallA start'" in wide
  assert "height 30 does not frame the cave, 34.0 tall where the face stands" in tall
  assert [(entry["end"], entry["frame"]["center"]) for entry in moved["refitFacades"]] == [("start", [32.0, 92.0, 0.0])]
  assert [(entry["facade"], entry["stale"]) for entry in detail["definedPasses"]] == [("hallA start", False)]
  assert [(cave["name"], cave["stale"]) for cave in detail["caves"]] == [("hallA", False)]


def testAFacadeKeepsOffOtherCavesGroundAndARegradeNamesTheHandWorkItTakesBack(stageBlenderServer, tmp_path):
  async def steps(session):
    await cliffPlot(session, tmp_path)
    await session.expectSuccess("cutCave", cliffHall("hallA", 20))
    await session.expectSuccess("cutCave", cliffHall("hallB", 160))
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "bump"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [130, 74, 0], "radius": 8, "strength": 3})
    await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    reaching = await session.expectError("dressFacade", cliffFacade("hallA", blend=150))
    await session.expectSuccess("dressFacade", cliffFacade("hallA"))
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "touchUp"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [24, 82, 0], "radius": 8, "strength": 3})
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    return reaching, regraded

  reaching, regraded = stageBlenderServer.session(steps)
  assert "The facade's apron or blend reaches the ground within reach of cave(s) ['hallB']" in reaching
  (facade,) = [summary for summary in regraded["replayed"] if summary.get("facade") == "hallA start"]
  assert [entry["pass"] for entry in facade["overrodeHandWork"]] == ["touchUp"] and facade["overrodeHandWork"][0]["largestOffset"] > 0
