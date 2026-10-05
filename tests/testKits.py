import math
import sys
from pathlib import Path

from conftest import writePNG
import structurePlots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from playerScale import stepHeight

wallPiece = "testKitWall25"
doorFrame = {"width": 1.5, "depth": 0.5, "material": "testKitFrame", "worldUnitsPerRepeat": 2.5}


def socketPlaces(placement):
  return {socket["name"]: (socket["at"], socket["direction"]) for socket in placement["sockets"]}


def joins(placement):
  return {socket["name"]: socket["joinedTo"] for socket in placement["sockets"]}


def repeats(piece):
  return {entry["material"]: entry["worldUnitsPerRepeat"] for entry in piece["materials"]}


async def place(session, name, kitPath, piece=wallPiece, **arguments):
  return await session.expectSuccess("placeKitPiece", {"name": name, "kitPath": str(kitPath) if kitPath is not None else None, "piece": piece} | arguments)


async def snap(session, name, kitPath, target, socket="end", pieceSocket="start", piece=wallPiece):
  return await place(session, name, kitPath, piece, snapTo={"object": target, "socket": socket, "pieceSocket": pieceSocket})


async def kitPieceOf(session, name):
  return (await session.expectSuccess("getObjectDetail", {"name": name}))["kitPiece"]


async def addFrameMaterial(session, folder):
  texture = writePNG(folder / "textures" / "testKitFrame.png", 4, 4, (40, 30, 20, 255))
  await session.expectSuccess("createMaterial", {"name": "testKitFrame", "diffuseTexture": str(texture)})


async def handBlock(session, name, collection, size, location, material="testKitStone"):
  """A block modeled by hand into its own collection, ready to mark."""
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": size, "location": location})
  await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": material})
  await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 12.5})
  await session.expectSuccess("organize", {"collections": {name: collection}})


cornerSockets = [{"name": "start", "at": [-5, 0, 0], "direction": [-1, 0, 0]}, {"name": "end", "at": [0, 5, 0], "direction": [0, 1, 0]}]


async def flatZone(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0], "collection": "terrain"})


def testAKitPieceIsModeledToSizeWithItsOriginAtItsBaseCenterAndItsRolesAtTheirRepeat(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    pieces = {name: await kitPieceOf(session, name) for name in (wallPiece, "testKitBeam", "testKitPost", "testKitRope")}
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    placed = await place(session, "wall", kitPath, location=[0, 0, 0], facingDegrees=0)
    detail = await session.expectSuccess("getObjectDetail", {"name": "wall"})
    return pieces, placed, detail

  pieces, placed, detail = stageBlenderServer.session(steps)
  assert detail["worldMinimum"] == [-12.5, -5.0, 0.0] and detail["worldMaximum"] == [12.5, 5.0, 30.0]
  assert socketPlaces(placed) == {"start": ([-12.5, 0.0, 0.0], [-1.0, 0.0, 0.0]), "end": ([12.5, 0.0, 0.0], [1.0, 0.0, 0.0]), "top": ([0.0, 0.0, 30.0], [0.0, 0.0, 1.0])}
  assert placed["piece"]["triangles"] == 10 and placed["piece"]["module"] == 25.0 and placed["piece"]["size"] == [25.0, 10.0, 30.0]
  assert pieces[wallPiece]["bounds"] == [[-12.5, -5.0, 0.0], [12.5, 5.0, 30.0]]
  measured = {name: repeats(piece) for name, piece in pieces.items()}
  given = {wallPiece: {"testKitStone": 12.5, "testKitTrim": 5}, "testKitBeam": {"testKitTimber": 12, "testKitTrim": 12}, "testKitPost": {"testKitTimber": 10, "testKitTrim": 10}, "testKitRope": {"testKitRope": 4}}
  for name, expected in given.items():
    assert sorted(measured[name]) == sorted(expected), name
    assert all(abs(measured[name][material] - repeat) <= 0.01 for material, repeat in expected.items()), (name, measured[name])
  assert [(seam["material"], seam["along"], seam["repeats"]) for seam in pieces["testKitBeam"]["seams"]] == [("testKitTimber", "x", round(25 / 12, 4))]
  assert pieces[wallPiece]["seams"] == []


def testABarsGrainRunsAlongItsLengthAndAWallsAndPostsRunsUp(stageBlenderServer, tmp_path):
  readGradients = """
import numpy
found = {}
for name, normal in (('testKitBeam', (0, 1, 0)), ('testKitPlank', (0, 0, 1)), ('testKitRail', (0, 0, 1)), ('testKitPost', (0, 1, 0)), ('testKitWall25', (0, 1, 0))):
  mesh = bpy.data.objects[name].data
  polygon = next(polygon for polygon in mesh.polygons if numpy.dot(polygon.normal, normal) > 0.999)
  points = numpy.array([list(mesh.vertices[mesh.loops[loop].vertex_index].co) for loop in polygon.loop_indices])
  uvs = numpy.array([list(mesh.uv_layers.active.data[loop].uv) for loop in polygon.loop_indices])
  inPlane = numpy.ptp(points, axis=0) > 1e-6
  gradient = numpy.zeros((3, 2))
  gradient[inPlane] = numpy.linalg.lstsq(numpy.column_stack([points[:, inPlane], numpy.ones(len(points))]), uvs, rcond=None)[0][:-1]
  found[name] = [[round(float(value), 4) + 0.0 for value in row] for row in gradient.T]
result = found
"""

  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    return (await session.expectSuccess("runPython", {"code": readGradients}))["result"]

  gradients = stageBlenderServer.session(steps)
  assert gradients["testKitBeam"] == [[0.0, 0.0, round(1 / 12, 4)], [round(1 / 12, 4), 0.0, 0.0]]
  assert gradients["testKitPlank"] == [[0.0, 0.1, 0.0], [0.1, 0.0, 0.0]]
  assert gradients["testKitRail"] == [[0.0, 0.08, 0.0], [0.08, 0.0, 0.0]]
  assert gradients["testKitPost"] == [[0.1, 0.0, 0.0], [0.0, 0.0, 0.1]]
  assert gradients["testKitWall25"] == [[0.08, 0.0, 0.0], [0.0, 0.0, 0.08]]


def testKitPieceRefusals(stageBlenderServer, tmp_path):
  wall = {"kind": "wall", "size": [25, 10, 30], "location": [0, 0, 0], "materials": {"face": "testKitStone", "edge": "testKitTrim"}, "worldUnitsPerRepeat": {"face": 12.5, "edge": 5}}
  rope = {"kind": "ropeRail", "size": [25, 0, 1], "location": [0, 40, 0], "materials": {"rope": "testKitRope"}, "worldUnitsPerRepeat": {"rope": 4}}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    (tmp_path / "textures").mkdir()
    await structurePlots.testKitMaterials(session, tmp_path)
    unsaved = await session.expectError("createKitPiece", {"name": "testKitWall25"} | wall)
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "testKit.blend")})
    refusals = {
      "unsaved": unsaved,
      "stem": await session.expectError("createKitPiece", {"name": "wall25"} | wall),
      "kind": await session.expectError("createKitPiece", {"name": "testKitColumn"} | wall | {"kind": "column"}),
      "role": await session.expectError("createKitPiece", {"name": "testKitWall"} | wall | {"materials": {"face": "testKitStone"}}),
      "opaqueRope": await session.expectError("createKitPiece", {"name": "testKitRope"} | rope | {"materials": {"rope": "testKitStone"}}),
      "deepRope": await session.expectError("createKitPiece", {"name": "testKitRope"} | rope | {"size": [25, 0.5, 1]}),
    }
    await session.expectSuccess("createKitPiece", {"name": "testKitWall25"} | wall)
    refusals["taken"] = await session.expectError("createKitPiece", {"name": "testKitWall25"} | wall | {"location": [50, 0, 0]})
    summary = await session.expectSuccess("getSceneSummary")
    return refusals, summary

  refusals, summary = stageBlenderServer.session(steps)
  assert "never been saved" in refusals["unsaved"]
  assert "must start with the kit file's stem 'testKit'" in refusals["stem"]
  assert "kind must be one of" in refusals["kind"] and "'column'" in refusals["kind"]
  assert "exactly its roles ['face', 'edge']" in refusals["role"]
  assert "must be cutout" in refusals["opaqueRope"]
  assert "depth exactly 0" in refusals["deepRope"]
  assert "'testKitWall25' is already the name of" in refusals["taken"]
  assert summary["collections"] == ["testKitWall25"]


