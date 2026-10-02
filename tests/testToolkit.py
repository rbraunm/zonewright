import itertools
import math

from conftest import writePNG

upFacing = {"facing": {"direction": [0, 0, 1], "withinDegrees": 10}}


async def heightsAt(session, points):
  measured = await session.expectSuccess("measure", {"points": [[x, y, 100] for x, y in points], "snapToSurface": True})
  return [point[2] for point in measured["points"]]


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def testPrimitivesAreBuiltToExactSize(stageBlenderServer):
  shapes = [
    ("plane", {}, (4, 1, 2), [10.0, 12.0, 0.0]),
    ("grid", {"divisions": [4, 4]}, (25, 16, 32), [10.0, 12.0, 0.0]),
    ("cube", {}, (8, 6, 12), [10.0, 12.0, 20.0]),
    ("cylinder", {"segments": 12}, (24, 14, 44), [10.0, 12.0, 20.0]),
    ("cone", {"segments": 12}, (13, 13, 22), [10.0, 12.0, 20.0]),
    ("sphere", {"segments": 12}, (62, 72, 120), [10.0, 12.0, 20.0]),
  ]

  async def steps(session):
    await freshScene(session)
    created = {}
    for kind, extra, _, _ in shapes:
      created[kind] = await session.expectSuccess("createPrimitive", {"kind": kind, "name": kind, "size": [10, 12, 20], "location": [0, 0, 5]} | extra)
    cubeDetail = await session.expectSuccess("getObjectDetail", {"name": "cube"})
    duplicateName = await session.expectError("createPrimitive", {"kind": "cube", "name": "cube", "size": [1, 1, 1], "location": [0, 0, 0]})
    missingSegments = await session.expectError("createPrimitive", {"kind": "cylinder", "name": "pipe", "size": [1, 1, 1], "location": [0, 0, 0]})
    return created, cubeDetail, duplicateName, missingSegments

  created, cubeDetail, duplicateName, missingSegments = stageBlenderServer.session(steps)
  for kind, _, (vertices, faces, triangles), dimensions in shapes:
    assert (created[kind]["vertices"], created[kind]["faces"], created[kind]["triangles"]) == (vertices, faces, triangles), kind
    assert created[kind]["dimensions"] == dimensions, kind
  assert cubeDetail["worldMinimum"] == [-5.0, -6.0, 5.0]
  assert cubeDetail["worldMaximum"] == [5.0, 6.0, 25.0]
  assert "An object named 'cube' already exists" in duplicateName
  assert "A cylinder needs segments of at least 3" in missingSegments


