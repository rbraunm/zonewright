import structurePlots

legs = {"piece": "testKitLeg", "spacing": 8}
bridge = {"start": [-100, 0, 0], "end": [-20, 0, 0], "width": 8, "deck": "testKitPlank"}


async def kitAndPlot(session, folder):
  kitPath = await structurePlots.testKit(session, folder)
  await structurePlots.testPlot(session, folder)
  return str(kitPath)


def wall(kitPath, name, path, **changes):
  return {"name": name, "kitPath": kitPath, "path": path, "frontSide": "left", "sections": ["testKitWall25"]} | changes


def staleness(listed, name):
  return next(entry for entry in listed["structures"] if entry["name"] == name)


def testGroundMovedUnderAStructureMakesItStaleWhereItMovedAndRelayingFitsIt(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildWall", wall(kitPath, "plazaWall", [[15, -20], [65, -20]], posts={"piece": "testKitPost", "at": "joints"}))
    before = await session.expectSuccess("getStructures")
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [40, -20, 0], "radius": 12, "strength": 3, "direction": [0, 0, 1]})
    after = await session.expectSuccess("getStructures")
    relaid = await session.expectSuccess("editStructure", {"name": "plazaWall"})
    again = await session.expectSuccess("getStructures")
    return before, after, relaid, again

  before, after, relaid, again = stageBlenderServer.session(steps)
  assert not staleness(before, "plazaWall")["stale"]
  moved = staleness(after, "plazaWall")
  assert moved["stale"] and moved["why"] == ["ground"]
  assert abs(moved["ground"]["largest"]["change"] - 3.0) <= 0.01 and moved["ground"]["largest"]["at"][:2] == [40.0, -20.0]
  assert not staleness(again, "plazaWall")["stale"]
  joint = next(joint for joint in relaid["joints"] if joint["at"] == [40.0, -20.0])
  assert abs(joint["ground"] - 3.0) <= 0.01 and joint["height"] <= joint["ground"] - 1.0
  middle = next(post for post in relaid["posts"] if post["at"] == [40.0, -20.0])
  assert -1.0 < middle["base"] < 3.0 - 1.0
  assert relaid["probesMovedSinceLaid"]["moved"] > 0


