import structurePlots

prefab = "testKitHouse"
partNames = {"exterior": "testKitHouseExterior", "interior": "testKitHouseInterior", "roof": "testKitHouseRoof"}
footprintHalf = structurePlots.houseFootprintHalf
plinth = {"material": "testPlotGround", "worldUnitsPerRepeat": 8, "sink": 2}
readParts = """
found = {}
for name in names:
  collection = bpy.data.collections[name]
  found[name] = {'members': sorted(member.name for member in collection.objects), 'offset': [round(value, 4) for value in collection.instance_offset]}
result = found
"""


async def place(session, name, kitPath, **arguments):
  return await session.expectSuccess("placePrefab", {"name": name, "kitPath": str(kitPath), "prefab": prefab} | arguments)


async def flatZone(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0], "collection": "terrain"})


def worldBounds(detail):
  return detail["worldMinimum"], detail["worldMaximum"]


def testAPrefabGathersPlacedPiecesIntoItsPartsWithItsOriginAtItsFootprint(stageBlenderServer, tmp_path):
  async def steps(session):
    await structurePlots.testPrefab(session, tmp_path)
    detail = {name: await session.expectSuccess("getObjectDetail", {"name": name}) for name in ("houseNorth", "houseFloor")}
    pieces = {}
    for name in ("testKitWall25", "testKitWall25Door", "testKitWall12", "testKitPost", "testKitFloor", "testKitGableRoof"):
      pieces[name] = (await session.expectSuccess("getObjectDetail", {"name": name}))["kitPiece"]["triangles"]
    gathered = (await session.expectSuccess("runPython", {"code": f"names = {list(partNames.values()) + [prefab]!r}\n" + readParts}))["result"]
    again = await session.expectSuccess("assemblePrefab", {"name": prefab, "parts": structurePlots.houseParts, "entrances": [structurePlots.houseDoor]})
    return detail, pieces, gathered, again

  detail, pieces, gathered, assembled = stageBlenderServer.session(steps)
  center = list(structurePlots.houseCenter) + [0.0]
  for part, collection in partNames.items():
    assert gathered[collection]["members"] == sorted(structurePlots.houseParts[part]), part
    assert gathered[collection]["offset"] == center, part
  assert gathered[prefab]["offset"] == center and gathered[prefab]["members"] == []
  half = footprintHalf
  assert assembled["footprint"] == [[-half[0], -half[1]], [half[0], -half[1]], [half[0], half[1]], [-half[0], half[1]]]
  assert assembled["footprintSize"] == [2 * half[0], 2 * half[1]]
  assert assembled["origin"] == center
  assert assembled["entrances"] == [{"name": "front", "at": [-12.5, 18.75, 0.0], "facingDegrees": 0.0}]
  counts = {part["part"]: part for part in assembled["parts"]}
  pieceOf = {name: piece for name, (piece, _, _) in (structurePlots.houseWalls | structurePlots.housePosts).items()}
  assert counts["exterior"]["triangles"] == sum(pieces[pieceOf[name]] for name in structurePlots.houseParts["exterior"])
  assert counts["interior"]["triangles"] == pieces["testKitFloor"] and counts["roof"]["triangles"] == pieces["testKitGableRoof"]
  assert assembled["triangles"] == sum(part["triangles"] for part in assembled["parts"])
  assert detail["houseNorth"]["prefabPart"] == {"prefab": prefab, "part": "exterior"}
  assert detail["houseFloor"]["prefabPart"] == {"prefab": prefab, "part": "interior"}


def testAssemblingAgainReplacesThePartsAndReturnsTheRestToTheScene(stageBlenderServer, tmp_path):
  async def steps(session):
    await structurePlots.testPrefab(session, tmp_path)
    walls = [name for name in structurePlots.houseParts["exterior"] if not name.startswith("housePost")]
    again = await session.expectSuccess("assemblePrefab", {"name": prefab, "parts": {"exterior": walls, "floors": ["houseFloor"]}, "entrances": [structurePlots.houseDoor]})
    code = f"names = {[partNames['exterior'], 'testKitHouseFloors']!r}\n" + readParts + "\nresult['scene'] = sorted(member.name for member in bpy.context.scene.collection.objects)\nresult['children'] = sorted(child.name for child in bpy.data.collections['testKitHouse'].children)"
    gathered = (await session.expectSuccess("runPython", {"code": code}))["result"]
    return again, gathered

  again, gathered = stageBlenderServer.session(steps)
  assert [part["part"] for part in again["parts"]] == ["exterior", "floors"]
  assert gathered["children"] == [partNames["exterior"], "testKitHouseFloors"]
  assert gathered[partNames["exterior"]]["members"] == sorted(structurePlots.houseWalls)
  assert gathered["testKitHouseFloors"]["members"] == ["houseFloor"]
  assert {"housePostNE", "housePostSE", "housePostSW", "housePostNW", "houseRoof"} <= set(gathered["scene"])
  assert again["footprint"] == [[-30.0, -23.75], [30.0, -23.75], [30.0, 23.75], [-30.0, 23.75]], again["footprint"]


def testPrefabRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await structurePlots.testPrefab(session, tmp_path)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "plainBlock", "size": [4, 4, 4], "location": [700, 0, 0]})
    parts = structurePlots.houseParts
    refusals = {
      "twoParts": await session.expectError("assemblePrefab", {"name": "testKitShed", "parts": {"exterior": ["houseNorth"], "roof": ["houseNorth"]}}),
      "plainMesh": await session.expectError("assemblePrefab", {"name": "testKitShed", "parts": {"exterior": ["plainBlock"]}}),
      "noEntrance": await session.expectError("assemblePrefab", {"name": prefab, "parts": parts}),
      "outside": await session.expectError("assemblePrefab", {"name": prefab, "parts": parts, "entrances": [structurePlots.houseDoor | {"at": [500, 340, 0]}]}),
      "stem": await session.expectError("assemblePrefab", {"name": "shed", "parts": {"exterior": ["plainBlock"]}}),
      "otherPrefab": await session.expectError("assemblePrefab", {"name": "testKitShed", "parts": {"exterior": ["houseNorth"]}}),
      "partName": await session.expectError("assemblePrefab", {"name": "testKitShed", "parts": {"Exterior": ["plainBlock"]}}),
      "piece": await session.expectError("assemblePrefab", {"name": "testKitWall25", "parts": {"exterior": ["houseNorth"]}}),
    }
    summary = await session.expectSuccess("getSceneSummary")
    return refusals, summary

  refusals, summary = stageBlenderServer.session(steps)
  assert "'houseNorth' is named in parts 'exterior' and 'roof'" in refusals["twoParts"]
  assert "'plainBlock' is not an instance of one of this file's pieces" in refusals["plainMesh"]
  assert "interior part is walked into" in refusals["noEntrance"]
  assert "Entrance 'front'" in refusals["outside"] and "15.25 outside the building's footprint" in refusals["outside"]
  assert "must start with the kit file's stem 'testKit'" in refusals["stem"]
  assert "'houseNorth' is part 'exterior' of prefab 'testKitHouse'" in refusals["otherPrefab"]
  assert "camelCase" in refusals["partName"]
  assert "'testKitWall25' is already the name of a collection in this file that is not a prefab" in refusals["piece"]
  assert "testKitShed" not in summary["collections"]


def testNestedInstancesAreStoodOnSettledOntoAndSectioned(stageBlenderServer, tmp_path):
  section = """
import bridgeSketch
cuts = bridgeSketch.sectionCuts([-60.0, 0.0], [60.0, 0.0], -10.0, 80.0, ['ground'])['ground']
result = sorted(set(round(value, 2) for s0, z0, s1, z1 in cuts for value in (s0, s1) if max(z0, z1) >= 30.0))
"""

  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await flatZone(session)
    placed = await place(session, "house", kitPath, location=[0, 0, 1], facingDegrees=0)
    walk = await session.expectSuccess("walkRoute", {"path": [[-10, -10, 1], [10, -10, 1]], "sampleSpacing": 2})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [2, 2, 2], "location": [5, 5, 20]})
    settled = await session.expectSuccess("settleObjects", {"names": ["crate"], "onto": "houseInterior"})
    crate = await session.expectSuccess("getObjectDetail", {"name": "crate"})
    tall = (await session.expectSuccess("runPython", {"code": section}))["result"]
    return placed, walk, settled, crate, tall

  placed, walk, settled, crate, tall = stageBlenderServer.session(steps)
  assert placed["floor"] == 1.0 and placed["plinth"] is None
  assert {part["name"] for part in placed["parts"]} == {"houseExterior", "houseInterior", "houseRoof"}
  assert walk["walkable"] is True
  assert {row["at"][2] for row in walk["profile"]} == {1.0}
  assert abs(crate["worldMinimum"][2] - 1.0) <= 0.01, settled
  # The section along y = 0 runs through the west and east walls (their faces at x -30 and -20, 20 and 30) up to their tops at 31.
  assert {30.0, 40.0, 80.0, 90.0} <= set(tall)


