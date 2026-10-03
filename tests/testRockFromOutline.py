from testModelsAndDressing import freshScene, readShapedMesh

readCollections = "result = [collection.name for collection in bpy.data.objects[objectName].users_collection]"
slab = [[-100, -20], [100, -20], [100, 20], [-100, 20]]


def edgeUses(faces):
  uses = {}
  for face in faces:
    for first, second in zip(face, face[1:] + face[:1]):
      key = (min(first, second), max(first, second))
      uses[key] = uses.get(key, 0) + 1
  return uses


def heightsAt(vertices, x, y):
  return sorted({round(z, 3) for vx, vy, z in vertices if abs(vx - x) < 1e-3 and abs(vy - y) < 1e-3})


def testRockFromOutlineSpansOnASmoothUnderside(stageBlenderServer):
  span = {"name": "span", "outline": slab, "top": 50, "underside": {"axis": [[-100, 0], [100, 0]], "profile": [[0, 0], [100, 40], [200, 0]]}, "spacing": 8}

  async def steps(session):
    await freshScene(session)
    created = await session.expectSuccess("createRockFromOutline", span)
    shaped = (await session.expectSuccess("runPython", {"code": "objectName = 'span'" + readShapedMesh}))["result"]
    collections = (await session.expectSuccess("runPython", {"code": "objectName = 'span'\n" + readCollections}))["result"]
    crossed = await session.expectError("createRockFromOutline", span | {"name": "bowtie", "outline": [[-100, -20], [100, 20], [100, -20], [-100, 20]]})
    pinched = await session.expectError("createRockFromOutline", span | {"name": "pinched", "underside": {"axis": [[-100, 0], [100, 0]], "profile": [[0, 0], [100, 60], [200, 0]]}})
    backwards = await session.expectError("createRockFromOutline", span | {"name": "backwards", "underside": {"axis": [[-100, 0], [100, 0]], "profile": [[100, 40], [0, 0]]}})
    return created, shaped, collections, crossed, pinched, backwards

  created, shaped, collections, crossed, pinched, backwards = stageBlenderServer.session(steps)
  vertices = shaped["vertices"]
  assert collections == ["terrain"]
  assert set(edgeUses(shaped["faces"]).values()) == {2}
  # The top is flat at 50; the underside climbs from 0 at the ends to its crown of 40 in the middle, bowed above a straight climb
  # (20 at 52 along it), never above 40; the faces have rows every 8 units.
  assert max(z for _, _, z in vertices) == 50
  assert heightsAt(vertices, 0, 0) == [40, 50] and heightsAt(vertices, -48, 0) == [25.992, 50]
  assert heightsAt(vertices, -100, -20) == [0, 8, 16, 24, 32, 40, 50]
  assert all(z <= 40 + 1e-6 or z == 50 for _, _, z in vertices)
  assert created["thinnest"]["thickness"] == 10 and created["thinnest"]["at"][0] == 0 and created["tuckedVertices"] == 0
  assert "crosses itself" in crossed
  assert "comes within 1 of the top at [0.0" in pinched
  assert "distances rising" in backwards


def testRockFromOutlineTucksUnderTheGroundAndFlaresAtItsFaces(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 10, "location": [0, 0, 50], "collection": "terrain"})
    await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"box": {"minimum": [-62, -200, 40], "maximum": [62, 200, 60]}}})
    created = await session.expectSuccess("createRockFromOutline", {
      "name": "bridge", "outline": [[-100, -20], [0, -20], [100, -20], [100, 20], [0, 20], [-100, 20]], "top": 50, "ground": "ground", "spacing": 8, "flare": [[0, 6], [8, 0]],
      "underside": {"axis": [[-100, 0], [100, 0]], "profile": [[0, -10], [30, -10], [100, 30], [170, -10], [200, -10]]},
    })
    shaped = (await session.expectSuccess("runPython", {"code": "objectName = 'bridge'" + readShapedMesh}))["result"]
    return created, shaped

  created, shaped = stageBlenderServer.session(steps)
  vertices = shaped["vertices"]
  # Over the ground, which ends between 60 and 70 from the middle, the top runs 2 under it; over the gap it is at 50.
  tops = {(round(x, 3), round(y, 3)): z for x, y, z in vertices if z > 45}
  assert set(tops.values()) == {48, 50} and all(z == 48 for (x, _), z in tops.items() if abs(x) >= 70) and all(z == 50 for (x, _), z in tops.items() if abs(x) <= 60)
  assert created["tuckedVertices"] == sum(z == 48 for z in tops.values())
  # Under the middle the underside is 30 at the center line and rises 6 to 36 at each face, the thinnest point.
  assert heightsAt(vertices, 0, 0)[0] == 30 and heightsAt(vertices, 0, 20)[0] == 36 and heightsAt(vertices, 0, -20)[0] == 36
  assert created["thinnest"]["thickness"] == 14 and abs(created["thinnest"]["at"][1]) == 20