def testAHandModeledCollectionIsMarkedAtItsBaseCenterWithItsModuleBetweenItsSockets(stageBlenderServer, tmp_path):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "testKit.blend")})
    (tmp_path / "textures").mkdir()
    await structurePlots.testKitMaterials(session, tmp_path)
    await handBlock(session, "testKitSlabMesh", "testKitSlab", [25, 10, 30], [50, 20, 3])
    marked = await session.expectSuccess("markKitPiece", {"collectionName": "testKitSlab", "kind": "wall"})
    offset = (await session.expectSuccess("runPython", {"code": "result = list(bpy.data.collections['testKitSlab'].instance_offset)"}))["result"]
    await handBlock(session, "testKitCornerBlock", "testKitCorner", [10, 10, 30], [0, 60, 0])
    corner = await session.expectSuccess("markKitPiece", {"collectionName": "testKitCorner", "kind": "custom", "sockets": cornerSockets})
    await place(session, "slabPlaced", None, "testKitSlab", location=[0, -60, 0], facingDegrees=0, collection="testKitHouse")
    await session.expectSuccess("createLiquidMaterial", {"name": "testKitWet", "liquid": "waterfall", "diffuseTexture": str(tmp_path / "textures" / "testKitStone.png")})
    await handBlock(session, "testKitWetBlock", "testKitWet", [4, 4, 4], [0, 100, 0], "testKitWet")
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "testKitBareBlock", "size": [4, 4, 4], "location": [0, 120, 0]})
    await session.expectSuccess("assignMaterial", {"objectName": "testKitBareBlock", "materialName": "testKitStone"})
    await session.expectSuccess("organize", {"collections": {"testKitBareBlock": "testKitBare"}})

    def slabSockets(start, end):
      return [{"name": "start", "at": start[0], "direction": start[1]}, {"name": "end", "at": end[0], "direction": end[1]}]

    refusals = {
      "notMesh": await session.expectError("markKitPiece", {"collectionName": "testKitHouse", "kind": "custom"}),
      "material": await session.expectError("markKitPiece", {"collectionName": "testKitWet", "kind": "custom"}),
      "uvs": await session.expectError("markKitPiece", {"collectionName": "testKitBare", "kind": "custom"}),
      "tilted": await session.expectError("markKitPiece", {"collectionName": "testKitSlab", "kind": "wall", "sockets": slabSockets(([-12.5, 0, 0], [-0.6, 0, 0.8]), ([12.5, 0, 0], [1, 0, 0]))}),
      "outside": await session.expectError("markKitPiece", {"collectionName": "testKitSlab", "kind": "wall", "sockets": slabSockets(([-12.5, 0, 0], [-1, 0, 0]), ([15.5, 0, 0], [1, 0, 0]))}),
      "sameWay": await session.expectError("markKitPiece", {"collectionName": "testKitSlab", "kind": "wall", "sockets": slabSockets(([-12.5, 0, 0], [1, 0, 0]), ([12.5, 0, 0], [1, 0, 0]))}),
    }
    after = await kitPieceOf(session, "testKitSlabMesh")
    return marked, offset, corner, refusals, after

  marked, offset, corner, refusals, after = stageBlenderServer.session(steps)
  assert offset == [50.0, 20.0, 3.0]
  assert marked["module"] == 25.0 and marked["kind"] == "wall"
  assert [socket["name"] for socket in marked["sockets"]] == ["start", "end", "top"]
  assert marked["bounds"] == [[-12.5, -5.0, 0.0], [12.5, 5.0, 30.0]]
  assert corner["kind"] == "custom" and corner["sockets"] == [socket | {"at": [float(value) for value in socket["at"]], "direction": [float(value) for value in socket["direction"]]} for socket in cornerSockets]
  # A corner's sockets turn: the plan distance between them is no module.
  assert corner["module"] is None
  assert "['slabPlaced'], which are not meshes" in refusals["notMesh"]
  assert "Mesh 'testKitWetBlock' has 6 faces without a material createMaterial made" in refusals["material"]
  assert "Mesh 'testKitBareBlock' has no texture coordinates on its 6 faces" in refusals["uvs"]
  assert "neither level nor straight up or down" in refusals["tilted"]
  assert "Socket 'end' at [15.5, 0.0, 0.0] lies 3.00 outside" in refusals["outside"]
  assert "start and end must face apart" in refusals["sameWay"]
  assert after["sockets"] == marked["sockets"]


