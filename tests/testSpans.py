import math

import structurePlots

# Anchors on the gorge's lips: a deck sagging from an anchor back on the rim would dip into the rim's flat ground, and one from the wall
# below the lip leaves a notch to step into.
bridgeStart, bridgeEnd = [-100.0, 0.0, 0.0], [-20.0, 0.0, 0.0]
bridgeSpan = bridgeEnd[0] - bridgeStart[0]
legs = {"piece": "testKitLeg", "spacing": 25}
ropes = {"piece": "testKitRope", "height": 4}


async def kitAndPlot(session, folder):
  kitPath = await structurePlots.testKit(session, folder)
  await structurePlots.testPlot(session, folder)
  return str(kitPath)


def bridgeArguments(kitPath, name="gorgeBridge", **changes):
  return {"name": name, "kitPath": kitPath, "start": bridgeStart, "end": bridgeEnd, "width": 8, "deck": "testKitPlank"} | changes


def testARopeBridgeSagsBetweenItsAnchorsCoveredEdgeToEdgeAndWalkedAcross(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, profile={"sag": 8}, posts=legs, rails=ropes))
    middle = await session.expectSuccess("measure", {"points": [[-61, 1.25, 10]], "snapToSurface": True})
    anchors = {(post["at"][0], post["at"][1]): post for post in built["posts"] if post["anchor"]}
    found = await structurePlots.faces(session, "gorgeBridge")
    summary = await session.expectSuccess("getSceneSummary")
    return built, middle, anchors, found, summary

  built, middle, anchors, found, summary = stageBlenderServer.session(steps)
  assert abs(middle["points"][0][2] - -8.0) <= 0.05
  assert abs(built["steepest"]["degrees"] - math.degrees(math.atan(4 * 8 / bridgeSpan))) <= 0.1 and built["steepest"]["at"] in ("start", "end")
  assert built["deck"]["planks"] == round(built["deckLength"] / 2.5) and abs(built["deck"]["covered"] - built["deckLength"]) <= 0.01
  assert len(anchors) == 4
  for at, post in anchors.items():
    lowest = min(point[2] for point in structurePlots.pointsNear(found, at[0], at[1], 1.5))
    assert abs(lowest - (structurePlots.gorgeHeight(at[0]) - 2.0)) <= 0.05, (at, lowest)
  walk = built["walk"]
  assert walk["walkable"], walk["problems"]
  assert walk["narrowest"]["width"] == 8.0, [(row["at"], row["left"], row["right"]) for row in walk["profile"]]
  assert [part["name"] for part in built["parts"]] == ["gorgeBridge"] and built["parts"][0]["model"] == "obj_gorgebridge.mod"
  assert "gorgeBridge" in [entry["name"] for entry in summary["objects"]]


def testRopesAndRailsArePassableAndNothingElse(stageBlenderServer, tmp_path):
  railY = 40.0
  bars = {"piece": "testKitRail", "height": 2.5}
  across = [[-70, railY, 0], [-70, railY + 14, 0]]

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    roped = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, "ropeBridge", posts=legs, rails=ropes))
    railed = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, "railBridge", start=[-100, railY, 0], end=[-20, railY, 0], posts=legs, rails=bars))
    found = await structurePlots.faces(session, "ropeBridge", "railBridge")
    passableWalk = await session.expectSuccess("walkRoute", {"path": across})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "zone.blend")})
    await session.expectSuccess("openFile", {"path": kitPath, "discardUnsavedChanges": True})
    await session.expectSuccess("markKitPiece", {"collectionName": "testKitRail", "kind": "rail", "passable": False})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("openFile", {"path": str(tmp_path / "zone.blend"), "discardUnsavedChanges": True})
    stale = await session.expectSuccess("getStructures", {"names": ["railBridge"]})
    await session.expectSuccess("editStructure", {"name": "railBridge"})
    solidWalk = await session.expectSuccess("walkRoute", {"path": across})
    return roped, railed, found, passableWalk, stale, solidWalk

  roped, railed, found, passableWalk, stale, solidWalk = stageBlenderServer.session(steps)
  ropeFaces = [face for face in found if face["object"] == "ropeBridge"]
  railFaces = [face for face in found if face["object"] == "railBridge"]
  assert [face["passable"] for face in ropeFaces] == [face["material"] == "testKitRope" for face in ropeFaces]
  assert sum(face["material"] == "testKitRope" for face in ropeFaces) > 0
  barsLaid = sum(len([post for post in railed["posts"] if post["side"] == side]) - 1 for side in ("left", "right"))
  assert sum(face["passable"] for face in railFaces) == 6 * barsLaid
  assert passableWalk["problems"] == [] and [(step["kind"], step["height"]) for step in passableWalk["oneWay"]] == [("ledge", 40.0)]
  assert stale["structures"][0]["stale"] and stale["structures"][0]["kitChanged"] == ["testKitRail"]
  assert solidWalk["problems"][0]["kind"] == "rise"


def testBridgeRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    refusals = {
      "noFooting": await session.expectError("buildBridge", bridgeArguments(kitPath, start=[-100, 0, 10])),
      "tooSteep": await session.expectError("buildBridge", bridgeArguments(kitPath, profile={"sag": 40})),
      "railsWithoutPosts": await session.expectError("buildBridge", bridgeArguments(kitPath, rails=ropes)),
      "stationPast": await session.expectError("buildBridge", bridgeArguments(kitPath, bents={"stations": [100], "post": "testKitLeg"})),
      "wrongKind": await session.expectError("buildBridge", bridgeArguments(kitPath, deck="testKitBeam")),
    }
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [-60, 0, -40], "radius": 16, "strength": 46, "direction": [0, 0, 1]})
    refusals["hump"] = await session.expectError("buildBridge", bridgeArguments(kitPath))
    structures = await session.expectSuccess("getStructures")
    summary = await session.expectSuccess("getSceneSummary")
    return refusals, structures, summary

  refusals, structures, summary = stageBlenderServer.session(steps)
  assert "has no footing within a step" in refusals["noFooting"] and "start" in refusals["noFooting"]
  fits = (bridgeSpan * math.tan(math.radians(30))) / 4
  assert "the largest sag that fits is" in refusals["tooSteep"] and f"{fits:.2f}" in refusals["tooSteep"]
  assert "Rails run from post to post" in refusals["railsWithoutPosts"]
  assert "Bent station 100" in refusals["stationPast"]
  assert "deck 'testKitBeam' is a beam piece; deck takes a plank or floor piece" in refusals["wrongKind"]
  assert "underside meets the ground" in refusals["hump"] and "along" in refusals["hump"]
  assert structures["structures"] == []
  assert [entry["name"] for entry in summary["objects"]] == ["ground"]
  assert summary["collections"] == ["terrain"] and summary["libraries"] == []


def testATrestleStandsItsBentsOnTheGround(stageBlenderServer, tmp_path):
  stations = [30.0, 55.0]

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, bents={"stations": stations, "post": "testKitLeg", "beam": "testKitBeam"}))
    return built, await structurePlots.faces(session, "gorgeBridge")

  built, found = stageBlenderServer.session(steps)
  assert [bent["station"] for bent in built["bents"]] == stations
  for station in stations:
    x = bridgeStart[0] + station
    for y in (3.0, -3.0):
      lowest = min(point[2] for point in structurePlots.pointsNear(found, x, y, 1.45))
      assert abs(lowest - (structurePlots.gorgeHeight(x) - 2.0)) <= 0.05, (x, y, lowest)
    beam = [point for point in structurePlots.pointsNear(found, x, 0, 4.1) if -3.5 - 1e-6 <= point[2] <= -0.5 + 1e-6 and abs(point[1]) > 3.9]
    assert beam and min(point[2] for point in beam) == -3.5
  assert all(len(bent["legs"]) == 2 for bent in built["bents"])


