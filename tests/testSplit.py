import sys
from pathlib import Path

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from buildTolerances import maxModelTriangles
from testModelsAndDressing import freshScene
from testWater import liquidMaterials

readMesh = """
import bmesh
editor = bmesh.new()
editor.from_mesh(bpy.data.objects[objectName].data)
uvs = editor.loops.layers.uv.active
result = {
  "positions": [[round(vertex.co.x, 3), round(vertex.co.y, 3), round(vertex.co.z, 3)] for vertex in editor.verts],
  "triangles": sum(len(face.verts) - 2 for face in editor.faces), "area": sum(face.calc_area() for face in editor.faces),
  "corners": None if uvs is None else sorted([round(loop.vert.co.x, 3), round(loop.vert.co.y, 3), round(loop[uvs].uv[0], 4), round(loop[uvs].uv[1], 4)] for face in editor.faces for loop in face.loops),
  "materials": [material.name for material in bpy.data.objects[objectName].data.materials],
}
editor.free()
"""

readMarks = """
import json
result = {
  sceneObject.name: json.loads(sceneObject["zonewrightSplit"]) for sceneObject in bpy.data.objects
  if "zonewrightSplit" in sceneObject and sceneObject.name.startswith(prefix)
}
"""

makeDiamond = """
import bmesh, math, mathutils
editor = bmesh.new()
bmesh.ops.create_grid(editor, x_segments=20, y_segments=20, size=100.0, calc_uvs=True)
bmesh.ops.rotate(editor, cent=(0, 0, 0), matrix=mathutils.Matrix.Rotation(math.radians(30), 3, 'Z'), verts=list(editor.verts))
mesh = bpy.data.meshes.new("diamond")
editor.to_mesh(mesh)
editor.free()
sceneObject = bpy.data.objects.new("diamond", mesh)
bpy.context.scene.collection.objects.link(sceneObject)
result = len(mesh.polygons)
"""


async def meshOf(session, name):
  return (await session.expectSuccess("runPython", {"code": f"objectName = {name!r}" + readMesh}))["result"]


async def marksOf(session, prefix):
  return (await session.expectSuccess("runPython", {"code": f"prefix = {prefix!r}" + readMarks}))["result"]


