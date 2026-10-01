from conftest import pinnedBlender


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def linkedArchives(found, tier):
  return [(definition["archive"], definition["via"]) for definition in found["linked"].get(tier, [])]


def testFindModelFollowsTheClientsLinks(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  # guka_chr.txt loads only SPI and WIL from gfaydark_chr.s3d; BAT is in that archive too but not linked to guka.
  spider, _ = server.callToolExpectingSuccess("findModel", {"model": "SPI_ACTORDEF", "zone": "guka"})
  willOWisp, _ = server.callToolExpectingSuccess("findModel", {"model": "WIL", "zone": "guka"})
  bat, _ = server.callToolExpectingSuccess("findModel", {"model": "BAT", "zone": "guka"})
  # eqgame.exe loads poknowledge_obj3.eqg for the Plane of Knowledge by zone id.
  portal, _ = server.callToolExpectingSuccess("findModel", {"model": "PORTBASE", "zone": "poknowledge"})
  kiln, _ = server.callToolExpectingSuccess("findModel", {"model": "IT10800_ACTORDEF", "zone": "neighborhood"})
  twice, _ = server.callToolExpectingSuccess("findModel", {"model": "IT67", "zone": "neighborhood"})
  darkElf, _ = server.callToolExpectingSuccess("findModel", {"model": "DAF"})
  lizard, _ = server.callToolExpectingSuccess("findModel", {"model": "LIZ"})
  notAZone = server.callToolExpectingError("findModel", {"model": "DAF", "zone": "nowhere"})

  assert spider["resolves"]["archive"] == "gfaydark_chr.s3d" and spider["resolves"]["via"] == "guka_chr.txt"
  assert willOWisp["resolves"]["archive"] == "gfaydark_chr.s3d" and willOWisp["resolves"]["via"] == "guka_chr.txt"
  assert "gfaydark_chr.s3d" in bat["unlinkedArchives"]
  assert bat["resolves"]["error"].startswith("No archive zone 'guka' links defines model 'bat'; unlinked archives define it:")
  assert linkedArchives(portal, "zone") == [("poknowledge_obj3.eqg", "eqgame.exe")]
  assert (kiln["resolves"]["tier"], kiln["resolves"]["archive"], kiln["resolves"]["via"]) == ("zone", "tradeskill_objects.eqg", "neighborhood_assets.txt")
  assert sorted(linkedArchives(twice, "global")) == [("equipment-01.eqg", "GlobalLoad.txt"), ("gequip.s3d", "GlobalLoad.txt")]
  assert "is defined 2 times in the global tier" in twice["resolves"]["error"]
  # Luclin models are on in this client's eqclient.ini, so the player model wins over global_chr.s3d's classic one.
  assert (darkElf["resolves"]["tier"], darkElf["resolves"]["archive"], darkElf["resolves"]["via"]) == ("playerModels", "globaldaf_chr.s3d", "eqclient.ini")
  assert ("global_chr.s3d", "GlobalLoad.txt") in linkedArchives(darkElf, "global")
  assert (lizard["resolves"]["tier"], lizard["resolves"]["archive"], lizard["resolves"]["via"]) == ("global", "liz_chr.s3d", "Resources/GlobalLoad_chr.txt")
  assert "'nowhere' is not a zone" in notAZone


def testPlaceSpawnDrawsLinkedModelsAtTheirEQSize(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    darkElf = await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "darkElf", "size": 5, "location": [0, 0, 20], "headingDegrees": 0})
    kobold = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "KOB_ACTORDEF", "name": "kobold", "size": 4, "eqLocation": [20, 10, 3], "eqHeading": 128, "textureSet": 1})
    gnoll = await session.expectSuccess("placeSpawn", {"zone": None, "model": "GBN", "name": "gnoll", "size": 6, "location": [-10, 0, 0], "headingDegrees": 90, "headType": 3, "snapToGround": False})
    bothFrames = await session.expectError("placeSpawn", {"zone": None, "model": "DAF", "name": "twice", "size": 5, "location": [0, 0, 0], "headingDegrees": 0, "eqLocation": [0, 0, 0], "eqHeading": 0})
    unlinked = await session.expectError("placeSpawn", {"zone": "neighborhood", "model": "AEL", "name": "nobody", "size": 5, "location": [0, 0, 0], "headingDegrees": 0})
    return darkElf, kobold, gnoll, bothFrames, unlinked

  darkElf, kobold, gnoll, bothFrames, unlinked = stageBlenderServer.session(steps)
  assert darkElf["dimensions"][2] == 5.0
  assert darkElf["anchor"] == [0.0, 0.0, 0.0]
  assert darkElf["rotationDegrees"][2] == 90.0
  assert (darkElf["source"]["archive"], darkElf["source"]["linkedBy"], darkElf["source"]["pose"]) == ("globaldaf_chr.s3d", "eqclient.ini", "armsLowered")
  # The server's (x, y, z) is Blender's (y, x, z); heading 128 of 512 is a quarter turn clockwise.
  assert kobold["anchor"] == [10.0, 20.0, 0.0]
  assert kobold["rotationDegrees"][2] == -90.0
  assert kobold["dimensions"][2] == 4.0
  assert (kobold["source"]["archive"], kobold["source"]["tier"]) == ("poknowledge_chr.s3d", "zone")
  assert kobold["source"]["swappedMaterials"] == 12
  assert gnoll["source"]["pieces"] == ["GBN00", "GBNHE03"]
  assert (gnoll["source"]["tier"], gnoll["source"]["pose"]) == ("onDemand", "bind")
  assert "Give one pair" in bothFrames
  assert "unlinked archives define it: ['ael_chr.s3d', 'global5_chr.s3d']" in unlinked


