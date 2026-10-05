import math
import sys
from pathlib import Path

from conftest import writePNG
from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from playerScale import eyeHeight, playerHeight, stepHeight, walkableNormalZ


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


# Half a unit over a step (playerScale), the low step is a rise and the slab's underside leaves less headroom than a player's height
# without being met as a rise, whatever the step is measured at below that height.
pastStep = stepHeight + 0.5
courseBlocks = [
  ("thinSlab", [30, 40, 1], [-150, 0, pastStep]), ("rock", [20, 20, 12], [-90, 0, -2]), ("lowStep", [30, 40, pastStep], [-30, 0, 0]),
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
    insideRock = await session.expectError("measure", {"points": [[-90, 0, 5]], "snapToSurface": True})
    return coarse, fine, offLedge, underDeck, insideRock

  coarse, fine, offLedge, underDeck, insideRock = stageBlenderServer.session(steps)
  # Every block on the course stops a player, each listed once, and the walk takes up again on the ground beyond it: the 1-thick slab
  # leaves its height of headroom, the rock sunk 2 into the ground stands 10 above it, the steps their heights, and the chasm has no
  # floor.
  assert pastStep < playerHeight
  for walked in (coarse, fine):
    assert not walked["walkable"] and walked["oneWay"] == []
    assert [(problem["kind"], problem.get("height", problem.get("lowest"))) for problem in walked["problems"]] == [
      ("headroom", round(pastStep, 1)), ("rise", 10.0), ("rise", round(pastStep, 1)), ("rise", 10.0), ("drop", None),
    ]
    slab, rock, lowStep, stepTen, chasm = walked["problems"]
    assert near(slab["from"], -165) and near(slab["to"], -135)
    assert near(rock["at"], -100) and near(rock["resumesAt"], -80)
    assert near(lowStep["at"], -45) and near(lowStep["resumesAt"], -15)
    assert near(stepTen["at"], 25) and near(stepTen["resumesAt"], 55)
    assert near(chasm["at"], 108) and near(chasm["resumesAt"], 152)
    assert all(row["at"][2] == 0.0 for row in walked["profile"])
    # The 380 walked leaves out what the rock, the two steps, and the chasm stand across (20, 30, 30, and 44), and at each stop the
    # half-unit stride it stops in.
    assert walked["length"] == 380 - (20 + 30 + 30 + 44) - 4 * 0.5
  # Stepping off the 10-high step is a way down a player cannot climb back, not a slope too steep to walk.
  assert offLedge["walkable"] and offLedge["problems"] == []
  assert len(offLedge["oneWay"]) == 1 and offLedge["oneWay"][0]["kind"] == "ledge" and offLedge["oneWay"][0]["height"] == 10.0
  assert near(offLedge["oneWay"][0]["at"], 55) and offLedge["oneWay"][0]["at"][2] == 10.0
  # Under a deck with 9 of headroom the footing runs on to either side: the deck is no wall.
  assert underDeck["walkable"] and underDeck["lowestHeadroom"]["headroom"] == 9.0
  assert underDeck["narrowest"]["left"] is None and underDeck["narrowest"]["right"] is None
  # measure stands where walkRoute does: the ground inside the rock is no footing.
  assert "No surface players stand on below [-90.0, 0.0, 5.0]" in insideRock


def rampArguments(name, degrees, y, run=8.0):
  """A plane `run` long rising toward +x at `degrees`, its foot on the ground at x 20, 20 wide about y."""
  rise = run * math.sin(math.radians(degrees))
  return {"kind": "plane", "name": name, "size": [run, 20, 0], "location": [20 + run * math.cos(math.radians(degrees)) / 2, y, rise / 2], "rotationDegrees": [0, -degrees, 0]}


def testWalkRouteClimbsWhatTheRoF2ClientClimbs(stageBlenderServer):
  steepestWalkable = math.degrees(math.acos(walkableNormalZ))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "course", "size": [200, 120], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "lowRiser", "size": [20, 20, stepHeight - 0.5], "location": [-60, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "highRiser", "size": [20, 20, stepHeight + 0.5], "location": [-60, 40, 0]})
    await session.expectSuccess("createPrimitive", rampArguments("climbable", steepestWalkable - 2, 0))
    await session.expectSuccess("createPrimitive", rampArguments("tooSteep", steepestWalkable + 5, 40))
    walks = {}
    for name, y in (("lowRiser", 0), ("highRiser", 40)):
      walks[name] = await session.expectSuccess("walkRoute", {"path": [[-90, y, 0], [-30, y, 0]]})
    for name, y in (("climbable", 0), ("tooSteep", 40)):
      walks[name] = await session.expectSuccess("walkRoute", {"path": [[0, y, 0], [21, y, 6]]})
    return walks

  walks = stageBlenderServer.session(steps)
  # Half a unit under the step players climb (playerScale) is walked up and down; half a unit over it stops them.
  assert walks["lowRiser"]["walkable"] and walks["lowRiser"]["problems"] == [] and walks["lowRiser"]["oneWay"] == []
  assert max(row["at"][2] for row in walks["lowRiser"]["profile"]) == stepHeight - 0.5
  assert [(problem["kind"], problem["height"]) for problem in walks["highRiser"]["problems"]] == [("rise", round(stepHeight + 0.5, 1))]
  # A face two degrees under the steepest players walk is climbed; one five degrees over it is too steep.
  assert walks["climbable"]["walkable"] and walks["climbable"]["problems"] == []
  assert abs(walks["climbable"]["steepest"]["slopeDegrees"] - (steepestWalkable - 2)) <= 0.2
  assert [problem["kind"] for problem in walks["tooSteep"]["problems"]] == ["steep"]
  assert abs(walks["tooSteep"]["problems"][0]["steepestDegrees"] - (steepestWalkable + 5)) <= 0.2