async def doorWall(session, tmp_path):
  kitPath = await structurePlots.testKit(session, tmp_path)
  await addFrameMaterial(session, tmp_path)
  cut = await session.expectSuccess("cutOpening", {"piece": wallPiece, "kind": "door", "along": 0, "width": 10, "height": 16, "frame": doorFrame})
  await session.expectSuccess("saveFile", {})
  return kitPath, cut


def testADoorIsCutWithItsFrameAndWalkedThroughUnderItsHeadroom(stageBlenderServer, tmp_path):
  readFrame = """
mesh = bpy.data.objects['testKitWall25'].data
offset = bpy.data.collections['testKitWall25'].instance_offset
slot = [index for index, material in enumerate(mesh.materials) if material.name == 'testKitFrame'][0]
framed = [polygon for polygon in mesh.polygons if polygon.material_index == slot]
ys = [mesh.vertices[index].co.y for polygon in framed for index in polygon.vertices]
result = {'faces': len(framed), 'front': max(ys), 'back': min(ys), 'others': max(mesh.vertices[index].co.y for polygon in mesh.polygons if polygon.material_index != slot for index in polygon.vertices)}
"""

  async def steps(session):
    kitPath, cut = await doorWall(session, tmp_path)
    frame = (await session.expectSuccess("runPython", {"code": readFrame}))["result"]
    piece = await kitPieceOf(session, wallPiece)
    await flatZone(session)
    await place(session, "door", kitPath, location=[0, 0, 0], facingDegrees=0)
    walk = await session.expectSuccess("walkRoute", {"path": [[0, 20, 0], [0, -20, 0]]})
    return cut, frame, piece, walk

  cut, frame, piece, walk = stageBlenderServer.session(steps)
  assert cut["clearWidth"] == 10.0 and cut["clearHeight"] == 16.0
  assert cut["corners"] == [[-5.0, 0.0], [5.0, 0.0], [5.0, 16.0], [-5.0, 16.0]]
  assert cut["triangles"]["before"] == 10 and cut["triangles"]["after"] > 10
  assert frame["faces"] > 0 and frame["front"] == 5.5 and frame["back"] == -5.5 and frame["others"] == 5.0
  assert abs(repeats(piece)["testKitStone"] - 12.5) <= 0.01
  assert abs(repeats(piece)["testKitFrame"] - 2.5) <= 0.01
  assert piece["openings"] == [{"kind": "door", "along": 0.0, "width": 10.0, "height": 16.0, "sill": 0.0, "archRise": None, "archSegments": 8, "frame": doorFrame}]
  assert walk["walkable"] is True, walk["problems"]
  assert abs(walk["lowestHeadroom"]["headroom"] - 16.0) <= 0.05


def testAnArchedWindowRisesToItsArchAndOverlapsAreRefused(stageBlenderServer, tmp_path):
  readTop = """
mesh = bpy.data.objects['testKitWall25'].data
result = max(vertex.co.z for vertex in mesh.vertices if abs(vertex.co.x) <= 4.0001 and vertex.co.z < 29)
"""

  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    window = await session.expectSuccess("cutOpening", {"piece": wallPiece, "kind": "window", "along": 0, "width": 8, "height": 8, "sill": 12, "archRise": 3})
    top = (await session.expectSuccess("runPython", {"code": readTop}))["result"]
    overlapping = await session.expectError("cutOpening", {"piece": wallPiece, "kind": "window", "along": 2, "width": 6, "height": 5, "sill": 14})
    nearEnd = await session.expectError("cutOpening", {"piece": wallPiece, "kind": "window", "along": 9, "width": 6, "height": 5, "sill": 12})
    piece = await kitPieceOf(session, wallPiece)
    return window, top, overlapping, nearEnd, piece

  window, top, overlapping, nearEnd, piece = stageBlenderServer.session(steps)
  assert abs(top - (12 + 8 + 3)) <= 1e-4
  assert window["clearHeight"] == 11.0 and window["clearWidth"] == 8.0
  assert "overlaps opening 0" in overlapping
  assert "leaves only 0.50 of wall at its +X end" in nearEnd and "0.50 too wide" in nearEnd
  assert len(piece["openings"]) == 1


