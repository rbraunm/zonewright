from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials


def near(point, x, tolerance=0.6):
  return abs(point[0] - x) <= tolerance


def testWalkRouteCrossesADeckAndFindsDropsAndLowCeilings(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"box": {"minimum": [-62, -200, -10], "maximum": [62, 200, 10]}}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "deck", "size": [200, 24, 15], "location": [0, 0, -15.5]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ledge", "size": [70, 20, 4], "location": [-145, 70, 5]})
    across = await session.expectSuccess("walkRoute", {"path": [[-150, 0, 0], [150, 0, 0]]})
    beside = await session.expectSuccess("walkRoute", {"path": [[-150, 40, 0], [150, 40, 0]]})
    under = await session.expectSuccess("walkRoute", {"path": [[-190, 70, 0], [-100, 70, 0]]})
    return across, beside, under

  across, beside, under = stageBlenderServer.session(steps)
  # Over the chasm the footing is the 24-wide deck, half a unit below the ground it runs under at each end.
  assert across["walkable"] and across["problems"] == [] and across["oneWay"] == [] and across["lowestHeadroom"] is None
  assert abs(across["length"] - 300) < 1 and across["steepest"]["slopeDegrees"] < 10
  assert 20 <= across["narrowest"]["width"] <= 24 and abs(across["narrowest"]["at"][0]) < 64
  # Beside the deck the ground ends at the chasm's edge, 64 from the middle, and the walk takes up again on its far side.
  assert not beside["walkable"] and [problem["kind"] for problem in beside["problems"]] == ["drop"]
  assert near(beside["problems"][0]["at"], -64) and near(beside["problems"][0]["resumesAt"], 64.5)
  assert abs(beside["length"] - 171.5) < 1
  # Under the ledge, whose underside is 5 above the ground, there is not room for a player 6 tall: one stretch, not one line a sample.
  assert not under["walkable"] and under["lowestHeadroom"]["headroom"] == 5.0
  assert len(under["problems"]) == 1 and under["problems"][0]["kind"] == "headroom" and under["problems"][0]["lowest"] == 5.0
  assert near(under["problems"][0]["from"], -180) and near(under["problems"][0]["to"], -110)


courseBlocks = [
  ("thinSlab", [30, 40, 1], [-150, 0, 5]), ("rock", [20, 20, 12], [-90, 0, -2]), ("stepFive", [30, 40, 5], [-30, 0, 0]),
  ("stepTen", [30, 40, 10], [40, 0, 0]), ("deck", [40, 30, 1], [0, -90, 9]),
]