def testTerrainGridAndFineVertexMoves(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    grid = await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    uneven = await session.expectError("createTerrainGrid", {"name": "rough", "size": [100, 95], "spacing": 10, "location": [0, 0, 0]})
    moved = await session.expectSuccess("moveVertices", {
      "objectName": "ground", "selector": {"sphere": {"center": [0, 0, 0], "radius": 25}}, "offset": [0, 0, 10],
      "falloff": {"center": [0, 0, 0], "radius": 25, "curve": "linear"},
    })
    heights = await heightsAt(session, [(0, 0), (10, 0), (20, 0), (30, 0)])
    emptySelection = await session.expectError("moveVertices", {"objectName": "ground", "selector": {"sphere": {"center": [500, 0, 0], "radius": 5}}, "offset": [0, 0, 1]})
    return grid, uneven, moved, heights, emptySelection

  grid, uneven, moved, heights, emptySelection = stageBlenderServer.session(steps)
  assert (grid["vertices"], grid["faces"], grid["dimensions"]) == (121, 100, [100.0, 100.0, 0.0])
  assert "size 100 x 95 is not a whole number of 10-unit cells" in uneven
  assert heights == [10.0, 6.0, 2.0, 0.0]
  assert moved["largestMove"] == 10.0
  assert "matches no vertices of 'ground'" in emptySelection


def testSculptRaiseCarveSmoothAndCrease(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "hill", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "raise", "center": [0, 0, 0], "radius": 30, "strength": 10})
    raised = await heightsAt(session, [(0, 0), (10, 0), (40, 0)])
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "smooth", "center": [0, 0, 0], "radius": 30, "strength": 1, "iterations": 4})
    smoothed = await heightsAt(session, [(0, 0)])
    await session.expectSuccess("sculptAlongPath", {
      "objectName": "hill", "mode": "carve", "path": [[-100, -60, -5], [100, -60, -5]], "radius": 20, "strength": 1,
      "profile": [[0, 0], [1, 10]],
    })
    carved = await heightsAt(session, [(-30, -60), (-30, -50), (-30, -40)])
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "crease", "center": [60, 60, 0], "radius": 20, "strength": 4})
    creased = await heightsAt(session, [(60, 60)])
    carveWithoutProfile = await session.expectError("sculptAlongPath", {"objectName": "hill", "mode": "carve", "path": [[0, 0, 0], [10, 0, 0]], "radius": 5, "strength": 1})
    fractionTooBig = await session.expectError("sculptAtPoint", {"objectName": "hill", "mode": "flatten", "center": [0, 0, 0], "radius": 5, "strength": 2})
    return raised, smoothed, carved, creased, carveWithoutProfile, fractionTooBig

  raised, smoothed, carved, creased, carveWithoutProfile, fractionTooBig = stageBlenderServer.session(steps)
  closeness = 1 - 10 / 30
  assert raised[0] == 10.0
  assert abs(raised[1] - 10 * closeness * closeness * (3 - 2 * closeness)) < 0.001
  assert raised[2] == 0.0
  assert smoothed[0] < 10.0
  assert carved == [-5.0, 0.0, 0.0]
  assert creased[0] == -4.0
  assert "carve and fill need a profile" in carveWithoutProfile
  assert "flatten strength is a fraction in (0, 1]" in fractionTooBig


def testTopologyEdits(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "tower", "size": [10, 10, 10], "location": [0, 0, 0]})
    extruded = await session.expectSuccess("extrudeFaces", {"objectName": "tower", "selector": upFacing, "distance": 5})
    towerDetail = await session.expectSuccess("getObjectDetail", {"name": "tower"})
    inset = await session.expectSuccess("insetFaces", {"objectName": "tower", "selector": upFacing, "thickness": 1})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "block", "size": [10, 10, 10], "location": [30, 0, 0]})
    beveled = await session.expectSuccess("bevelEdges", {"objectName": "block", "selector": {"all": True}, "width": 0.5})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "tile", "size": [10, 10, 0], "location": [60, 0, 0]})
    subdivided = await session.expectSuccess("subdivide", {"objectName": "tile", "cuts": 3})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "doubled", "size": [10, 10, 0], "location": [90, 0, 0]})
    await session.expectSuccess("extrudeFaces", {"objectName": "doubled", "selector": {"all": True}, "distance": 0})
    cleaned = await session.expectSuccess("cleanupMesh", {"objectName": "doubled"})
    return extruded, towerDetail, inset, beveled, subdivided, cleaned

  extruded, towerDetail, inset, beveled, subdivided, cleaned = stageBlenderServer.session(steps)
  assert (extruded["vertices"], extruded["faces"]) == (12, 10)
  assert towerDetail["dimensions"] == [10.0, 10.0, 15.0]
  assert (inset["vertices"], inset["faces"]) == (16, 14)
  assert (beveled["beveledEdges"], beveled["vertices"], beveled["faces"]) == (12, 24, 26)
  assert (subdivided["vertices"], subdivided["faces"]) == (25, 16)
  assert cleaned["mergedVertices"] == 4
  assert (cleaned["after"]["vertices"], cleaned["after"]["faces"]) == (4, 1)