def testAPrefabPlacedTwiceSharesItsPartCollections(stageBlenderServer, tmp_path):
  readInstances = """
result = {name: bpy.data.objects[name].instance_collection.name for name in names}
result['libraries'] = len(bpy.data.libraries)
result['partCollections'] = sorted(collection.name for collection in bpy.data.collections if collection.name.startswith('testKitHouse'))
"""

  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await flatZone(session)
    first = await place(session, "houseA", kitPath, location=[-40, 0], facingDegrees=0)
    second = await place(session, "houseB", kitPath, location=[40, 0], facingDegrees=90)
    names = [f"house{which}{part}" for which in "AB" for part in ("Exterior", "Interior", "Roof")]
    instances = (await session.expectSuccess("runPython", {"code": f"names = {names!r}\n" + readInstances}))["result"]
    return first, second, instances

  first, second, instances = stageBlenderServer.session(steps)
  for part in ("Exterior", "Interior", "Roof"):
    assert instances[f"houseA{part}"] == instances[f"houseB{part}"] == f"testKitHouse{part}"
  assert instances["libraries"] == 1
  assert instances["partCollections"] == ["testKitHouse", "testKitHouseExterior", "testKitHouseInterior", "testKitHouseRoof"]
  assert [part["model"] for part in first["parts"]] == [part["model"] for part in second["parts"]] == [
    "obj_testkithouseexterior.mod", "obj_testkithouseinterior.mod", "obj_testkithouseroof.mod",
  ]


def testAPrefabOnTheSlopeSeatsOnItsHighestGroundOverAPlinthToItsLowest(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    placed = await place(session, "cottage", kitPath, location=[55, -80], facingDegrees=0, plinth=plinth)
    detail = await session.expectSuccess("getObjectDetail", {"name": "cottagePlinth"})
    interior = await session.expectSuccess("getObjectDetail", {"name": "cottageInterior"})
    return placed, detail, interior

  placed, detail, interior = stageBlenderServer.session(steps)
  highest = structurePlots.slopeHeight(-80 - footprintHalf[1])
  lowest = structurePlots.slopeHeight(-80 + footprintHalf[1])
  assert abs(placed["floor"] - highest) <= 0.01
  assert abs(placed["ground"]["highest"] - highest) <= 0.01 and abs(placed["ground"]["lowest"] - lowest) <= 0.01
  assert abs(placed["plinth"]["bottom"] - (lowest - 2)) <= 0.01 and abs(placed["plinth"]["top"] - highest) <= 0.01
  assert placed["plinth"]["triangles"] == 8
  low, high = worldBounds(detail)
  assert abs(low[2] - (lowest - 2)) <= 0.01 and abs(high[2] - highest) <= 0.01
  assert [abs(value) for value in (low[0] - 55, high[0] - 55, low[1] + 80, high[1] + 80)] == [footprintHalf[0], footprintHalf[0], footprintHalf[1], footprintHalf[1]]
  assert abs(interior["location"][2] - highest) <= 0.01
  assert detail["structurePart"] == {"structure": "cottage", "kind": "prefab", "role": "plinth"}
  assert interior["structurePart"] == {"structure": "cottage", "kind": "prefab", "role": "interior"}
  assert [entry["material"] for entry in detail["materials"]] == ["testPlotGround"] and abs(detail["worldUnitsPerTextureRepeat"] - 8) <= 0.01


def testPlacePrefabRefusesBuryingFloatingAndRockOverGround(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "overhang", "size": [30, 30, 2], "location": [60, -10, 20]})
    before = await session.expectSuccess("getSceneSummary")
    refusals = {
      "buried": await session.expectError("placePrefab", {"name": "cottage", "kitPath": str(kitPath), "prefab": prefab, "location": [55, -80, 10], "facingDegrees": 0}),
      "floating": await session.expectError("placePrefab", {"name": "cottage", "kitPath": str(kitPath), "prefab": prefab, "location": [55, -80, 30], "facingDegrees": 0}),
      "rock": await session.expectError("placePrefab", {"name": "cottage", "kitPath": str(kitPath), "prefab": prefab, "location": [60, -10], "facingDegrees": 0}),
      "terrain": await session.expectError("placePrefab", {"name": "cottage", "kitPath": str(kitPath), "prefab": prefab, "location": [55, -80], "facingDegrees": 0, "collection": "terrain"}),
      "missing": await session.expectError("placePrefab", {"name": "cottage", "kitPath": str(kitPath), "prefab": "testKitBarn", "location": [55, -80], "facingDegrees": 0}),
      "taken": await session.expectError("placePrefab", {"name": "overhang", "kitPath": str(kitPath), "prefab": prefab, "location": [55, -80], "facingDegrees": 0}),
    }
    after = await session.expectSuccess("getSceneSummary")
    structures = await session.expectSuccess("getStructures", {})
    return refusals, before, after, structures

  refusals, before, after, structures = stageBlenderServer.session(steps)
  buriedBy = structurePlots.slopeHeight(-80 - footprintHalf[1]) - 10
  assert f"inside the footprint stands {buriedBy:.2f} over the floor at 10.00" in refusals["buried"] and "-104.8] inside the footprint" in refusals["buried"]
  floatBy = 30 - structurePlots.slopeHeight(-80 + footprintHalf[1])
  assert f"stands {floatBy:.2f} over the ground at [" in refusals["floating"] and "-55.2]" in refusals["floating"] and "give a plinth" in refusals["floating"]
  assert "rock lies over ground" in refusals["rock"] and "give z" in refusals["rock"]
  assert "A building is placed parts, not ground" in refusals["terrain"]
  assert "holds no prefab 'testKitBarn'" in refusals["missing"] and prefab in refusals["missing"]
  assert "'overhang' is already the name of an object" in refusals["taken"]
  assert after["objects"] == before["objects"] and after["collections"] == before["collections"] and after["libraries"] == before["libraries"]
  assert structures["structures"] == []