def testWalkRouteStopsAtEveryBlockAlikeAtAnySpacing(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "course", "size": [400, 320], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("deleteFaces", {"objectName": "course", "selector": {"box": {"minimum": [109, -30, -5], "maximum": [151, 30, 5]}}})
    for name, size, location in courseBlocks:
      await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": size, "location": location})
    coarse = await session.expectSuccess("walkRoute", {"path": [[-190, 0, 0], [190, 0, 0]], "sampleSpacing": 4})
    fine = await session.expectSuccess("walkRoute", {"path": [[-190, 0, 0], [190, 0, 0]], "sampleSpacing": 1})
    offLedge = await session.expectSuccess("walkRoute", {"path": [[40, 0, 10], [80, 0, 0]]})
    underDeck = await session.expectSuccess("walkRoute", {"path": [[-40, -90, 0], [40, -90, 0]]})
    return coarse, fine, offLedge, underDeck

  coarse, fine, offLedge, underDeck = stageBlenderServer.session(steps)
  # Every block on the course stops a player, each listed once, and the walk takes up again on the ground beyond it: the 1-thick slab
  # 5 up leaves 5 of headroom, the rock sunk 2 into the ground stands 10 above it, the steps 5 and 10, and the chasm has no floor.
  for walked in (coarse, fine):
    assert not walked["walkable"] and walked["oneWay"] == []
    assert [(problem["kind"], problem.get("height", problem.get("lowest"))) for problem in walked["problems"]] == [
      ("headroom", 5.0), ("rise", 10.0), ("rise", 5.0), ("rise", 10.0), ("drop", None),
    ]
    slab, rock, stepFive, stepTen, chasm = walked["problems"]
    assert near(slab["from"], -165) and near(slab["to"], -135)
    assert near(rock["at"], -100) and near(rock["resumesAt"], -80)
    assert near(stepFive["at"], -45) and near(stepFive["resumesAt"], -15)
    assert near(stepTen["at"], 25) and near(stepTen["resumesAt"], 55)
    assert near(chasm["at"], 108) and near(chasm["resumesAt"], 152)
    assert all(row["at"][2] == 0.0 for row in walked["profile"])
    # The length walked leaves out what the blocks and the chasm stand across, and at each stop the stride it stops in.
    assert 256 - 4 <= walked["length"] <= 380 - 20 - 30 - 30 - 44
  # Stepping off the 10-high step is a way down a player cannot climb back, not a slope too steep to walk.
  assert offLedge["walkable"] and offLedge["problems"] == []
  assert len(offLedge["oneWay"]) == 1 and offLedge["oneWay"][0]["kind"] == "ledge" and offLedge["oneWay"][0]["height"] == 10.0
  assert near(offLedge["oneWay"][0]["at"], 55) and offLedge["oneWay"][0]["at"][2] == 10.0
  # Under a deck with 9 of headroom the footing runs on to either side: the deck is no wall.
  assert underDeck["walkable"] and underDeck["lowestHeadroom"]["headroom"] == 9.0
  assert underDeck["narrowest"]["left"] is None and underDeck["narrowest"]["right"] is None


instancedStepCode = """
kit = bpy.data.collections.new('kit')
mesh = bpy.data.meshes.new('step')
mesh.from_pydata([(-20, -20, 0), (20, -20, 0), (20, 20, 0), (-20, 20, 0), (-20, -20, 1.5), (20, -20, 1.5), (20, 20, 1.5), (-20, 20, 1.5)], [],
  [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
kit.objects.link(bpy.data.objects.new('step', mesh))
instance = bpy.data.objects.new('stepInstance', None)
instance.instance_type = 'COLLECTION'
instance.instance_collection = kit
instance.location = (0, 150, 0)
bpy.context.scene.collection.objects.link(instance)
guide = bpy.data.meshes.new('board')
guide.from_pydata([(-160, -10, 2), (-100, -10, 2), (-100, 10, 2), (-160, 10, 2)], [], [(0, 1, 2, 3)])
board = bpy.data.objects.new('board', guide)
board['zonewrightGuide'] = 'test'
bpy.context.scene.collection.objects.link(board)
"""


def testPlayersStandOnTheBedUnderWaterAndOnInstancesButNotOnGuides(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    await session.expectSuccess("runPython", {"code": instancedStepCode})
    await session.expectSuccess("setZoneProperties", {
      "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
      "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000, "newEngineZone": False,
    })
    across = await session.expectSuccess("walkRoute", {"path": [[-150, 0, 0], [150, 0, 0]]})
    overStep = await session.expectSuccess("walkRoute", {"path": [[-100, 150, 0], [100, 150, 0]]})
    _, deep = await session.expectImage("renderView", {"view": {"standAt": [0, 0], "headingDegrees": 90, "pitchDegrees": 0}, "guides": False})
    _, shallow = await session.expectImage("renderView", {"view": {"standAt": [70, 0], "headingDegrees": 90, "pitchDegrees": 0}, "guides": False})
    return across, overStep, deep, shallow

  across, overStep, deep, shallow = stageBlenderServer.session(steps)
  # The route starts under the guide board at 2 and keeps to the ground at 0; through the pool its footing is the bed, which falls 20
  # in 100 to the middle: the samples 2 either side of it stand 19.6 down under 14.6 of water.
  assert across["profile"][0]["at"][2] == 0.0
  assert across["deepestWater"]["depth"] == 14.6 and abs(across["deepestWater"]["at"][0]) == 2.0
  assert min(row["at"][2] for row in across["profile"]) <= -19.5
  # The instanced step, 1.5 high, is footing like any mesh, stepped up onto and down from.
  assert overStep["walkable"] and overStep["oneWay"] == []
  assert max(row["at"][2] for row in overStep["profile"]) == 1.5 and overStep["deepestWater"] is None
  # In the middle a player swims, eye a unit over the surface; at 70 out the water is 1 deep and the player stands on the bed.
  assert deep["swimming"] is True and deep["waterDepth"] == 15.0 and abs(deep["eye"][2] - (-4.0)) < 1e-3
  assert shallow["swimming"] is False and abs(shallow["waterDepth"] - 1.0) < 0.1 and abs(shallow["eye"][2] - (shallow["ground"][2] + 5.5)) < 1e-3