def testBooleanCutsAndRefusesToErase(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [20, 20, 20], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "bore", "size": [8, 8, 40], "location": [0, 0, -10], "segments": 16})
    cut = await session.expectSuccess("booleanCut", {"objectName": "wall", "cutterName": "bore"})
    cutterGone = await session.expectError("getObjectDetail", {"name": "bore"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pebble", "size": [2, 2, 2], "location": [100, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "sphere", "name": "dome", "size": [10, 10, 10], "location": [100, 0, -4], "segments": 16})
    erased = await session.expectError("booleanCut", {"objectName": "pebble", "cutterName": "dome"})
    pebble = await session.expectSuccess("getObjectDetail", {"name": "pebble"})
    dome = await session.expectSuccess("getObjectDetail", {"name": "dome"})
    return cut, cutterGone, erased, pebble, dome

  cut, cutterGone, erased, pebble, dome = stageBlenderServer.session(steps)
  assert cut["before"]["faces"] == 6
  assert cut["after"]["faces"] > 6
  assert "No object named 'bore'" in cutterGone
  assert "would leave 'pebble' with no faces; nothing was changed" in erased
  assert pebble["faces"] == 6
  assert pebble["modifiers"] == []
  assert dome["type"] == "MESH"


def testDecimateHalvesTriangles(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "field", "size": [100, 100], "spacing": 5, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "field", "mode": "raise", "center": [0, 0, 0], "radius": 40, "strength": 15})
    return await session.expectSuccess("decimate", {"objectName": "field", "ratio": 0.5})

  decimated = stageBlenderServer.session(steps)
  assert decimated["before"]["triangles"] == 800
  assert 0.45 * 800 <= decimated["after"]["triangles"] <= 0.55 * 800


def testMaterialsAndWorldScaledUVs(stageBlenderServer, tmp_path):
  rockTexture = writePNG(tmp_path / "rock.png", 8, 8, (100, 96, 90, 255))
  leafTexture = writePNG(tmp_path / "leaf.png", 8, 8, (40, 120, 30, 128))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [10, 10, 10], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "floor", "size": [64, 64, 0], "location": [0, 40, 0]})
    rock = await session.expectSuccess("createMaterial", {"name": "rockMaterial", "diffuseTexture": str(rockTexture)})
    leaf = await session.expectSuccess("createMaterial", {"name": "leafMaterial", "diffuseTexture": str(leafTexture), "cutout": True})
    missing = await session.expectError("createMaterial", {"name": "ghostMaterial", "diffuseTexture": str(tmp_path / "absent.png")})
    await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "rockMaterial"})
    topOnly = await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "leafMaterial", "selector": upFacing})
    await session.expectSuccess("projectUVs", {"objectName": "floor", "method": "planar", "worldUnitsPerRepeat": 32, "direction": [0, 0, 1]})
    await session.expectSuccess("projectUVs", {"objectName": "crate", "method": "box", "worldUnitsPerRepeat": 5})
    crate = await session.expectSuccess("getObjectDetail", {"name": "crate"})
    floor = await session.expectSuccess("getObjectDetail", {"name": "floor"})
    summary = await session.expectSuccess("getSceneSummary")
    return rock, leaf, missing, topOnly, crate, floor, summary

  rock, leaf, missing, topOnly, crate, floor, summary = stageBlenderServer.session(steps)
  assert rock == {"material": "rockMaterial", "diffuseTexture": "rock.png", "normalTexture": None, "cutout": False}
  assert leaf["cutout"] is True
  assert "absent.png' is not an existing absolute path" in missing
  assert "ghostMaterial" not in [material["name"] for material in summary["materials"]]
  assert topOnly == {"object": "crate", "material": "leafMaterial", "faces": 1, "slot": 1}
  assert crate["materials"] == [{"material": "rockMaterial", "faces": 5}, {"material": "leafMaterial", "faces": 1}]
  assert crate["worldUnitsPerTextureRepeat"] == 5.0
  assert floor["worldUnitsPerTextureRepeat"] == 32.0
  assert {"name": "rockMaterial", "textures": ["rock.png"]} in summary["materials"]