def testARoofSitsOnItsFootprintAtItsPitchAndOverhangsEverySide(stageBlenderServer, tmp_path):
  roles = {"roof": "testKitTimber", "under": "testKitStone", "gable": "testKitFrame", "edge": "testKitTrim"}
  hipRoles = {role: material for role, material in roles.items() if role != "gable"}
  readRoof = """
import json
found = {}
for name in ('testKitGableRoof', 'testKitHipRoof', 'testKitShedRoof'):
  sceneObject = bpy.data.objects[name]
  mesh = sceneObject.data
  uvs = mesh.uv_layers.active.data
  faces = []
  for polygon in mesh.polygons:
    corners = [[*mesh.vertices[mesh.loops[loop].vertex_index].co, *uvs[loop].uv] for loop in polygon.loop_indices]
    faces.append({'material': mesh.materials[polygon.material_index].name, 'corners': corners})
  found[name] = faces
result = found
"""

  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await addFrameMaterial(session, tmp_path)
    common = {"pitchDegrees": 35, "overhang": 3, "thickness": 1, "footprint": [40, 30]}
    gable = await session.expectSuccess("addRoof", {"name": "testKitGableRoof", "kind": "gable", "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5), "location": [0, 200, 0]} | common)
    hip = await session.expectSuccess("addRoof", {"name": "testKitHipRoof", "kind": "hip", "materials": hipRoles, "worldUnitsPerRepeat": dict.fromkeys(hipRoles, 5), "location": [100, 200, 0]} | common)
    shed = await session.expectSuccess("addRoof", {"name": "testKitShedRoof", "kind": "shed", "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5), "location": [200, 200, 0]} | common)
    hipGable = await session.expectError("addRoof", {"name": "testKitHipRoof2", "kind": "hip", "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5), "location": [300, 200, 0]} | common)
    hipShort = await session.expectError("addRoof", {"name": "testKitHipRoof3", "kind": "hip", "materials": hipRoles, "worldUnitsPerRepeat": dict.fromkeys(hipRoles, 5), "location": [300, 200, 0], "ridgeAlong": "y"} | common)
    geometry = (await session.expectSuccess("runPython", {"code": readRoof}))["result"]
    for name, location, facing in (("north", [0, 340, 0], 0), ("south", [0, 300, 0], 0), ("west", [-17.5, 320, 0], 90), ("east", [17.5, 320, 0], 90)):
      await place(session, name, None, location=location, facingDegrees=facing)
    over = await session.expectSuccess("addRoof", {
      "name": "testKitOverRoof", "kind": "gable", "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5), "location": [0, 500, 0],
      "pitchDegrees": 35, "overhang": 3, "thickness": 1, "over": ["north", "south", "west", "east"], "ridgeAlong": "y",
    })
    return gable, hip, shed, hipGable, hipShort, geometry, over

  gable, hip, shed, hipGable, hipShort, geometry, over = stageBlenderServer.session(steps)
  rise = math.tan(math.radians(35))
  for described in (gable, hip, shed):
    assert described["bounds"][0][:2] == [-23.0, -18.0] and described["bounds"][1][:2] == [23.0, 18.0], described["roof"]
    assert [socket["name"] for socket in described["sockets"]] == ["plate"]
  gableFaces = geometry["testKitGableRoof"]
  under = [corner for face in gableFaces if face["material"] == "testKitStone" for corner in face["corners"]]
  assert abs(max(corner[2] for corner in under) - 15 * rise) <= 1e-4
  assert abs(min(corner[2] for corner in under) + 3 * rise) <= 1e-4
  assert abs(gable["eaveHeight"] + 3 * rise) <= 1e-4
  for name in ("testKitGableRoof", "testKitHipRoof", "testKitShedRoof"):
    for face in geometry[name]:
      if face["material"] != "testKitTimber":
        continue
      for first in face["corners"]:
        for second in face["corners"]:
          if abs(first[2] - second[2]) <= 1e-6:
            assert abs(first[4] - second[4]) <= 1e-5, (name, face)
  assert {face["material"] for face in geometry["testKitHipRoof"]} == {"testKitTimber", "testKitStone", "testKitTrim"}
  assert {face["material"] for face in geometry["testKitGableRoof"]} == {"testKitTimber", "testKitStone", "testKitTrim", "testKitFrame"}
  shedTop = [corner for face in geometry["testKitShedRoof"] if face["material"] == "testKitTimber" for corner in face["corners"]]
  back = max(corner[2] for corner in shedTop if corner[1] < 0)
  front = max(corner[2] for corner in shedTop if corner[1] > 0)
  assert abs((back - front) - 36 * rise) <= 1e-4
  assert "exactly its roles ['roof', 'under', 'edge']" in hipGable
  assert "ridge runs along the longer side" in hipShort
  assert over["footprint"] == [45.0, 50.0] and over["plateOver"] == [0.0, 320.0, 30.0]
  assert over["bounds"][0][:2] == [-25.5, -28.0] and over["bounds"][1][:2] == [25.5, 28.0]


def testPiecesSnapEndToEndExactlyAModuleApartAndTurnWithTheirSockets(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await handBlock(session, "testKitCornerBlock", "testKitCorner", [10, 10, 30], [400, 0, 0])
    await session.expectSuccess("markKitPiece", {"collectionName": "testKitCorner", "kind": "custom", "sockets": cornerSockets})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    first = await place(session, "wallA", kitPath, location=[0, 0, 0], facingDegrees=0)
    second = await snap(session, "wallB", kitPath, "wallA")
    third = await snap(session, "wallC", kitPath, "wallB")
    corner = await snap(session, "corner", kitPath, "wallC", piece="testKitCorner")
    turned = await snap(session, "wallD", kitPath, "corner")
    details = {name: await kitPieceOf(session, name) for name in ("wallA", "wallB", "wallC", "corner", "wallD")}
    return first, second, third, corner, turned, details

  first, second, third, corner, turned, details = stageBlenderServer.session(steps)
  assert [placement["location"] for placement in (first, second, third)] == [[0.0, 0.0, 0.0], [25.0, 0.0, 0.0], [50.0, 0.0, 0.0]]
  assert [placement["facingDegrees"] for placement in (first, second, third)] == [0.0, 0.0, 0.0]
  assert joins(details["wallA"]) == {"start": None, "end": {"object": "wallB", "socket": "start"}, "top": None}
  assert joins(details["wallB"]) == {"start": {"object": "wallA", "socket": "end"}, "end": {"object": "wallC", "socket": "start"}, "top": None}
  assert joins(details["wallC"]) == {"start": {"object": "wallB", "socket": "end"}, "end": {"object": "corner", "socket": "start"}, "top": None}
  assert corner["location"] == [67.5, 0.0, 0.0] and corner["facingDegrees"] == 0.0
  assert turned["facingDegrees"] == 270.0 and turned["location"] == [67.5, 17.5, 0.0]
  assert joins(details["wallD"]) == {"start": {"object": "corner", "socket": "end"}, "end": None, "top": None}


def testAPostSnapsOntoAWallsTopKeepingItsFacing(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await place(session, "wall", kitPath, location=[0, 0, 0], facingDegrees=45)
    kept = await snap(session, "post", kitPath, "wall", socket="top", pieceSocket="base", piece="testKitPost")
    await place(session, "otherWall", kitPath, location=[100, 0, 0], facingDegrees=0)
    given = await place(session, "otherPost", kitPath, "testKitPost", snapTo={"object": "otherWall", "socket": "top", "pieceSocket": "base"}, facingDegrees=30)
    return kept, given

  kept, given = stageBlenderServer.session(steps)
  assert kept["location"] == [0.0, 0.0, 30.0] and kept["facingDegrees"] == 45.0
  assert socketPlaces(kept)["base"][0] == [0.0, 0.0, 30.0]
  assert joins(kept)["base"] == {"object": "wall", "socket": "top"}
  assert given["location"] == [100.0, 0.0, 30.0] and given["facingDegrees"] == 30.0


def testSnapRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await place(session, "wallA", kitPath, location=[0, 0, 0], facingDegrees=0)
    await snap(session, "wallB", kitPath, "wallA")
    before = await session.expectSuccess("getSceneSummary")

    def snapping(socket, pieceSocket, **extra):
      return {"name": "wallC", "kitPath": str(kitPath), "piece": wallPiece, "snapTo": {"object": "wallA", "socket": socket, "pieceSocket": pieceSocket}} | extra

    refusals = {
      "joined": await session.expectError("placeKitPiece", snapping("end", "start")),
      "levelOnVertical": await session.expectError("placeKitPiece", snapping("top", "start")),
      "unknown": await session.expectError("placeKitPiece", snapping("side", "start")),
      "unknownOwn": await session.expectError("placeKitPiece", snapping("start", "corner")),
      "facing": await session.expectError("placeKitPiece", snapping("start", "end", facingDegrees=10)),
      "terrain": await session.expectError("placeKitPiece", snapping("start", "end", collection="terrain")),
      "missingPiece": await session.expectError("placeKitPiece", snapping("start", "end") | {"piece": "testKitWall99"}),
    }
    after = await session.expectSuccess("getSceneSummary")
    return refusals, before, after

  refusals, before, after = stageBlenderServer.session(steps)
  assert "'wallA''s socket 'end' is already joined to 'wallB''s 'start'" in refusals["joined"]
  assert "a level socket meets only a level one" in refusals["levelOnVertical"]
  assert "'wallA' has no socket 'side'; its sockets: ['start', 'end', 'top']" in refusals["unknown"]
  assert "has no socket 'corner'; its sockets: ['start', 'end', 'top']" in refusals["unknownOwn"]
  assert "takes no facingDegrees" in refusals["facing"]
  assert "not ground" in refusals["terrain"]
  assert "holds no piece 'testKitWall99'" in refusals["missingPiece"] and "testKitWall25" in refusals["missingPiece"]
  assert [entry["name"] for entry in after["objects"]] == ["wallA", "wallB"]
  assert after["collections"] == before["collections"] and after["libraries"] == before["libraries"]


def testThePlotStandsAtTheHeightsItsDefinitionGives(stageBlenderServer, tmp_path):
  points = {
    "west rim": ([-110, 0], 0.0), "gorge floor": ([-60, 30], -structurePlots.gorgeDepth), "gorge wall": ([-90, -30], -20.0),
    "cliff top": ([60, 100], structurePlots.cliffHeight), "cliff face": ([60, 16], structurePlots.cliffHeight / 2),
    "slope": ([55, -100], structurePlots.slopeHeight(-100)), "slope edge": ([2, -100], structurePlots.slopeHeight(-100)),
    "beside the slope": ([-8, -100], 0.0), "plaza": ([60, -20], 0.0), "under the cliff": ([60, 8], 0.0),
  }

  async def steps(session):
    await structurePlots.testPlot(session, tmp_path)
    return await session.expectSuccess("measure", {"points": [spot + [200] for spot, _ in points.values()], "snapToSurface": True})

  measured = stageBlenderServer.session(steps)
  for (name, (_, height)), point in zip(points.items(), measured["points"]):
    assert abs(point[2] - height) <= 0.01, (name, point)


def testAPieceGivenOnlyXYSettlesOntoTheLowestGroundUnderItsFootprint(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    placed = await place(session, "wall", kitPath, location=[55, -80], facingDegrees=0, depth=0.5)
    detail = await session.expectSuccess("getObjectDetail", {"name": "wall"})
    unsettled = await session.expectError("placeKitPiece", {"name": "wall2", "kitPath": str(kitPath), "piece": wallPiece, "location": [55, -80, 0], "facingDegrees": 0, "depth": 1})
    return placed, detail, unsettled

  placed, detail, unsettled = stageBlenderServer.session(steps)
  assert abs(detail["worldMinimum"][2] - (structurePlots.slopeHeight(-75) - 0.5)) <= 0.01
  assert abs(placed["settled"]["under"][0] - structurePlots.slopeHeight(-75)) <= 0.01
  assert "depth sinks a settled piece" in unsettled


def testASwapKeepsPlacementsAndJointsAndRefusesDifferentSockets(stageBlenderServer, tmp_path):
  wall = {"kind": "wall", "size": [25, 10, 30], "location": [100, 60, 0], "materials": {"face": "testKitStone", "edge": "testKitTrim"}, "worldUnitsPerRepeat": {"face": 12.5, "edge": 5}}

  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await session.expectSuccess("createKitPiece", {"name": "testKitWall25Door"} | wall)
    await session.expectSuccess("cutOpening", {"piece": "testKitWall25Door", "kind": "door", "along": 0, "width": 10, "height": 16})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await place(session, "wallA", kitPath, location=[10, 20, 0], facingDegrees=90)
    await snap(session, "wallB", kitPath, "wallA")
    await snap(session, "wallC", kitPath, "wallB")
    before = await kitPieceOf(session, "wallB")
    beforeDetail = await session.expectSuccess("getObjectDetail", {"name": "wallB"})
    swapped = await session.expectSuccess("swapKitPiece", {"names": ["wallB"], "piece": "testKitWall25Door"})
    after = await kitPieceOf(session, "wallB")
    afterDetail = await session.expectSuccess("getObjectDetail", {"name": "wallB"})
    refused = await session.expectError("swapKitPiece", {"names": ["wallA", "wallB"], "piece": "testKitWall12"})
    kept = await kitPieceOf(session, "wallA")
    summary = await session.expectSuccess("getSceneSummary")
    return before, beforeDetail, swapped, after, afterDetail, refused, kept, summary

  before, beforeDetail, swapped, after, afterDetail, refused, kept, summary = stageBlenderServer.session(steps)
  assert [library["name"] for library in summary["libraries"]] == ["testKit.blend"]
  assert sorted(name for name in summary["collections"] if name.startswith("testKit")) == [wallPiece, "testKitWall25Door"]
  assert swapped["placements"][0]["piece"]["piece"] == "testKitWall25Door"
  assert afterDetail["location"] == beforeDetail["location"] and afterDetail["rotationDegrees"] == beforeDetail["rotationDegrees"]
  assert joins(after) == joins(before) == {"start": {"object": "wallA", "socket": "end"}, "end": {"object": "wallC", "socket": "start"}, "top": None}
  assert after["triangles"] > before["triangles"]
  assert "'start' at [-12.5, 0.0, 0.0] on testKitWall25 but [-6.25, 0.0, 0.0] on testKitWall12" in refused
  assert kept["piece"] == wallPiece


def testAKitEditReachesEveryZoneThatLinksIt(stageBlenderServer, tmp_path):
  zonePath = tmp_path / "zones" / "zone.blend"
  zonePath.parent.mkdir()

  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    before = await place(session, "wall", kitPath, location=[0, 0, 0], facingDegrees=0)
    await session.expectSuccess("saveFile", {"path": str(zonePath)})
    await session.expectSuccess("openFile", {"path": str(kitPath)})
    cut = await session.expectSuccess("cutOpening", {"piece": wallPiece, "kind": "window", "along": 0, "width": 8, "height": 8, "sill": 12})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("openFile", {"path": str(zonePath)})
    after = await kitPieceOf(session, "wall")
    return before, cut, after

  before, cut, after = stageBlenderServer.session(steps)
  assert after["triangles"] - before["piece"]["triangles"] == cut["triangles"]["after"] - cut["triangles"]["before"] > 0
  assert after["fingerprint"] != before["piece"]["fingerprint"]


def testARailPieceIsPassableToWalks(stageBlenderServer, tmp_path):
  # Taller than a step, so only its passable flag lets a walk through it.
  barHeight = stepHeight + 2
  bar = {"size": [25, 0.8, barHeight], "materials": {"side": "testKitTimber", "end": "testKitTrim"}, "worldUnitsPerRepeat": {"side": 12.5, "end": 12.5}}

  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await session.expectSuccess("createKitPiece", {"name": "testKitTallRail", "kind": "rail", "location": [0, 80, 0]} | bar)
    await session.expectSuccess("createKitPiece", {"name": "testKitTallBeam", "kind": "beam", "location": [0, 100, 0]} | bar)
    await session.expectSuccess("saveFile", {})
    walks = {}
    for piece in ("testKitTallRail", "testKitTallBeam", wallPiece):
      await flatZone(session)
      await place(session, "across", kitPath, piece, location=[0, 0, 0], facingDegrees=0)
      walks[piece] = (await session.expectSuccess("walkRoute", {"path": [[0, 20, 0], [0, -20, 0]]}), await kitPieceOf(session, "across"))
    return walks

  walks = stageBlenderServer.session(steps)
  railWalk, rail = walks["testKitTallRail"]
  assert rail["passable"] is True and railWalk["walkable"] is True
  for piece, height in (("testKitTallBeam", barHeight), (wallPiece, 30.0)):
    walk, detail = walks[piece]
    assert detail["passable"] is False
    assert [problem["kind"] for problem in walk["problems"]] == ["rise"] and walk["problems"][0]["height"] == height, (piece, walk["problems"])


async def hallPlot(session, folder):
  """The test plot with a hall 24 wide and 20 tall cut from its plaza 50 into the cliff (its floor 0, its ceiling 20)."""
  await structurePlots.testPlot(session, folder)
  for name, color in (("hallWall", (150, 140, 120, 255)), ("hallFloor", (90, 80, 70, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(folder / f"{name}.png", 4, 4, color))})
  await session.expectSuccess("cutCave", {
    "objectName": "ground", "name": "hall", "path": [[60, 0, 0], [60, 50, 0]], "widths": [24, 24], "heights": [20, 20], "wallMaterial": "hallWall",
    "floorMaterial": "hallFloor", "worldUnitsPerRepeat": 12, "wallShare": 1,
  })


def testAColumnUpToAHallsCeilingSettlesOnTheHallsFloor(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await hallPlot(session, tmp_path)
    # A leg 10 tall held 10 over the floor: its top touches the ceiling.
    await place(session, "column", kitPath, "testKitLeg", location=[60, 40, 10], facingDegrees=0)
    settled = await session.expectSuccess("settleObjects", {"names": ["column"]})
    choice = await session.expectError("placeKitPiece", {"name": "column2", "kitPath": str(kitPath), "piece": "testKitLeg", "location": [52, 40], "facingDegrees": 0})
    return settled, choice

  settled, choice = stageBlenderServer.session(steps)
  (column,) = settled["settled"]
  assert column["location"] == [60.0, 40.0, 0.0] and column["under"] == [0.0, 0.0] and column["spans"] == [0.0, 10.0]
  assert "At [52, 40] 'ground' stands over the ground at [52.0, 40.0]: the highest ground there, at 60.0, is the top of 'ground', whose underside, at 20.0, stands over ground at 0.0" in choice
  assert "-0.0" not in choice


def testFramesStandProudOfTheWallOnceHoweverManyOpeningsItTakes(stageBlenderServer, tmp_path):
  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    await addFrameMaterial(session, tmp_path)
    window = {"piece": wallPiece, "kind": "window", "width": 6, "height": 8, "sill": 12, "frame": doorFrame}
    await session.expectSuccess("cutOpening", window | {"along": -6})
    await session.expectSuccess("cutOpening", window | {"along": 6})
    low = await session.expectError("cutOpening", {"piece": "testKitWall12", "kind": "window", "along": 0, "width": 4, "height": 4, "sill": 0.5, "frame": doorFrame})
    return low, await kitPieceOf(session, wallPiece)

  low, piece = stageBlenderServer.session(steps)
  assert piece["bounds"] == [[-12.5, -5.5, 0.0], [12.5, 5.5, 30.0]]
  assert [opening["along"] for opening in piece["openings"]] == [-6.0, 6.0]
  assert "The window with its frame reaches 1.00 past the wall's base; it needs at least 1 of wall below it, so it is 2.00 too wide" in low


def testARoofOverWallsIsMeasuredSquareToThemOnTheirBodies(stageBlenderServer, tmp_path):
  roles = {"roof": "testKitTimber", "under": "testKitStone", "edge": "testKitTrim"}

  def roof(name, over):
    return {"name": name, "kind": "hip", "pitchDegrees": 30, "overhang": 2, "thickness": 1, "over": over, "location": [0, 900, 0], "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5)}

  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    await addFrameMaterial(session, tmp_path)
    await session.expectSuccess("cutOpening", {"piece": wallPiece, "kind": "window", "along": 0, "width": 6, "height": 8, "sill": 12, "frame": doorFrame})
    await handBlock(session, "testKitCornerBlock", "testKitCorner", [10, 10, 30], [400, 0, 0])
    await session.expectSuccess("markKitPiece", {"collectionName": "testKitCorner", "kind": "custom", "sockets": cornerSockets})
    # A hut of four framed walls and four corners, the first wall turned 30 degrees, each piece snapped on to the last.
    names = ["hutWall1"]
    await place(session, "hutWall1", None, location=[0, 0, 0], facingDegrees=30)
    for index in range(1, 8):
      names.append(f"hut{'Corner' if index % 2 else 'Wall'}{index}")
      await snap(session, names[-1], None, names[-2], piece="testKitCorner" if index % 2 else wallPiece)
    measured = await session.expectSuccess("addRoof", roof("testKitHutRoof", names))
    await place(session, "askew", None, location=[200, 0, 0], facingDegrees=10)
    refused = await session.expectError("addRoof", roof("testKitAskewRoof", ["hutWall1", "askew"]))
    return measured, refused

  measured, refused = stageBlenderServer.session(steps)
  # Each wall's body is 25 long and 10 deep however proud its window's frame stands; with its corners the hut is 45 square.
  assert measured["footprint"] == [45.0, 45.0] and measured["facingOver"] == 30.0
  assert "'askew' stands turned +20.00 degrees against 'hutWall1'" in refused


def testAGableRoofsEndsAreSeenFromInsideAndOutside(stageBlenderServer, tmp_path):
  roles = {"roof": "testKitTimber", "under": "testKitStone", "gable": "testKitFrame", "edge": "testKitTrim"}
  readGables = """
found = {}
for name in names:
  mesh = bpy.data.objects[name].data
  slot = [index for index, material in enumerate(mesh.materials) if material.name == 'testKitFrame'][0]
  found[name] = [
    {'x': round(sum(mesh.vertices[index].co.x for index in polygon.vertices) / len(polygon.vertices), 4), 'normal': [round(value, 4) + 0.0 for value in polygon.normal]}
    for polygon in mesh.polygons if polygon.material_index == slot
  ]
result = found
"""

  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    await addFrameMaterial(session, tmp_path)
    common = {"pitchDegrees": 35, "overhang": 3, "thickness": 1, "footprint": [40, 30], "materials": roles, "worldUnitsPerRepeat": dict.fromkeys(roles, 5)}
    await session.expectSuccess("addRoof", {"name": "testKitGableRoof", "kind": "gable", "location": [0, 200, 0]} | common)
    await session.expectSuccess("addRoof", {"name": "testKitShedRoof", "kind": "shed", "location": [200, 200, 0]} | common)
    return (await session.expectSuccess("runPython", {"code": "names = ['testKitGableRoof', 'testKitShedRoof']\n" + readGables}))["result"]

  gables = stageBlenderServer.session(steps)
  # Each end wall over the plate is two faces on corners of their own, one looking out of the building and one into it.
  for name in ("testKitGableRoof", "testKitShedRoof"):
    ends = sorted((face["x"], face["normal"]) for face in gables[name])
    assert ends == [(-20.0, [-1.0, 0.0, 0.0]), (-20.0, [1.0, 0.0, 0.0]), (20.0, [-1.0, 0.0, 0.0]), (20.0, [1.0, 0.0, 0.0])], (name, ends)


def testACurvedWallSectionTakesAFramedWindow(stageBlenderServer, tmp_path):
  # A 22.5-degree arc of a tube 15 to 25 from its center, turned so its middle faces +Y, marked about the middle of its wall.
  turn = math.radians(22.5)
  wedge = [-35 * math.sin(turn), 35 * math.cos(turn), -10]
  start, end = math.radians(90 - 11.25), math.radians(90 + 11.25)
  sockets = [
    {"name": "start", "at": [20 * math.cos(start), 20 * math.sin(start) - 20, 0.0], "direction": [math.sin(start), -math.cos(start), 0.0]},
    {"name": "end", "at": [20 * math.cos(end), 20 * math.sin(end) - 20, 0.0], "direction": [-math.sin(end), math.cos(end), 0.0]},
  ]
  cutters = (("arcInner", "cylinder", [30, 30, 40], [0, 0, -5], [0, 0, 0]), ("arcSouth", "cube", [140, 70, 50], [0, -35, -10], [0, 0, 0]), ("arcWedge", "cube", [140, 70, 50], wedge, [0, 0, 22.5]))

  async def steps(session):
    await structurePlots.testKit(session, tmp_path)
    await addFrameMaterial(session, tmp_path)
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "testKitArcMesh", "size": [50, 50, 30], "location": [0, 0, 0], "segments": 64})
    for name, kind, size, location, rotation in cutters:
      await session.expectSuccess("createPrimitive", {"kind": kind, "name": name, "size": size, "location": location, "rotationDegrees": rotation} | ({"segments": 64} if kind == "cylinder" else {}))
      await session.expectSuccess("booleanCut", {"objectName": "testKitArcMesh", "cutterName": name})
    await session.expectSuccess("transformObjects", {"names": ["testKitArcMesh"], "rotateDegrees": [0, 0, 90 - 11.25]})
    await session.expectSuccess("assignMaterial", {"objectName": "testKitArcMesh", "materialName": "testKitStone"})
    await session.expectSuccess("projectUVs", {"objectName": "testKitArcMesh", "method": "box", "worldUnitsPerRepeat": 12.5})
    await session.expectSuccess("organize", {"collections": {"testKitArcMesh": "testKitArc"}})
    marked = await session.expectSuccess("markKitPiece", {"collectionName": "testKitArc", "kind": "custom", "origin": [0, 20, 0], "sockets": sockets})
    cut = await session.expectSuccess("cutOpening", {"piece": "testKitArc", "kind": "window", "along": 0, "width": 4, "height": 6, "sill": 12, "frame": doorFrame})
    return marked, cut, await kitPieceOf(session, "testKitArcMesh")

  marked, cut, piece = stageBlenderServer.session(steps)
  assert marked["module"] is None
  assert cut["clearWidth"] == 4.0 and cut["clearHeight"] == 6.0
  assert [opening["kind"] for opening in piece["openings"]] == ["window"] and "testKitFrame" in repeats(piece)


def testASnappedPieceSaysWhatGroundLiesUnderIt(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    await place(session, "rimWall", kitPath, location=[-5, -20], facingDegrees=0)
    out = await snap(session, "gorgeWall", kitPath, "rimWall", socket="start", pieceSocket="end")
    given = await place(session, "plazaWall", kitPath, location=[60, -20, 0], facingDegrees=0)
    return out, given

  out, given = stageBlenderServer.session(steps)
  # Snapped west off the rim, the wall runs from x -42.5 to -17.5 at the rim's height, over the gorge's wall falling to its floor at -40.
  assert out["footing"] == {"base": 0.0, "under": [-structurePlots.gorgeDepth, 0.0], "floats": structurePlots.gorgeDepth}
  assert given["footing"] == {"base": 0.0, "under": [0.0, 0.0], "floats": None}


def testPlacedPiecesStayUprightAtScaleOne(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testKit(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    await place(session, "slopeWall", kitPath, location=[55, -80], facingDegrees=90)
    await place(session, "wallA", kitPath, location=[60, -20, 0], facingDegrees=0)
    await snap(session, "wallB", kitPath, "wallA")
    before = await session.expectSuccess("getObjectDetail", {"name": "wallB"})
    refusals = {
      "tilt": await session.expectError("settleObjects", {"names": ["slopeWall"], "tiltShare": 1}),
      "scale": await session.expectError("transformObjects", {"names": ["wallB"], "scale": [1.5, 1.5, 1.5]}),
      "roll": await session.expectError("transformObjects", {"names": ["wallB"], "rotateDegrees": [0, 4, 0]}),
      "copy": await session.expectError("placeCopies", {"source": "wallA", "copies": [{"location": [0, 0, 0], "scale": 2}]}),
    }
    after = await session.expectSuccess("getObjectDetail", {"name": "wallB"})
    turned = await session.expectSuccess("transformObjects", {"names": ["wallB"], "rotateDegrees": [0, 0, 15]})
    return before, refusals, after, turned

  before, refusals, after, turned = stageBlenderServer.session(steps)
  assert "settle it with tiltShare 0" in refusals["tilt"]
  for action in ("scale", "roll"):
    assert "'wallB' is a placed kit piece, which stands upright at scale 1" in refusals[action] and "this transform would tilt or scale it" in refusals[action]
  assert "Copy 0 of placed kit piece 'wallA' is tilted or scaled" in refusals["copy"]
  assert [after[key] for key in ("location", "rotationDegrees", "scale")] == [before[key] for key in ("location", "rotationDegrees", "scale")]
  assert joins(after["kitPiece"])["start"] == {"object": "wallA", "socket": "end"}
  assert turned["objects"][0]["rotationDegrees"] == [0.0, 0.0, 15.0]
