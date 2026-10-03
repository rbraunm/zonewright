import math

from testModelsAndDressing import freshScene, readShapedMesh

readCollections = "result = [collection.name for collection in bpy.data.objects[objectName].users_collection]"


def signedVolume(vertices, faces):
  volume = 0.0
  for face in faces:
    origin = vertices[face[0]]
    for second, third in zip(face[1:-1], face[2:]):
      a, b = vertices[second], vertices[third]
      volume += (origin[0] * (a[1] * b[2] - a[2] * b[1]) - origin[1] * (a[0] * b[2] - a[2] * b[0]) + origin[2] * (a[0] * b[1] - a[1] * b[0])) / 6
  return volume


def edgeUses(faces):
  uses = {}
  for face in faces:
    for first, second in zip(face, face[1:] + face[:1]):
      key = (min(first, second), max(first, second))
      uses[key] = uses.get(key, 0) + 1
  return uses


def testRockMassFollowsItsPathWithASmoothSection(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createRockMass", {"name": "slab", "path": [[-100, 0, 50], [100, 0, 50]], "widths": [20, 20], "thicknesses": [10, 10], "spacing": 2})
    slab = (await session.expectSuccess("runPython", {"code": "objectName = 'slab'" + readShapedMesh}))["result"]
    collections = (await session.expectSuccess("runPython", {"code": "objectName = 'slab'\n" + readCollections}))["result"]
    await session.expectSuccess("createRockMass", {"name": "arch", "path": [[-100, 0, 50], [0, 0, 50], [100, 0, 50]], "widths": [20, 20, 20], "thicknesses": [40, 10, 40], "spacing": 4})
    arch = (await session.expectSuccess("runPython", {"code": "objectName = 'arch'" + readShapedMesh}))["result"]
    upright = await session.expectError("createRockMass", {"name": "spire", "path": [[0, 0, 0], [0, 0, 100]], "widths": [20, 20], "thicknesses": [10, 10], "spacing": 4})
    miscounted = await session.expectError("createRockMass", {"name": "ledge", "path": [[0, 0, 0], [50, 0, 0]], "widths": [20], "thicknesses": [10, 10], "spacing": 4})
    return slab, collections, arch, upright, miscounted

  slab, collections, arch, upright, miscounted = stageBlenderServer.session(steps)
  vertices = slab["vertices"]
  assert collections == ["terrain"]
  assert [round(min(vertex[axis] for vertex in vertices), 3) for axis in range(3)] == [-100, -10, 40]
  assert [round(max(vertex[axis] for vertex in vertices), 3) for axis in range(3)] == [100, 10, 50]
  assert set(edgeUses(slab["faces"]).values()) == {2}
  # A squareness-4 superellipse 20 wide and 10 deep encloses 3.708 x 10 x 5 square units: more than the ellipse's pi x 10 x 5,
  # less than the box's 4 x 10 x 5; the inscribed outline falls slightly short of it.
  superellipseArea = 4 * 10 * 5 * math.gamma(1.25) ** 2 / math.gamma(1.5)
  assert 0.97 < signedVolume(vertices, slab["faces"]) / (superellipseArea * 200) < 1.0

  def lowestNear(x):
    return min(vertex[2] for vertex in arch["vertices"] if abs(vertex[0] - x) < 2.5)

  # Its underside is 10 below the top over the middle and 40 below at the ends, and climbs steadily between without overshooting.
  undersides = [lowestNear(x) for x in (0, 25, 50, 75, 100)]
  assert abs(undersides[0] - 40) < 0.5 and abs(undersides[-1] - 10) < 0.5
  assert all(higher > lower for higher, lower in zip(undersides, undersides[1:]))
  assert min(vertex[2] for vertex in arch["vertices"]) > 10 - 0.5
  assert "runs straight up or down" in upright
  assert "widths are one positive value per path point (2)" in miscounted


def testWalkRouteCrossesADeckAndFindsDropsAndLowCeilings(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"box": {"minimum": [-62, -200, -10], "maximum": [62, 200, 10]}}})
    await session.expectSuccess("createRockMass", {"name": "deck", "path": [[-100, 0, -0.5], [0, 0, -0.5], [100, 0, -0.5]], "widths": [24, 24, 24], "thicknesses": [60, 15, 60], "spacing": 4})
    await session.expectSuccess("createRockMass", {"name": "ledge", "path": [[-180, 70, 9], [-110, 70, 9]], "widths": [20, 20], "thicknesses": [4, 4], "spacing": 4})
    across = await session.expectSuccess("walkRoute", {"path": [[-150, 0, 0], [150, 0, 0]]})
    beside = await session.expectSuccess("walkRoute", {"path": [[-150, 40, 0], [150, 40, 0]]})
    under = await session.expectSuccess("walkRoute", {"path": [[-190, 70, 0], [-100, 70, 0]]})
    return across, beside, under

  across, beside, under = stageBlenderServer.session(steps)
  # Over the chasm the footing is the 24-wide deck, half a unit below the ground it runs under at each end.
  assert across["walkable"] and across["problems"] == [] and across["lowestHeadroom"] is None
  assert abs(across["length"] - 300) < 1 and across["steepest"]["slopeDegrees"] < 10
  assert 20 <= across["narrowest"]["width"] <= 24 and abs(across["narrowest"]["at"][0]) < 64
  # Beside the deck the ground ends at the chasm's edge, 64 from the middle.
  assert not beside["walkable"] and len(beside["problems"]) == 1 and beside["problems"][0].startswith("drop")
  assert -68 <= beside["length"] - 150 <= -60
  # Under the ledge, whose underside is 5 above the ground, there is not room for a player 6 tall.
  assert not under["walkable"] and under["lowestHeadroom"]["headroom"] == 5.0
  assert all(problem.startswith("headroom 5.0") for problem in under["problems"])