def testAFlightHasEqualRisersAtMostTheRiserAndWalksFootToHead(stageBlenderServer, tmp_path):
  foot, head = [-75.0, -60.0, -40.0], [-20.0, -60.0, 0.0]

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildStairs", {
      "name": "gorgeFlight", "kitPath": kitPath, "bottom": foot, "top": head, "width": 6, "tread": "testKitTread", "riser": 1,
      "posts": {"piece": "testKitLeg", "spacing": 8},
    })
    found = await structurePlots.faces(session, "gorgeFlight")
    steep = await session.expectError("buildStairs", {"name": "steepFlight", "kitPath": kitPath, "bottom": [-55, -60, -40], "top": head, "width": 6, "tread": "testKitTread"})
    tall = await session.expectError("buildStairs", {"name": "tallFlight", "kitPath": kitPath, "bottom": foot, "top": head, "width": 6, "tread": "testKitTread", "riser": 2.5})
    return built, found, steep, tall

  built, found, steep, tall = stageBlenderServer.session(steps)
  assert built["risers"] == {"count": 40, "height": 1.0}
  treadTops = sorted({round(face["points"][0][2], 4) for face in found if face["material"] == "testKitTimber" and len({round(point[2], 6) for point in face["points"]}) == 1 and abs(face["points"][0][1] + 60) < 3.5 and face["points"][0][2] > -40})
  assert len(treadTops) >= 40
  tops = [value for value in treadTops if abs(value - round(value)) < 1e-4]
  assert tops == [float(value) for value in range(-39, 1)]
  ends = sorted({round(point[0], 4) for face in found if face["material"] == "testKitTimber" for point in face["points"] if abs(point[2] - round(point[2])) < 1e-4 and abs(point[1] + 60) <= 3 + 1e-6})
  run = (head[0] - foot[0]) / 40
  assert all(any(abs(end - (foot[0] + step * run)) < 1e-4 for end in ends) for step in range(41))
  assert built["legs"] and all(abs(leg["bottom"] - (structurePlots.gorgeHeight(leg["at"][0]) - 2.0)) <= 0.05 for leg in built["legs"])
  assert built["walk"]["walkable"], built["walk"]["problems"]
  assert "needs a run of at least 40.00" in steep
  assert "riser must be above 0 and at most a step" in tall


def testAWalkwayHoldsLevelLandingsGradesItsLegsAndLaysListedFlights(stageBlenderServer, tmp_path):
  points = [[110, 6, 0], [70, 6, 10], [70, -20, 10], [70, -50, 22]]

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildWalkway", {
      "name": "cliffWalk", "kitPath": kitPath, "points": points, "width": 4, "deck": "testKitPlank", "treads": "testKitTread", "stairLegs": [2],
      "posts": {"piece": "testKitLeg", "spacing": 8}, "brackets": {"piece": "testKitBeam", "side": "right", "reach": 10, "legs": [0]},
    })
    landings = {}
    for x, y in ((70, 6), (70, -20)):
      landings[(x, y)] = (await session.expectSuccess("measure", {"points": [[x + dx, y + dy, 20] for dx, dy in ((0, 0), (1.5, 1.5), (-1.5, -1.5), (1.5, -1.5))], "snapToSurface": True}))["points"]
    return built, landings

  built, landings = stageBlenderServer.session(steps)
  for (x, y), measured in landings.items():
    assert all(abs(point[2] - 10.0) <= 1e-4 for point in measured), measured
  legReports = {leg["leg"]: leg for leg in built["legs"]}
  assert abs(legReports[0]["gradeDegrees"] - math.degrees(math.atan2(10, 38))) <= 0.05
  assert legReports[2]["laid"] == "flight" and legReports[2]["risers"] == {"count": 12, "height": 1.0}
  assert built["brackets"]
  for bracket in built["brackets"]:
    assert bracket["at"][1] == 8.0
  legZeroPosts = [post for post in built["posts"]["each"] if 72 < post["at"][0] <= 110 and abs(post["at"][1] - 6) < 4]
  assert legZeroPosts and {post["side"] for post in legZeroPosts} == {"left"}
  assert built["walk"]["walkable"], built["walk"]["problems"]