def testSplitCutsAGridIntoPartsThatFitAndJoinPutsItBackAsItWas(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "slab", "size": [200, 100], "spacing": 5, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("projectUVs", {"objectName": "slab", "method": "planar", "worldUnitsPerRepeat": 40, "direction": [0, 0, 1]})
    before = await meshOf(session, "slab")
    split = await session.expectSuccess("splitObject", {"name": "slab", "maxTriangles": 500})
    parts = [await meshOf(session, part["name"]) for part in split["parts"]]
    marks = await marksOf(session, "slab")
    details = [await session.expectSuccess("getObjectDetail", {"name": part["name"]}) for part in split["parts"]]
    joined = await session.expectSuccess("joinParts", {"name": "slab.part3"})
    after = await meshOf(session, "slab")
    gone = await session.expectError("getObjectDetail", {"name": "slab.part2"})
    marksAfter = await marksOf(session, "slab")
    return before, split, parts, marks, details, joined, after, gone, marksAfter

  before, split, parts, marks, details, joined, after, gone, marksAfter = stageBlenderServer.session(steps)
  assert before["triangles"] == 1600 and before["corners"] is not None and len(before["positions"]) == 41 * 21
  assert [part["name"] for part in split["parts"]] == ["slab", "slab.part2", "slab.part3", "slab.part4"]
  # The cuts fall in the gaps between columns of faces, so no face is cut: the four slabs are equal and add up to the whole grid.
  assert [part["triangles"] for part in split["parts"]] == [400, 400, 400, 400]
  assert sum(len(part["positions"]) for part in parts) == 41 * 21 + 3 * 21
  assert [numpy.array(part["positions"])[:, 0].min() for part in parts] == [-100.0, -50.0, 0.0, 50.0]
  assert [numpy.array(part["positions"])[:, 0].max() for part in parts] == [-50.0, 0.0, 50.0, 100.0]
  assert marks == {name: {"whole": "slab", "index": index, "count": 4} for index, name in enumerate(["slab", "slab.part2", "slab.part3", "slab.part4"], 1)}
  assert all(detail["collections"] == ["terrain"] for detail in details)
  assert all(part["materials"] == before["materials"] for part in parts)
  assert [detail["mesh"] for detail in details] == ["slab", "slab.part2", "slab.part3", "slab.part4"]
  assert joined["joined"] == 4 and joined["weldedVertices"] == 63 and joined["missingParts"] == [] and joined["triangles"] == 1600
  # Joined, the grid has its own vertices again and each corner its own texture coordinates.
  assert sorted(map(tuple, after["positions"])) == sorted(map(tuple, before["positions"]))
  assert after["corners"] == before["corners"]
  assert "No object named 'slab.part2'" in gone and marksAfter == {}


def testSplitCutsThroughFacesWhereTheyOverlapAndJoinWeldsTheCut(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    faces = (await session.expectSuccess("runPython", {"code": makeDiamond}))["result"]
    before = await meshOf(session, "diamond")
    split = await session.expectSuccess("splitObject", {"name": "diamond", "maxTriangles": 300})
    parts = [await meshOf(session, part["name"]) for part in split["parts"]]
    joined = await session.expectSuccess("joinParts", {"name": "diamond"})
    after = await meshOf(session, "diamond")
    return faces, before, split, parts, joined, after

  faces, before, split, parts, joined, after = stageBlenderServer.session(steps)
  assert faces == 400 and before["triangles"] == 800
  # A grid turned 30 degrees has no gap between its columns, so each cut goes through faces.
  assert all(part["triangles"] <= 300 for part in split["parts"]) and len(split["parts"]) >= 3
  assert sum(part["triangles"] for part in split["parts"]) > 800
  assert numpy.isclose(sum(part["area"] for part in parts), before["area"], rtol=1e-6)
  assert numpy.isclose(after["area"], before["area"], rtol=1e-6)
  # Every vertex on a cut was welded to the one across it, and only those: none left doubled.
  assert len(set(map(tuple, after["positions"]))) == len(after["positions"])
  assert joined["weldedVertices"] > 0


def testSplitKeepsConnectedPiecesTogetherWhereTheyFitAndApartWhereTheyDoNot(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    for name, size, location in (("first", [100, 100], [0, 0, 0]), ("second", [100, 100], [300, 0, 0]), ("third", [30, 30], [600, 0, 0])):
      await session.expectSuccess("createTerrainGrid", {"name": name, "size": size, "spacing": 10, "location": location})
    await session.expectSuccess("joinObjects", {"names": ["first", "second", "third"], "into": "first"})
    split = await session.expectSuccess("splitObject", {"name": "first", "maxTriangles": 250})
    parts = [await meshOf(session, part["name"]) for part in split["parts"]]
    return split, parts

  split, parts = stageBlenderServer.session(steps)
  # The first grid fills a part; the second cannot join it, and the small third fits beside the second.
  assert [part["triangles"] for part in split["parts"]] == [200, 218]
  assert numpy.array(parts[0]["positions"])[:, 0].max() <= 50 and numpy.array(parts[1]["positions"])[:, 0].min() >= 250


def testSplitIsRefusedWhereItWouldLeaveOrBreakSomething(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "slab", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    within = await session.expectError("splitObject", {"name": "slab", "maxTriangles": 200})
    badLimit = await session.expectError("splitObject", {"name": "slab", "maxTriangles": 0})
    await session.expectSuccess("duplicateObjects", {"names": ["slab"], "offset": [300, 0, 0], "linkData": True})
    shared = await session.expectError("splitObject", {"name": "slab", "maxTriangles": 100})
    await session.expectSuccess("createTerrainGrid", {"name": "plain", "size": [100, 100], "spacing": 10, "location": [0, 300, 0]})
    await session.expectSuccess("splitObject", {"name": "plain", "maxTriangles": 100})
    again = await session.expectError("splitObject", {"name": "plain", "maxTriangles": 50})
    joinWhole = await session.expectError("joinObjects", {"names": ["plain", "slab"], "into": "slab"})
    unsplit = await session.expectError("joinParts", {"name": "slab"})
    copy = await session.expectSuccess("duplicateObjects", {"names": ["plain.part2"], "offset": [0, 0, 300]})
    copyMarks = await marksOf(session, "plain.part2")
    return within, badLimit, shared, again, joinWhole, unsplit, copy, copyMarks

  within, badLimit, shared, again, joinWhole, unsplit, copy, copyMarks = stageBlenderServer.session(steps)
  assert "'slab' has 200 triangles, within 200; there is nothing to split" in within
  assert "maxTriangles is a whole number of triangles, 1 or more, got 0" in badLimit
  assert "'slab' shares its mesh with 1 other objects" in shared
  assert "'plain' is already split into 2 parts; joinParts puts them back as one" in again
  assert "'plain' is part of the split 'plain'; joinParts puts its parts back as one" in joinWhole
  assert "'slab' is not split into parts" in unsplit
  # A copy of a part is a mesh like any other, not another part of the whole.
  assert list(copy.values()) == ["plain.part2.001"] and list(copyMarks) == ["plain.part2"]


def testRenamingAndDeletingSplitParts(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "slab", "size": [200, 100], "spacing": 5, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("splitObject", {"name": "slab", "maxTriangles": 500})
    part = await session.expectError("organize", {"renames": {"slab.part2": "ledge"}})
    await session.expectSuccess("organize", {"renames": {"slab": "ridge"}, "collections": {"ridge": "rock"}})
    marks = await marksOf(session, "ridge")
    detail = await session.expectSuccess("getObjectDetail", {"name": "ridge.part3"})
    await session.expectSuccess("deleteObjects", {"names": ["ridge.part2"]})
    joined = await session.expectSuccess("joinParts", {"name": "ridge"})
    return part, marks, detail, joined

  part, marks, detail, joined = stageBlenderServer.session(steps)
  assert "'slab.part2' is part 2 of 'slab'; rename the whole by its name, or joinParts first" in part
  assert marks == {name: {"whole": "ridge", "index": index, "count": 4} for index, name in enumerate(["ridge", "ridge.part2", "ridge.part3", "ridge.part4"], 1)}
  # Every part moved to the new collection with the first, and a mesh named for its object.
  assert detail["collections"] == ["rock"] and detail["mesh"] == "ridge.part3"
  assert joined["joined"] == 3 and joined["missingParts"] == [2]


def testCheckExportRefusesAModelOverTheClientsLimitUntilItIsSplit(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "bigRock", "size": [300, 300], "spacing": 4, "location": [0, 0, 0]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "big.blend")})
    over = await session.expectSuccess("checkExport", {"path": str(tmp_path / "big.eqg"), "purpose": "test"})
    overForGame = await session.expectSuccess("checkExport", {"path": str(tmp_path / "big.eqg"), "purpose": "game"})
    split = await session.expectSuccess("splitObject", {"name": "bigRock"})
    await session.expectSuccess("saveFile", {})
    fitted = await session.expectSuccess("checkExport", {"path": str(tmp_path / "big.eqg"), "purpose": "test"})
    return over, overForGame, split, fitted

  over, overForGame, split, fitted = stageBlenderServer.session(steps)
  refusals = [failure for failure in over["failures"] if failure["failure"] == "model too large"]
  assert [(failure["object"], failure["triangles"], failure["limit"]) for failure in refusals] == [("bigRock", 11250, maxModelTriangles)]
  assert "splitObject cuts a mesh into parts that fit" in refusals[0]["message"]
  # A test export is refused too: the client draws nothing of the model whatever the export is for.
  assert [failure["object"] for failure in overForGame["failures"] if failure["failure"] == "model too large"] == ["bigRock"]
  # The limit is the default: four parts, each under it.
  assert len(split["parts"]) == 4 and all(part["triangles"] <= maxModelTriangles for part in split["parts"])
  assert not [failure for failure in fitted["failures"] if failure["failure"] == "model too large"]


def testARiverOverTheLimitIsBuiltInPartsAndEveryToolTakesItWhole(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [1200, 400], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-600, 0, -30], [600, 0, -30]], "radius": 40, "strength": 1, "profile": [[0, 0], [0.55, 0], [1, 30]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    river = await session.expectSuccess("runWater", {"name": "river", "path": [[-500, 0, -4], [500, 0, -8]], "reach": 40, "spacing": 6, "material": "water"})
    partDetails = [await session.expectSuccess("getObjectDetail", {"name": name}) for name in ("river", "river.part2")]
    water = await session.expectSuccess("getWater", {})
    marks = await marksOf(session, "river")
    partEdit = await session.expectError("editWater", {"name": "river.part2"})
    partDelete = await session.expectError("deleteObjects", {"names": ["river.part2"]})
    rebuilt = await session.expectSuccess("editWater", {"name": "river"})
    afterRebuild = await marksOf(session, "river")
    moved = await session.expectSuccess("transformObjects", {"names": ["river"], "translate": [0, 10, 0]})
    boxes = await session.expectSuccess("buildSwimVolumes", {"body": "river"})
    volumes = await session.expectSuccess("getSwimVolumes", {})
    await session.expectSuccess("editWater", {"name": "river"})
    rebuiltVolumes = await session.expectSuccess("getSwimVolumes", {})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "river.blend")})
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "river.eqg"), "purpose": "test"})
    await session.expectSuccess("deleteObjects", {"names": ["river"]})
    left = await marksOf(session, "river")
    return river, partDetails, water, marks, partEdit, partDelete, rebuilt, afterRebuild, moved, boxes, volumes, rebuiltVolumes, checked, left

  river, partDetails, water, marks, partEdit, partDelete, rebuilt, afterRebuild, moved, boxes, volumes, rebuiltVolumes, checked, left = stageBlenderServer.session(steps)
  assert river["triangles"] > maxModelTriangles and river["parts"] == 2
  assert all(0 < detail["triangles"] <= maxModelTriangles for detail in partDetails)
  # Each part is a model named for its mesh on export.
  assert [detail["mesh"] for detail in partDetails] == ["river", "river.part2"]
  assert [body["name"] for body in water["bodies"]] == ["river"] and water["bodies"][0]["parts"] == 2
  assert marks == {"river": {"whole": "river", "index": 1, "count": 2}, "river.part2": {"whole": "river", "index": 2, "count": 2}}
  assert "'river.part2' is part 2 of the water body 'river'" in partEdit and "'river.part2' is part 2 of the water body 'river'" in partDelete
  # Built again, a river has the same two parts, none left over from the last build.
  assert rebuilt["parts"] == 2 and afterRebuild == marks and moved["objects"][0]["name"] == "river"
  assert boxes["built"] > 0
  xs = [box["minimum"][0] for box in volumes["volumes"]] + [box["maximum"][0] for box in volumes["volumes"]]
  assert min(xs) < -400 and max(xs) > 400
  # The boxes were made over the whole surface, so building the river again as it was, in parts again, leaves them as they were.
  assert [body["state"] for body in volumes["bodies"]] == ["boxed"] and [body["state"] for body in rebuiltVolumes["bodies"]] == ["boxed"]
  assert not [failure for failure in checked["failures"] if failure["failure"] == "model too large"]
  assert left == {}