def testDoorsAndObjectsComeFromTheZonesArchives(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    door = await session.expectSuccess("placeDoor", {"zone": "poknowledge", "model": "POKDOOR500", "name": "door", "eqLocation": [0, 0, 0], "eqHeading": 0})
    bigDoor = await session.expectSuccess("placeDoor", {"zone": "poknowledge", "model": "POKDOOR500", "name": "bigDoor", "location": [50, 0, 0], "headingDegrees": 90, "scalePercent": 150})
    kiln = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "IT10800_ACTORDEF", "name": "kiln", "location": [0, 30, 0], "headingDegrees": 0})
    tree = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "OBJ_TREEM", "name": "tree", "location": [30, 30, 0], "headingDegrees": 0})
    ambiguous = await session.expectError("placeObject", {"zone": "neighborhood", "model": "IT67", "name": "it67", "location": [0, 60, 0], "headingDegrees": 0})
    chosen = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "IT67", "name": "it67", "location": [0, 60, 0], "headingDegrees": 0, "source": "gequip.s3d"})
    treeDetail = await session.expectSuccess("getObjectDetail", {"name": "tree"})
    return door, bigDoor, kiln, tree, ambiguous, chosen, treeDetail

  door, bigDoor, kiln, tree, ambiguous, chosen, treeDetail = stageBlenderServer.session(steps)
  assert (door["source"]["archive"], door["source"]["linkedBy"], door["source"]["kind"]) == ("poknowledge_obj.s3d", "zone load order", "wldStatic")
  assert door["anchor"] == [0.0, 0.0, 0.0]
  assert abs(bigDoor["dimensions"][2] - 1.5 * door["dimensions"][2]) < 0.002
  assert (kiln["source"]["archive"], kiln["source"]["linkedBy"]) == ("tradeskill_objects.eqg", "neighborhood_assets.txt")
  # neighborhood.eqg ships only the bark's normal map; no archive the zone loads has its diffuse, so it draws as missing.
  assert tree["source"]["missingTextures"] == ["ab_dg_treebark_c.dds"]
  assert "eq_missing_ab_dg_treebark_c.dds" in [entry["material"] for entry in treeDetail["materials"]]
  assert "is defined 2 times in the global tier" in ambiguous
  assert chosen["source"]["archive"] == "gequip.s3d"