def testAKitChangeMakesSpansStaleWhilePlacedPiecesFollow(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildBridge", {"name": "gorgeBridge", "kitPath": kitPath} | bridge)
    await session.expectSuccess("placeKitPiece", {"name": "loosePlank", "kitPath": kitPath, "piece": "testKitPlank", "location": [20, -20, 0], "facingDegrees": 0})
    before = (await session.expectSuccess("getObjectDetail", {"name": "loosePlank"}))["kitPiece"]["triangles"]
    bridgeTriangles = (await session.expectSuccess("getObjectDetail", {"name": "gorgeBridge"}))["triangles"]
    zonePath = tmp_path / "zone.blend"
    await session.expectSuccess("saveFile", {"path": str(zonePath)})
    await session.expectSuccess("openFile", {"path": kitPath, "discardUnsavedChanges": True})
    await session.expectSuccess("subdivide", {"objectName": "testKitPlank", "cuts": 1})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("openFile", {"path": str(zonePath), "discardUnsavedChanges": True})
    listed = await session.expectSuccess("getStructures")
    after = (await session.expectSuccess("getObjectDetail", {"name": "loosePlank"}))["kitPiece"]["triangles"]
    unchanged = (await session.expectSuccess("getObjectDetail", {"name": "gorgeBridge"}))["triangles"]
    return before, after, bridgeTriangles, unchanged, listed

  before, after, bridgeTriangles, unchanged, listed = stageBlenderServer.session(steps)
  entry = staleness(listed, "gorgeBridge")
  assert entry["stale"] and entry["why"] == ["kit"] and entry["kitChanged"] == ["testKitPlank"]
  assert after > before and unchanged == bridgeTriangles
  assert listed["loosePieces"] == [{"kit": listed["loosePieces"][0]["kit"], "piece": "testKitPlank", "placements": 1}]


def testAStructureStandingOnAnEarlierOneIsStaleWhenThatOneMoves(stageBlenderServer, tmp_path):
  walkway = {"width": 4, "deck": "testKitPlank", "posts": legs}

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildWalkway", {"name": "plazaDeck", "kitPath": kitPath, "points": [[20, -10, 6], [60, -10, 6]]} | walkway)
    await session.expectSuccess("buildStairs", {"name": "deckFlight", "kitPath": kitPath, "bottom": [40, -30, 0], "top": [40, -11.9, 6], "width": 4, "tread": "testKitTread"})
    await session.expectSuccess("buildBridge", {"name": "gorgeBridge", "kitPath": kitPath} | bridge)
    await session.expectSuccess("buildWalkway", {"name": "highWalk", "kitPath": kitPath, "points": [[-110, 2, 12], [-60, 2, 12]]} | walkway)
    before = await session.expectSuccess("getStructures")
    await session.expectSuccess("editStructure", {"name": "plazaDeck", "changes": {"points": [[20, -10, 8], [60, -10, 8]]}})
    raised = await session.expectSuccess("getStructures")
    relaid = await session.expectSuccess("editStructure", {"name": "deckFlight", "changes": {"top": [40, -11.9, 8]}})
    after = await session.expectSuccess("getStructures")
    return before, raised, relaid, after

  before, raised, relaid, after = stageBlenderServer.session(steps)
  assert [entry["name"] for entry in before["structures"]] == ["plazaDeck", "deckFlight", "gorgeBridge", "highWalk"]
  assert not any(entry["stale"] for entry in before["structures"])
  assert "plazaDeck" in staleness(before, "deckFlight")["standsOn"]
  assert staleness(raised, "deckFlight")["stale"] and staleness(raised, "deckFlight")["why"] == ["ground"]
  assert not staleness(raised, "plazaDeck")["stale"]
  assert relaid["footingGaps"]["head"] == 0.0 and relaid["walk"]["walkable"]
  assert not any(entry["stale"] for entry in after["structures"])


def testARefusedEditLeavesTheStructureExactlyAsItWas(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildBridge", {"name": "gorgeBridge", "kitPath": kitPath, "posts": {"piece": "testKitLeg", "spacing": 20}} | bridge)
    await session.expectSuccess("buildWall", wall(kitPath, "slopeWall", [[30, -45], [30, -95]]))
    names = ["gorgeBridge"] + [f"slopeWallSection0{index}" for index in (1, 2)]
    before = await structurePlots.parts(session, names)
    refusals = [
      await session.expectError("editStructure", {"name": "gorgeBridge", "changes": {"profile": {"sag": 40}}}),
      await session.expectError("editStructure", {"name": "slopeWall", "changes": {"path": [[30, -45], [30, -96]]}}),
      await session.expectError("editStructure", {"name": "gorgeBridge", "changes": {"spacing": 3}}),
    ]
    after = await structurePlots.parts(session, names)
    summary = await session.expectSuccess("getSceneSummary")
    return before, refusals, after, summary

  before, refusals, after, summary = stageBlenderServer.session(steps)
  assert "the largest sag that fits" in refusals[0] and "no set of the modules" in refusals[1]
  assert "['spacing'] are not in a bridge's definition" in refusals[2]
  assert after == before
  assert not any(name.endswith("Laying") for name in summary["collections"])


def testRemovingAStructureReturnsADefinitionThatBuildsItAgainTheSame(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    first = await session.expectSuccess("buildWalkway", {
      "name": "cliffWalk", "kitPath": kitPath, "points": [[110, 6, 0], [70, 6, 10], [70, -20, 10], [70, -50, 22]], "width": 4, "deck": "testKitPlank",
      "treads": "testKitTread", "stairLegs": [2], "posts": legs, "brackets": {"piece": "testKitBeam", "side": "right", "reach": 10, "legs": [0]},
    })
    builtWall = await session.expectSuccess("buildWall", wall(kitPath, "slopeWall", [[30, -45], [30, -95]]))
    firstParts = await structurePlots.parts(session, ["cliffWalk"] + [section["name"] for section in builtWall["sections"]])
    removedWalk = await session.expectSuccess("removeStructure", {"name": "cliffWalk"})
    removedWall = await session.expectSuccess("removeStructure", {"name": "slopeWall"})
    emptied = await session.expectSuccess("runPython", {"code": "import bpy\nresult = [mesh.name for mesh in bpy.data.meshes if 'zonewrightShearOf' in mesh]"})
    second = await session.expectSuccess("buildWalkway", {"name": "cliffWalk"} | removedWalk["definition"])
    secondWall = await session.expectSuccess("buildWall", {"name": "slopeWall"} | removedWall["definition"])
    secondParts = await structurePlots.parts(session, ["cliffWalk"] + [section["name"] for section in secondWall["sections"]])
    return first, second, firstParts, secondParts, removedWalk, emptied

  first, second, firstParts, secondParts, removedWalk, emptied = stageBlenderServer.session(steps)
  assert removedWalk["kind"] == "walkway" and removedWalk["removedParts"] == ["cliffWalk"]
  assert emptied["result"] == []
  assert [part["name"] for part in second["parts"]] == [part["name"] for part in first["parts"]]
  assert [part["triangles"] for part in second["parts"]] == [part["triangles"] for part in first["parts"]]
  assert sorted(secondParts) == sorted(firstParts)
  for name, part in firstParts.items():
    again = secondParts[name]
    assert len(again["points"]) == len(part["points"])
    assert all(abs(a - b) <= 1e-5 for point, other in zip(part["points"], again["points"]) for a, b in zip(point, other))


def testStructurePartsRefuseHandEditsNamingTheStructureTools(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildBridge", {"name": "gorgeBridge", "kitPath": kitPath} | bridge)
    built = await session.expectSuccess("buildWall", wall(kitPath, "plazaWall", [[16, -20], [66, -20]]))
    section = built["sections"][0]["name"]
    return [
      await session.expectError("transformObjects", {"names": ["gorgeBridge"], "translate": [0, 0, 1]}),
      await session.expectError("deleteObjects", {"names": [section]}),
      await session.expectError("organize", {"renames": {section: "loose"}}),
      await session.expectError("moveVertices", {"objectName": "gorgeBridge", "selector": {"all": True}, "offset": [0, 0, 1]}),
      await session.expectError("assignMaterial", {"objectName": "gorgeBridge", "materialName": "testPlotGround"}),
      await session.expectError("swapKitPiece", {"names": [section], "piece": "testKitWall25"}),
    ]

  refusals = stageBlenderServer.session(steps)
  for refusal, tool in zip(refusals, ("transformObjects", "deleteObjects", "organize", "moveVertices", "assignMaterial", "swapKitPiece")):
    assert f"so {tool} cannot change it: editStructure changes it, removeStructure takes it back" in refusal, refusal


def testAStructuresWalkLineIsWalkedByName(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildBridge", {"name": "gorgeBridge", "kitPath": kitPath, "profile": {"sag": 6}} | bridge)
    walked = await session.expectSuccess("walkRoute", {"route": "gorgeBridge"})
    await session.expectSuccess("buildWall", wall(kitPath, "plazaWall", [[16, -20], [66, -20]]))
    notSpan = await session.expectError("walkRoute", {"route": "plazaWall"})
    await session.expectSuccess("saveReviewRoute", {"name": "rimPath", "path": [[-110, 10, 0], [-104, 10, 0]]})
    saved = await session.expectSuccess("walkRoute", {"route": "rimPath"})
    return built, walked, notSpan, saved

  built, walked, notSpan, saved = stageBlenderServer.session(steps)
  assert walked == built["walk"]
  assert walked["walkable"] and walked["length"] > 80 and walked["profile"][0]["at"][0] == -105.0
  assert "only bridges, flights, and walkways have a walk line" in notSpan
  assert saved["length"] == 6.0


def testRegradeTerrainNamesStaleStructures(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildWall", wall(kitPath, "plazaWall", [[16, -20], [66, -20]]))
    await session.expectSuccess("buildWall", wall(kitPath, "farWall", [[16, 0], [66, 0]]))
    await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "cutting", "points": [[40, -40, -4], [40, -30, -4]], "width": 8, "fillBatterDegrees": None})
    return await session.expectSuccess("regradeTerrain", {"objectName": "ground"})

  regraded = stageBlenderServer.session(steps)
  assert regraded["staleStructures"] == [{"structure": "plazaWall", "why": ["ground"]}]