def testTransformDuplicateOrganizeAndMeasure(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [2, 2, 2], "location": [0, 0, 0]})
    moved = await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [5, 0, 0], "rotateDegrees": [0, 0, 90]})
    conflicting = await session.expectError("transformObjects", {"names": ["crate"], "translate": [1, 0, 0], "location": [0, 0, 0]})
    copies = await session.expectSuccess("duplicateObjects", {"names": ["crate"], "offset": [10, 0, 0], "linkData": True})
    beforeParenting = await session.expectSuccess("getObjectDetail", {"name": copies["crate"]})
    await session.expectSuccess("organize", {"renames": {copies["crate"]: "lid"}, "parents": {"lid": "crate"}})
    await session.expectSuccess("organize", {"collections": {"lid": "props"}})
    lid = await session.expectSuccess("getObjectDetail", {"name": "lid"})
    measured = await session.expectSuccess("measure", {"points": [[0, 0, 0], [3, 4, 0], [7, 4, 3]]})
    return moved, conflicting, copies, beforeParenting, lid, measured

  moved, conflicting, copies, beforeParenting, lid, measured = stageBlenderServer.session(steps)
  assert moved["objects"][0]["location"] == [5.0, 0.0, 0.0]
  assert moved["objects"][0]["rotationDegrees"][2] == 90.0
  assert "Pass translate or location, not both" in conflicting
  assert copies == {"crate": "crate.001"}
  assert beforeParenting["sharedMeshUsers"] == 2
  assert lid["parent"] == "crate"
  assert lid["collections"] == ["props"]
  assert lid["worldMinimum"] == beforeParenting["worldMinimum"]
  assert lid["worldMaximum"] == beforeParenting["worldMaximum"]
  assert [segment["distance"] for segment in measured["segments"]] == [5.0, 5.0]
  assert measured["segments"][1] == {"distance": 5.0, "horizontalDistance": 4.0, "heightChange": 3.0, "slopeDegrees": 36.87}
  assert measured["totalDistance"] == 10.0


def testPlaceOnSurfaceAndScatter(stageBlenderServer):
  scatter = {
    "sourceObject": "shrub", "region": {"circle": {"center": [-60, -60], "radius": 30}}, "density": 40, "minimumSpacing": 4,
    "scaleRange": [0.8, 1.2], "maximumSlopeDegrees": 30, "seed": 11, "surfaceObjects": ["ground"],
  }

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 40, "strength": 20, "falloff": "linear"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [2, 2, 2], "location": [0, 0, 100]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "barrel", "size": [2, 2, 2], "location": [0, 0, 0]})
    placed = await session.expectSuccess("placeOnSurface", {"objectNames": ["crate", "barrel"], "at": [[0, 0, 100], [20, 0, 100]], "alignToNormal": True, "surfaceObjects": ["ground"]})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "shrub", "size": [2, 2, 4], "location": [0, 0, -500], "segments": 6})
    first = await session.expectSuccess("scatterInRegion", scatter | {"collection": "shrubs"})
    second = await session.expectSuccess("scatterInRegion", scatter | {"collection": "shrubsAgain"})
    summary = await session.expectSuccess("getSceneSummary", {"objectLimit": 500})
    return placed, first, second, summary

  placements, first, second, summary = stageBlenderServer.session(steps)
  placed = placements["placements"]
  assert placed[0]["location"] == [0.0, 0.0, 20.0]
  assert placed[0]["surface"] == "ground"
  assert placed[1]["location"][2] == 10.0
  assert placed[1]["slopeDegrees"] > 0
  expectedCount = round(40 * math.pi * 30 * 30 / 10000)
  assert first["targetCount"] == expectedCount
  assert first["placed"] == expectedCount
  assert first["rejected"] == {"noSurface": 0, "tooSteep": 0, "nearAvoidedObject": 0}
  assert first["sharedMesh"] == "shrub"
  byCollection = {}
  for sceneObject in summary["objects"]:
    for collection in sceneObject["collections"]:
      byCollection.setdefault(collection, []).append(sceneObject["location"])
  shrubs, shrubsAgain = byCollection["shrubs"], byCollection["shrubsAgain"]
  assert len(shrubs) == expectedCount
  assert sorted(shrubs) == sorted(shrubsAgain)
  assert all(location[2] == 0.0 for location in shrubs)
  assert all(math.hypot(location[0] + 60, location[1] + 60) <= 30 for location in shrubs)
  assert min(math.dist(a[:2], b[:2]) for a, b in itertools.combinations(shrubs, 2)) >= 4