def testABracketReachesTheRockBesideIt(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    return await session.expectSuccess("buildWalkway", {
      "name": "ledgeWalk", "kitPath": kitPath, "points": [[100, 6, 4], [60, 6, 4]], "width": 4, "deck": "testKitPlank",
      "posts": {"piece": "testKitLeg", "spacing": 10, "sides": "left"}, "brackets": {"piece": "testKitBeam", "side": "right", "reach": 10, "legs": [0]},
    })

  built = stageBlenderServer.session(steps)
  middle = 4 - 0.5 - 1.5
  assert len(built["brackets"]) == 5
  for bracket in built["brackets"]:
    assert abs(bracket["reach"] - (structurePlots.cliffFaceY(middle) - 8)) <= 0.01, bracket


def testWalkwayRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)

    def walkway(name, points, **changes):
      return {"name": name, "kitPath": kitPath, "points": points, "width": 4, "deck": "testKitPlank", "treads": "testKitTread"} | changes

    return {
      "steep": await session.expectError("buildWalkway", walkway("steep", [[110, -10, 0], [70, -10, 20]])),
      "stairTooSteep": await session.expectError("buildWalkway", walkway("stair", [[110, -10, 0], [100, -10, 12]], stairLegs=[0])),
      "noRock": await session.expectError("buildWalkway", walkway("bracketed", [[110, -10, 6], [70, -10, 6]], posts={"piece": "testKitLeg", "spacing": 8, "sides": "left"}, brackets={"piece": "testKitBeam", "side": "right", "reach": 5, "legs": [0]})),
      "unheld": await session.expectError("buildWalkway", walkway("raised", [[110, -10, 5], [70, -10, 5]])),
      "turn": await session.expectError("buildWalkway", walkway("hairpin", [[110, -10, 0], [70, -10, 0], [109, -3, 0]])),
    }

  refusals = stageBlenderServer.session(steps)
  assert "Leg 0 grades 26.57 degrees" in refusals["steep"] and f"a run of {20 / math.tan(math.radians(15)):.2f}" in refusals["steep"]
  assert "Stair leg 0 is 50.19 degrees steep" in refusals["stairTooSteep"]
  assert "No rock within reach 5 beside the bracket station" in refusals["noRock"] and "[110.0, -8.0]" in refusals["noRock"]
  assert "held by neither posts nor brackets" in refusals["unheld"] and "left edge" in refusals["unheld"]
  assert "turns 169.8 degrees at point 1" in refusals["turn"]


def testAWalkwayMayEndOffTheGroundAndReportsEachEndsFooting(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    return await session.expectSuccess("buildWalkway", {
      "name": "gorgeDock", "kitPath": kitPath, "points": [[-19, -90, 0], [-50, -90, 0]], "width": 4, "deck": "testKitPlank",
      "posts": {"piece": "testKitLeg", "spacing": 8},
    })

  built = stageBlenderServer.session(steps)
  assert built["endFootingGaps"] == {"start": 0.0, "end": None}
  assert built["posts"]["longest"] > 40
  assert built["walk"]["walkable"]


def testAWalkwayInTheTerrainCollectionIsTerrain(stageBlenderServer, tmp_path):
  classify = """
import bridgeExport
shipped, excluded, failures = bridgeExport.classifyObjects()
result = {sceneObject.name: role for sceneObject, role in shipped}
"""

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildWalkway", {
      "name": "plazaDeck", "kitPath": kitPath, "points": [[20, -10, 0.5], [60, -10, 0.5]], "width": 4, "deck": "testKitPlank", "collection": "terrain",
    })
    return built, (await session.expectSuccess("runPython", {"code": classify}))["result"]

  built, roles = stageBlenderServer.session(steps)
  assert roles["plazaDeck"] == "terrain"
  assert built["parts"][0]["model"] is None