def testAWalkInBuildingIsWalkedIntoThroughEachEntrance(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await flatZone(session)
    return await place(session, "inn", kitPath, location=[0, 0], facingDegrees=90)

  placed = stageBlenderServer.session(steps)
  (entrance,) = placed["entrances"]
  assert entrance["name"] == "front" and entrance["at"] == [18.75, 12.5, 0.0] and entrance["facingDegrees"] == 90.0
  assert entrance["groundOutside"] == 0.0 and entrance["stepUp"] == 0.0
  walk = entrance["walk"]
  assert walk["path"] == [[28.75, 12.5, 0.0], [8.75, 12.5, 0.0]]
  assert walk["walkable"] is True, walk["problems"]
  assert walk["lowestHeadroom"]["headroom"] >= structurePlots.houseDoorHeight - 0.05
  assert placed["views"]["entranceFront"] == {"standAt": [43.75, 12.5], "headingDegrees": 270.0, "pitchDegrees": 0.0}
  assert placed["views"]["orbit"] == {"objects": ["innExterior", "innInterior", "innRoof"]}


def testAPrefabFollowsItsKitAndGoesStaleWhenItsFootprintChanges(stageBlenderServer, tmp_path):
  zonePath = tmp_path / "zones" / "zone.blend"
  zonePath.parent.mkdir()

  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await flatZone(session)
    placed = await place(session, "house", kitPath, location=[0, 0], facingDegrees=0)
    await session.expectSuccess("saveFile", {"path": str(zonePath)})
    await session.expectSuccess("openFile", {"path": str(kitPath)})
    cut = await session.expectSuccess("cutOpening", {"piece": "testKitWall25", "kind": "window", "along": 0, "width": 8, "height": 8, "sill": 12})
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("openFile", {"path": str(zonePath)})
    followed = (await session.expectSuccess("getStructures", {}))["structures"][0]
    await session.expectSuccess("openFile", {"path": str(kitPath)})
    await session.expectSuccess("placeKitPiece", {"name": "housePorchPost", "kitPath": None, "piece": "testKitPost", "location": [500, 340, 0], "facingDegrees": 0})
    wider = await session.expectSuccess("assemblePrefab", {
      "name": prefab, "parts": structurePlots.houseParts | {"exterior": structurePlots.houseParts["exterior"] + ["housePorchPost"]}, "entrances": [structurePlots.houseDoor],
    })
    await session.expectSuccess("saveFile", {})
    await session.expectSuccess("openFile", {"path": str(zonePath)})
    stale = (await session.expectSuccess("getStructures", {}))["structures"][0]
    return placed, cut, followed, wider, stale

  placed, cut, followed, wider, stale = stageBlenderServer.session(steps)
  wallsCut = sum(1 for piece, _, _ in structurePlots.houseWalls.values() if piece == "testKitWall25")
  added = cut["triangles"]["after"] - cut["triangles"]["before"]
  before = {part["name"]: part["triangles"] for part in placed["parts"]}
  after = {part["name"]: part["triangles"] for part in followed["parts"]}
  assert after["houseExterior"] - before["houseExterior"] == wallsCut * added > 0
  assert after["houseRoof"] == before["houseRoof"] and after["houseInterior"] == before["houseInterior"]
  assert followed["stale"] is False and followed["why"] == []
  assert wider["footprintSize"] == [62.0, 70.75]
  assert stale["stale"] is True and stale["why"] == ["kit"] and stale["kitChanged"] == [prefab]


def testEditingAndRemovingAPlacedPrefab(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    placed = await place(session, "cottage", kitPath, location=[55, -80], facingDegrees=0, plinth=plinth)
    edited = await session.expectSuccess("editStructure", {"name": "cottage", "changes": {"facingDegrees": 90}})
    details = {name: await session.expectSuccess("getObjectDetail", {"name": name}) for name in ("cottageExterior", "cottagePlinth")}
    unknown = await session.expectError("editStructure", {"name": "cottage", "changes": {"width": 4}})
    removed = await session.expectSuccess("removeStructure", {"name": "cottage"})
    gone = await session.expectSuccess("getSceneSummary")
    rebuilt = await session.expectSuccess("placePrefab", {"name": removed["name"]} | removed["definition"])
    return placed, edited, details, unknown, removed, gone, rebuilt

  placed, edited, details, unknown, removed, gone, rebuilt = stageBlenderServer.session(steps)
  assert edited["changes"] == {"facingDegrees": 90} and edited["facingDegrees"] == 90.0
  assert details["cottageExterior"]["rotationDegrees"][2] == -90.0
  low, high = worldBounds(details["cottagePlinth"])
  assert [round(high[0] - low[0], 3), round(high[1] - low[1], 3)] == [2 * footprintHalf[1], 2 * footprintHalf[0]]
  highest = structurePlots.slopeHeight(-80 - footprintHalf[0])
  assert abs(edited["floor"] - highest) <= 0.01 and abs(low[2] - (structurePlots.slopeHeight(-80 + footprintHalf[0]) - 2)) <= 0.01
  assert edited["structure"] == placed["structure"]
  assert "['width'] are not in a prefab's definition" in unknown and "facingDegrees" in unknown
  assert removed["kind"] == "prefab" and removed["definition"]["facingDegrees"] == 90.0 and removed["definition"]["plinth"] == {"material": "testPlotGround", "worldUnitsPerRepeat": 8.0, "sink": 2.0}
  assert not any(entry["name"].startswith("cottage") for entry in gone["objects"]) and "cottage" not in gone["collections"]
  assert [(part["name"], part["triangles"]) for part in rebuilt["parts"]] == [(part["name"], part["triangles"]) for part in edited["parts"]]
  assert rebuilt["floor"] == edited["floor"]


def testGroundMovedUnderAPlacedPrefabMakesItStaleAndLayingAgainSeatsIt(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    placed = await place(session, "house", kitPath, location=[60, -14], facingDegrees=0, plinth=plinth)
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [60, -14, 0], "radius": 12, "strength": 3})
    stale = (await session.expectSuccess("getStructures", {"names": ["house"]}))["structures"][0]
    relaid = await session.expectSuccess("editStructure", {"name": "house"})
    fresh = (await session.expectSuccess("getStructures", {"names": ["house"]}))["structures"][0]
    return placed, stale, relaid, fresh

  placed, stale, relaid, fresh = stageBlenderServer.session(steps)
  assert placed["floor"] == 0.0
  assert stale["stale"] is True and stale["why"] == ["ground"]
  change = stale["ground"]
  assert change["moved"] > 0 and abs(change["largest"]["change"] - relaid["floor"]) <= 0.01 and change["largest"]["before"] == 0.0
  assert abs(change["largest"]["at"][0] - 60) <= 4 and abs(change["largest"]["at"][1] + 14) <= 4
  assert relaid["probesMovedSinceLaid"]["moved"] == change["moved"] and relaid["floor"] > 2
  assert relaid["plinth"]["bottom"] == -2.0
  assert fresh["stale"] is False


def testPrefabPartsRefuseHandEditsNamingTheStructureTools(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await structurePlots.testPrefab(session, tmp_path)
    await structurePlots.testPlot(session, tmp_path)
    await place(session, "cottage", kitPath, location=[55, -80], facingDegrees=0, plinth=plinth)
    return {
      "transform": await session.expectError("transformObjects", {"names": ["cottageRoof"], "translate": [0, 0, 1]}),
      "delete": await session.expectError("deleteObjects", {"names": ["cottageExterior"]}),
      "organize": await session.expectError("organize", {"renames": {"cottageInterior": "inside"}}),
      "material": await session.expectError("assignMaterial", {"objectName": "cottagePlinth", "materialName": "testPlotGround"}),
      "settle": await session.expectError("settleObjects", {"names": ["cottageExterior"]}),
    }

  refusals = stageBlenderServer.session(steps)
  for action, refusal in refusals.items():
    assert "part of structure 'cottage'" in refusal and "editStructure changes it, removeStructure takes it back" in refusal, (action, refusal)