houseWalls = [
  ("wallNorth", [30, 0.5, 10], [0, 95, 0]), ("wallSouth", [30, 0.5, 10], [0, 65, 0]),
  ("wallWestA", [0.5, 11, 10], [-15, 70.5, 0]), ("wallWestB", [0.5, 11, 10], [-15, 89.5, 0]),
  ("wallEastA", [0.5, 11, 10], [15, 70.5, 0]), ("wallEastB", [0.5, 11, 10], [15, 89.5, 0]),
]
coverRoutes = {
  "house": [[-40, 78, 0], [40, 78, 0]], "wall": [[-40, 72, 0], [-5, 72, 0]], "deck": [[-235, 0, 0], [-165, 0, 0]],
  "fence": [[-150, 0, 0], [-90, 0, 0]], "tree": [[60, 4, 0], [140, 4, 0]],
}


def testWalkRouteGoesUnderOneSidedCoverAndMeasuresPlaneWalls(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "field", "size": [560, 320], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    for name, size, location in houseWalls:
      await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": size, "location": location})
    for name, y, tilt in (("roofSouth", 80 - 7.794, 30), ("roofNorth", 80 + 7.794, -30)):
      await session.expectSuccess("createPrimitive", {"kind": "plane", "name": name, "size": [32, 18, 0], "location": [0, y, 14.5], "rotationDegrees": [tilt, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "deck", "size": [40, 40, 0], "location": [-200, 0, 10]})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "fence", "size": [15, 40, 0], "location": [-120, 0, 7.5], "rotationDegrees": [0, 90, 0]})
    await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "OBJ_TREEM", "name": "tree", "location": [100, 0, 0], "headingDegrees": 0})
    walked = {name: await session.expectSuccess("walkRoute", {"path": path, "sampleSpacing": 2}) for name, path in coverRoutes.items()}
    inside = await session.expectSuccess("measure", {"points": [[0, 78, 5]], "snapToSurface": True})
    return walked, inside

  walked, inside = stageBlenderServer.session(steps)
  # One-sided surfaces facing up overhead (a gable roof of two planes, a plane deck 10 up, a client tree's leaf cards) cover open
  # ground: it is footing, walked under with the headroom they leave, and the ground under the deck runs on to either side.
  house, wall, deck, fence, tree = (walked[name] for name in coverRoutes)
  assert house["walkable"] and house["problems"] == [] and house["oneWay"] == [] and house["lowestHeadroom"]["headroom"] == 17.8
  assert deck["walkable"] and deck["problems"] == [] and deck["lowestHeadroom"]["headroom"] == 10.0
  assert all(row["left"] is None and row["right"] is None for row in deck["profile"])
  assert tree["walkable"] and tree["problems"] == [] and tree["lowestHeadroom"]["headroom"] == 15.7
  # Into the house's wall under its eave is a rise the height of the wall, and the walk takes up again inside.
  assert wall["problems"] == [{"kind": "rise", "at": [-15.7, 72.0, 0.0], "height": 10.0, "resumesAt": [-14.7, 72.0, 0.0]}]
  # A plane standing 15 tall, with no top to stand on, is a rise of 15.
  assert fence["problems"] == [{"kind": "rise", "at": [-120.0, 0.0, 0.0], "height": 15.0, "resumesAt": [-119.0, 0.0, 0.0]}]
  assert inside["points"] == [[0.0, 78.0, 0.0]]