def testKitAssetsLinkAsRelativeLibraries(stageBlenderServer, tmp_path):
  kitPath = tmp_path / "kits" / "rocks.blend"
  kitPath.parent.mkdir()
  (tmp_path / "textures").mkdir()
  rockTexture = writePNG(tmp_path / "textures" / "rock.png", 8, 8, (100, 96, 90, 255))
  zonePath = tmp_path / "zones" / "zone.blend"
  zonePath.parent.mkdir()

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [4, 4, 3], "location": [0, 0, 0]})
    await session.expectSuccess("createMaterial", {"name": "rockMaterial", "diffuseTexture": str(rockTexture)})
    await session.expectSuccess("assignMaterial", {"objectName": "boulder", "materialName": "rockMaterial"})
    await session.expectSuccess("organize", {"collections": {"boulder": "boulderKit"}})
    marked = await session.expectSuccess("markAsset", {"collectionName": "boulderKit"})
    await session.expectSuccess("saveFile", {"path": str(kitPath)})
    await session.expectSuccess("newFile")
    linked = await session.expectSuccess("linkKitAsset", {"kitPath": str(kitPath), "assetName": "boulderKit", "instanceName": "boulderA", "location": [10, 0, 0]})
    unknown = await session.expectError("linkKitAsset", {"kitPath": str(kitPath), "assetName": "cliffKit", "instanceName": "cliffA", "location": [0, 0, 0]})
    instance = await session.expectSuccess("getObjectDetail", {"name": "boulderA"})
    await session.expectSuccess("saveFile", {"path": str(zonePath)})
    summary = await session.expectSuccess("getSceneSummary")
    return marked, linked, unknown, instance, summary

  marked, linked, unknown, instance, summary = stageBlenderServer.session(steps)
  assert marked == {"asset": "boulderKit", "objects": ["boulder"]}
  assert linked["location"] == [10.0, 0.0, 0.0]
  assert linked["asset"] == "boulderKit"
  assert "'cliffKit' is not a collection marked as an asset" in unknown
  assert "['boulderKit']" in unknown
  assert instance["instanceCollection"] == "boulderKit"
  assert summary["libraries"] == [{"name": "rocks.blend", "filePath": "//..\\kits\\rocks.blend"}]
  linkedImages = [image for image in summary["images"] if image["name"] == "rock.png"]
  assert [image["filePath"] for image in linkedImages] == ["//..\\textures\\rock.png"]


def testRunPythonCallsAreCounted(stageBlenderServer):
  async def steps(session):
    before = (await session.expectSuccess("getToolingStatus"))["runPython"]["calls"]
    await session.expectSuccess("runPython", {"code": "result = 1"})
    await session.expectSuccess("runPython", {"code": "result = 2"})
    after = await session.expectSuccess("getToolingStatus")
    return before, after["runPython"]

  before, after = stageBlenderServer.session(steps)
  assert after["calls"] == before + 2
  assert len(after["logFiles"]) >= 1
