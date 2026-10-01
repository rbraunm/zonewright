import math


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def testScaleFigureWalksAheadUntilAWall(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", {
      "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 300, "sunAzimuthDegrees": 0, "sunElevationDegrees": 45,
      "sunColor": [1, 1, 1], "sunStrength": 3, "ambientColor": [0.3, 0.3, 0.3], "newEngineZone": False,
    })
    _, open = await session.expectImage("renderView", {"view": {"standAt": [0, 0, 0], "headingDegrees": 0, "pitchDegrees": 0}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [40, 2, 20], "location": [0, 9, 0]})
    _, walled = await session.expectImage("renderView", {"view": {"standAt": [0, 0, 0], "headingDegrees": 0, "pitchDegrees": 0}})
    return open, walled

  open, walled = stageBlenderServer.session(steps)
  assert open["figure"] == [1.5, 15.0, 0.0]
  assert walled["figure"] == [1.5, 6.0, 0.0]


def testDeleteFacesInsideAnObject(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [25, 25, 20], "location": [0, 0, -10]})
    deleted = await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"insideObject": "boulder"}})
    everything = await session.expectError("deleteFaces", {"objectName": "boulder", "selector": {"all": True}})
    return deleted, everything

  deleted, everything = stageBlenderServer.session(steps)
  assert deleted["deletedFaces"] == 4
  assert deleted["faces"] == 96
  assert "matches every face of 'boulder'; delete the object instead" in everything


def testJoinObjectsMergesMeshesAndMaterials(stageBlenderServer, tmp_path):
  from conftest import writePNG
  barkTexture = writePNG(tmp_path / "bark.png", 4, 4, (90, 60, 30, 255))
  leafTexture = writePNG(tmp_path / "leaf.png", 4, 4, (30, 90, 30, 255))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createMaterial", {"name": "bark", "diffuseTexture": str(barkTexture)})
    await session.expectSuccess("createMaterial", {"name": "leaf", "diffuseTexture": str(leafTexture)})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "trunk", "size": [2, 2, 8], "location": [0, 0, 0], "segments": 6})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "canopy", "size": [10, 10, 20], "location": [0, 0, 6], "segments": 8})
    await session.expectSuccess("assignMaterial", {"objectName": "trunk", "materialName": "bark"})
    await session.expectSuccess("assignMaterial", {"objectName": "canopy", "materialName": "leaf"})
    joined = await session.expectSuccess("joinObjects", {"names": ["canopy", "trunk"], "into": "canopy"})
    trunkGone = await session.expectError("getObjectDetail", {"name": "trunk"})
    detail = await session.expectSuccess("getObjectDetail", {"name": "canopy"})
    return joined, trunkGone, detail

  joined, trunkGone, detail = stageBlenderServer.session(steps)
  assert joined["vertices"] == 9 + 12
  assert joined["materials"] == ["leaf", "bark"]
  assert detail["dimensions"] == [10.0, 10.0, 26.0]
  assert detail["materials"] == [{"material": "leaf", "faces": 9}, {"material": "bark", "faces": 8}]
  assert "No object named 'trunk'" in trunkGone


def testScatterAvoidsObjectsWithClearance(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "tower", "size": [20, 20, 40], "location": [0, 0, 0], "segments": 16})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "pine", "size": [4, 4, 10], "location": [0, 0, -500], "segments": 6})
    scattered = await session.expectSuccess("scatterInRegion", {
      "sourceObject": "pine", "region": {"circle": {"center": [0, 0], "radius": 50}}, "density": 40, "minimumSpacing": 5,
      "collection": "grove", "surfaceObjects": ["ground"], "avoidObjects": ["tower"], "avoidClearance": 8, "seed": 2,
    })
    summary = await session.expectSuccess("getSceneSummary", {"objectLimit": 500})
    return scattered, summary

  scattered, summary = stageBlenderServer.session(steps)
  grove = [sceneObject["location"] for sceneObject in summary["objects"] if "grove" in sceneObject["collections"]]
  assert scattered["rejected"]["nearAvoidedObject"] > 0
  assert scattered["placed"] == len(grove)
  assert scattered["placed"] + scattered["rejected"]["nearAvoidedObject"] == scattered["spacedCandidates"]
  assert all(math.hypot(x, y) > 10 + 8 - 0.5 for x, y, _ in grove)


def testCarveConformSlidesRimVerticesOntoTheContour(stageBlenderServer):
  path = [[-100, -100, -20], [100, 100, -20]]
  profile = [[0, 0], [0.5, 10], [1, 30]]
  readVertices = "result = [list(vertex.co) for vertex in bpy.data.objects['ground'].data.vertices]"

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    before = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": path, "radius": 40, "strength": 1, "profile": profile})
    after = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    nonRising = await session.expectError("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": path, "radius": 40, "strength": 1, "profile": [[0, 0], [0.5, 10], [1, 10]]})
    return before, after, nonRising

  before, after, nonRising = stageBlenderServer.session(steps)
  rimLateral = 40 * (0.5 + 0.5 * (20 - 10) / (30 - 10))
  slid = [(old, new) for old, new in zip(before, after) if new[2] == 0 and (abs(new[0] - old[0]) > 1e-6 or abs(new[1] - old[1]) > 1e-6)]
  assert slid
  for _, new in slid:
    assert abs(abs(new[0] - new[1]) / math.sqrt(2) - rimLateral) < 1e-3
  assert "conformRim needs profile heights that rise" in nonRising