# Taller than a step (playerScale), so it is a rise from the ground and a ledge from its top.
instancedStepHeight = stepHeight + 2
instancedStepCode = f"""
kit = bpy.data.collections.new('kit')
mesh = bpy.data.meshes.new('step')
top = {instancedStepHeight}
mesh.from_pydata([(-20, -20, 0), (20, -20, 0), (20, 20, 0), (-20, 20, 0), (-20, -20, top), (20, -20, top), (20, 20, top), (-20, 20, top)], [],
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
    offStep = await session.expectSuccess("walkRoute", {"path": [[0, 150, instancedStepHeight], [100, 150, 0]]})
    _, deep = await session.expectImage("renderView", {"view": {"standAt": [0, 0], "headingDegrees": 90, "pitchDegrees": 0}, "guides": False})
    _, shallow = await session.expectImage("renderView", {"view": {"standAt": [70, 0], "headingDegrees": 90, "pitchDegrees": 0}, "guides": False})
    return across, overStep, offStep, deep, shallow

  across, overStep, offStep, deep, shallow = stageBlenderServer.session(steps)
  # The route starts under the guide board at 2 and keeps to the ground at 0; through the pool its footing is the bed, which falls 20
  # in 100 to the middle: the samples 2 either side of it stand 19.6 down under 14.6 of water.
  assert across["profile"][0]["at"][2] == 0.0
  assert across["deepestWater"]["depth"] == 14.6 and abs(across["deepestWater"]["at"][0]) == 2.0
  assert min(row["at"][2] for row in across["profile"]) <= -19.5
  # The instanced step blocks a player like any mesh and is stood on like one: walked into from the ground it is a rise of its height,
  # and from its top the way off is a ledge as far down.
  assert [(problem["kind"], problem["height"]) for problem in overStep["problems"]] == [("rise", round(instancedStepHeight, 1))]
  assert near(overStep["problems"][0]["at"], -20) and near(overStep["problems"][0]["resumesAt"], 20.5)
  assert offStep["walkable"] and offStep["profile"][0]["at"][2] == instancedStepHeight
  assert offStep["oneWay"] == [{"kind": "ledge", "at": [20.0, 150.0, instancedStepHeight], "height": round(instancedStepHeight, 1)}]
  # In the middle a player swims, eye a unit over the surface; at 70 out the water is 1 deep and the player stands on the bed.
  assert deep["swimming"] is True and deep["waterDepth"] == 15.0 and abs(deep["eye"][2] - (-4.0)) < 1e-3
  assert shallow["swimming"] is False and abs(shallow["waterDepth"] - 1.0) < 0.1 and abs(shallow["eye"][2] - (shallow["ground"][2] + eyeHeight)) < 1e-3


def testEveryLookupForGroundPassesThroughACanopyAndWhatIsMarkedPassable(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createMaterial", {"name": "leaves", "diffuseTexture": str(writePNG(tmp_path / "leaves.png", 4, 4, (40, 110, 40, 200))), "cutout": True})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "canopy", "size": [60, 60, 0], "location": [0, 0, 20]})
    await session.expectSuccess("assignMaterial", {"objectName": "canopy", "materialName": "leaves"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "moss", "size": [20, 20, 8], "location": [60, 60, 0]})
    await session.expectSuccess("markPassable", {"objects": ["moss"]})
    for name, location in (("stone", [5, 5, 40]), ("pebble", [-5, -5, 40]), ("nest", [-10, 10, 40])):
      await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [2, 2, 2], "location": location})
    await session.expectSuccess("setZoneProperties", {
      "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
      "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000, "newEngineZone": False,
    })
    _, under = await session.expectImage("renderView", {"view": {"standAt": [0, 0], "headingDegrees": 90, "pitchDegrees": 0, "figureAt": [10, 0]}, "guides": False})
    _, mossy = await session.expectImage("renderView", {"view": {"standAt": [60, 60], "headingDegrees": 0, "pitchDegrees": 0}, "guides": False})
    measured = await session.expectSuccess("measure", {"points": [[0, 0, 50], [60, 60, 50]], "snapToSurface": True})
    sketched = await session.expectSuccess("sketch", {"sheet": "plot", "shapes": [{"name": "under", "kind": "point", "at": [0, 0]}, {"name": "onMoss", "kind": "point", "at": [60, 60]}]})
    placed = await session.expectSuccess("placeOnSurface", {"objectNames": ["stone"]})
    settled = await session.expectSuccess("settleObjects", {"names": ["pebble"]})
    nested = await session.expectSuccess("placeOnSurface", {"objectNames": ["nest"], "surfaceObjects": ["canopy"]})
    walked = await session.expectSuccess("walkRoute", {"path": [[-50, 0, 0], [50, 0, 0]]})
    _, section = await session.expectImage("renderSection", {"start": [-50, 0], "end": [50, 0], "bottom": -10, "top": 40})
    await session.expectSuccess("deleteObjects", {"names": ["canopy"]})
    _, bareSection = await session.expectImage("renderSection", {"start": [-50, 0], "end": [50, 0], "bottom": -10, "top": 40})
    return under, mossy, measured, sketched, placed, settled, nested, walked, section, bareSection

  under, mossy, measured, sketched, placed, settled, nested, walked, section, bareSection = stageBlenderServer.session(steps)
  # The cutout canopy 20 up and the crate marked passable are passed through by every lookup for the ground, as walkRoute passes them:
  # the views stand on the ground under them, and the stones land and settle on it.
  assert under["ground"] == [0.0, 0.0, 0.0] and under["eye"] == [0.0, 0.0, eyeHeight] and under["figure"] == [10.0, 0.0, 0.0]
  assert mossy["ground"] == [60.0, 60.0, 0.0]
  assert measured["points"] == [[0.0, 0.0, 0.0], [60.0, 60.0, 0.0]]
  assert [shape["ground"] for shape in sketched["shapes"]] == [0.0, 0.0]
  assert placed["placements"][0]["location"] == [5.0, 5.0, 0.0] and placed["placements"][0]["surface"] == "ground"
  assert settled["settled"][0]["under"] == [0.0, 0.0] and settled["settled"][0]["spans"] == [0.0, 2.0]
  # A surface named to place on is that surface, whole: a nest goes onto the canopy itself.
  assert nested["placements"][0]["location"] == [-10.0, 10.0, 20.0] and nested["placements"][0]["surface"] == "canopy"
  assert walked["walkable"] and walked["lowestHeadroom"] is None and all(row["at"][2] == 0.0 for row in walked["profile"])
  assert section["groundSegments"] == bareSection["groundSegments"]
