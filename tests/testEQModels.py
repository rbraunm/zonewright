from conftest import pinnedBlender


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def linkedArchives(found, tier):
  return [(definition["archive"], definition["via"]) for definition in found["linked"] if definition["tier"] == tier]


def testFindModelFollowsTheClientsLoadOrder(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  # guka_chr.txt loads only SPI and WIL from gfaydark_chr.s3d; BAT is in that archive too but not loaded for guka.
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
  assert bat["resolves"]["error"].startswith("No archive zone 'guka' loads defines model 'bat'; unlinked archives define it:")
  assert linkedArchives(portal, "zone") == [("poknowledge_obj3.eqg", "eqgame.exe")]
  assert (kiln["resolves"]["tier"], kiln["resolves"]["archive"], kiln["resolves"]["via"]) == ("zone", "tradeskill_objects.eqg", "neighborhood_assets.txt")
  # The client keeps the first definition it loads: equipment-01.eqg is GlobalLoad.txt's first line, gequip.s3d a later one.
  assert linkedArchives(twice, "global") == [("equipment-01.eqg", "GlobalLoad.txt"), ("gequip.s3d", "GlobalLoad.txt")]
  assert twice["resolves"]["archive"] == "equipment-01.eqg"
  # The Luclin player model loads before GlobalLoad.txt's phase 3 global_chr.s3d and its classic dark elf.
  assert (darkElf["resolves"]["tier"], darkElf["resolves"]["archive"], darkElf["resolves"]["via"]) == ("global", "globaldaf_chr.s3d", "eqgame.exe startup")
  assert linkedArchives(darkElf, "global") == [("globaldaf_chr.s3d", "eqgame.exe startup"), ("global_chr.s3d", "GlobalLoad.txt")]
  assert (lizard["resolves"]["tier"], lizard["resolves"]["archive"], lizard["resolves"]["via"]) == ("global", "liz_chr.s3d", "Resources/GlobalLoad_chr.txt")
  assert "'nowhere' is not a zone" in notAZone


def testPlaceSpawnDrawsTheClientsScaleForTheZone(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    noZoneFlag = await session.expectError("placeSpawn", {"zone": None, "model": "DAF", "name": "early", "height": 5, "location": [0, 0, 0], "headingDegrees": 0})
    await session.expectSuccess("setZoneProperties", {"newEngineZone": False})
    darkElf = await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "darkElf", "height": 5, "location": [0, 0, 20], "headingDegrees": 0})
    kobold = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "KOB_ACTORDEF", "name": "kobold", "height": 5, "x": 20, "y": 10, "z": 3, "heading": 128, "textureSet": 1})
    gnoll = await session.expectSuccess("placeSpawn", {"zone": None, "model": "GBN", "name": "gnoll", "height": 3, "location": [-10, 0, 0], "headingDegrees": 90, "headType": 3, "snapToGround": False})
    bothFrames = await session.expectError("placeSpawn", {"zone": None, "model": "DAF", "name": "twice", "height": 5, "location": [0, 0, 0], "headingDegrees": 0, "x": 0, "y": 0, "z": 0, "heading": 0})
    noHeight = await session.expectError("placeSpawn", {"zone": None, "model": "DAF", "name": "flat", "height": 0, "location": [0, 0, 0], "headingDegrees": 0})
    airElemental = await session.expectSuccess("placeSpawn", {"zone": "neighborhood", "model": "AEL", "name": "airElemental", "height": 6, "location": [-20, 0, 0], "headingDegrees": 0})
    unlinked = await session.expectError("placeSpawn", {"zone": "neighborhood", "model": "ARM", "name": "nobody", "height": 5, "location": [0, 0, 0], "headingDegrees": 0})
    await session.expectSuccess("setZoneProperties", {"newEngineZone": True})
    newEngineElf = await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "newEngineElf", "height": 5, "location": [30, 0, 0], "headingDegrees": 0})
    newEngineGnoll = await session.expectSuccess("placeSpawn", {"zone": None, "model": "GBN", "name": "newEngineGnoll", "height": 4, "location": [-30, 0, 0], "headingDegrees": 0})
    return noZoneFlag, darkElf, kobold, gnoll, bothFrames, noHeight, airElemental, unlinked, newEngineElf, newEngineGnoll

  noZoneFlag, darkElf, kobold, gnoll, bothFrames, noHeight, airElemental, unlinked, newEngineElf, newEngineGnoll = stageBlenderServer.session(steps)
  assert "Spawns need the zone's newEngineZone" in noZoneFlag
  # A WLD model outside a NewEngineZone zone draws at height / 5 with its origin ROffset (3.125 unless moddat.ini says) times that
  # above the ground; the live dumps record height-5 dark elves and kobolds in poknowledge at avatarHeight 3.125.
  assert (darkElf["height"], darkElf["scale"], darkElf["avatarHeight"]) == (5, 1.0, 3.125)
  assert (darkElf["ground"], darkElf["location"]) == ([0.0, 0.0, 0.0], [0.0, 0.0, 3.125])
  assert darkElf["rotationDegrees"][2] == 90.0
  assert (darkElf["source"]["archive"], darkElf["source"]["linkedBy"]) == ("globaldaf_chr.s3d", "eqgame.exe startup")
  # EQ (x, y, z) is Blender (y, x, z); heading 128 faces EQ +x, which is Blender +Y, so the +X front turns a quarter counter-clockwise.
  assert (kobold["ground"], kobold["location"]) == ([10.0, 20.0, 0.0], [10.0, 20.0, 3.125])
  assert kobold["rotationDegrees"][2] == 90.0
  assert (kobold["source"]["archive"], kobold["source"]["tier"]) == ("poknowledge_chr.s3d", "zone")
  assert kobold["source"]["swappedMaterials"] == 12
  assert gnoll["source"]["pieces"] == ["GBN00", "GBNHE03"]
  assert gnoll["source"]["tier"] == "onDemand"
  assert (gnoll["source"]["pose"]["animation"], gnoll["source"]["pose"]["wldAnimation"], gnoll["source"]["pose"]["resource"]) == ("STND", "P01", "STND_BA_1_GBN")
  # An EQG model outside a NewEngineZone zone draws at height * 1.3 / 6: the dumps' height-3 gnolls in poknowledge stand at 2.03124.
  assert abs(gnoll["avatarHeight"] - 2.03124) < 0.0001
  assert (gnoll["ground"], gnoll["location"]) == (None, [-10.0, 0.0, 0.0])
  assert "Give location and headingDegrees" in bothFrames
  assert "height must be positive, got 0" in noHeight
  # eqgame.exe loads global5_chr.s3d at startup while UseLuclinElementals is on.
  assert (airElemental["source"]["archive"], airElemental["source"]["linkedBy"]) == ("global5_chr.s3d", "eqgame.exe startup")
  assert "unlinked archives define it: ['beholder_chr.s3d', 'nro_chr.s3d', 'oggok_chr.s3d', 'poeartha_chr.s3d', 'postorms_chr.s3d']" in unlinked
  # A NewEngineZone zone draws WLD models 1.3 times smaller and EQG models at height / 6, as the dumps' guild halls and neighborhood record.
  assert abs(newEngineElf["avatarHeight"] - 2.40384) < 0.0001
  assert abs(newEngineElf["dimensions"][2] * 1.3 - darkElf["dimensions"][2]) < 0.002
  assert abs(newEngineGnoll["avatarHeight"] - 2.08333) < 0.0001


def testSpawnsPlayTheAnimationsTheClientGivesThem(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", {"newEngineZone": False})
    stand = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "stand", "height": 5, "location": [0, 0, 0], "headingDegrees": 0})
    wave = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "wave", "height": 5, "location": [5, 0, 0], "headingDegrees": 0, "animation": "wave", "animationFrame": 12})
    walk = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "HUF", "name": "walk", "height": 6, "location": [10, 0, 0], "headingDegrees": 0, "animation": "L01", "animationVariant": "b", "animationFrame": 3})
    erudite = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "ERF", "name": "erudite", "height": 6, "location": [15, 0, 0], "headingDegrees": 0, "animation": "O01"})
    kobold = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "KOB", "name": "kobold", "height": 4, "location": [20, 0, 0], "headingDegrees": 0})
    pastTheEnd = await session.expectError("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "late", "height": 5, "location": [25, 0, 0], "headingDegrees": 0, "animation": "S03", "animationFrame": 27})
    noVariant = await session.expectError("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "variant", "height": 5, "location": [25, 0, 0], "headingDegrees": 0, "animation": "S03", "animationVariant": "C"})
    unknown = await session.expectError("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "dance", "height": 5, "location": [25, 0, 0], "headingDegrees": 0, "animation": "MOONWALK"})
    eqgName = await session.expectSuccess("placeSpawn", {"zone": "poknowledge", "model": "DAF", "name": "run", "height": 5, "location": [30, 0, 0], "headingDegrees": 0, "animation": "NRUN"})
    return stand, wave, walk, erudite, kobold, pastTheEnd, noVariant, unknown, eqgName

  stand, wave, walk, erudite, kobold, pastTheEnd, noVariant, unknown, eqgName = stageBlenderServer.session(steps)
  # eqgame.exe maps a dark elf (DA..) to the elf's animations (EL..) when it has none of its own.
  assert {key: stand["source"]["pose"][key] for key in ("animation", "resource", "archive", "borrowedFrom", "variant", "frame")} == {
    "animation": "P01", "resource": "P01AELF", "archive": "globalelf_chr.s3d", "borrowedFrom": "ELF", "variant": "A", "frame": 0,
  }
  assert (wave["source"]["pose"]["animation"], wave["source"]["pose"]["resource"], wave["source"]["pose"]["frameCount"], wave["source"]["pose"]["millisecondsPerFrame"]) == ("S03", "S03AELF", 27, 100)
  assert wave["dimensions"][2] > stand["dimensions"][2] + 0.4
  assert (walk["source"]["pose"]["resource"], walk["source"]["pose"]["borrowedFrom"], walk["source"]["pose"]["frame"]) == ("L01BHUF", None, 3)
  # A Luclin erudite borrows the human's animations.
  assert (erudite["source"]["pose"]["resource"], erudite["source"]["pose"]["borrowedFrom"]) == ("O01AHUF", "HUF")
  # A classic kobold borrows the werewolf's, and the client keeps global_chr.s3d's copy, loaded at startup, over the zone's.
  assert (kobold["source"]["pose"]["resource"], kobold["source"]["pose"]["archive"], kobold["source"]["pose"]["variant"]) == ("P01WER", "global_chr.s3d", None)
  assert "has frames 0-26, not 27" in pastTheEnd
  assert "has variants ['A', 'B'], not C" in noVariant
  assert "'MOONWALK' is neither an animation the client loads" in unknown
  # An EQG animation name plays the WLD animation the client maps its id to: NRUN is id 27, L02 (RUN).
  assert (eqgName["source"]["pose"]["animation"], eqgName["source"]["pose"]["resource"]) == ("L02", "L02AELF")


def testEQGSpawnsPlayTheClientsEQGAnimations(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", {"newEngineZone": False})
    stand = await session.expectSuccess("placeSpawn", {"zone": "neighborhood", "model": "DKF", "name": "stand", "height": 5.7, "hairStyle": 2, "location": [0, 0, 0], "headingDegrees": 0})
    wave = await session.expectSuccess("placeSpawn", {"zone": "neighborhood", "model": "DKF", "name": "wave", "height": 5.7, "animation": "S03", "animationFrame": 20, "location": [5, 0, 0], "headingDegrees": 0})
    walk = await session.expectSuccess("placeSpawn", {"zone": None, "model": "GBN", "name": "walk", "height": 6, "animation": "walk", "location": [10, 0, 0], "headingDegrees": 0})
    standDetail = await session.expectSuccess("getObjectDetail", {"name": "stand"})
    lateFrame = await session.expectError("placeSpawn", {"zone": "neighborhood", "model": "DKF", "name": "late", "height": 5.7, "animation": "WAVE", "animationFrame": 44, "location": [15, 0, 0], "headingDegrees": 0})
    lettered = await session.expectError("placeSpawn", {"zone": "neighborhood", "model": "DKF", "name": "lettered", "height": 5.7, "animation": "WAVE", "animationVariant": "B", "location": [15, 0, 0], "headingDegrees": 0})
    armor = await session.expectError("placeSpawn", {"zone": "neighborhood", "model": "DKF", "name": "armored", "height": 5.7, "variation": 1, "location": [15, 0, 0], "headingDegrees": 0})
    return stand, wave, walk, standDetail, lateFrame, lettered, armor

  stand, wave, walk, standDetail, lateFrame, lettered, armor = stageBlenderServer.session(steps)
  # /wave is the client's animation id 75: S03 on a WLD model, WAVE_BA_1_<code> on an EQG one. dkf.eqg's line precedes
  # dkf_anims.eqg's in OnDemandResources.txt, and the client keeps the first.
  wavePose = wave["source"]["pose"]
  assert {key: wavePose[key] for key in ("animation", "wldAnimation", "resource", "archive", "frame", "frameCount", "frameMilliseconds")} == {
    "animation": "WAVE", "wldAnimation": "S03", "resource": "WAVE_BA_1_DKF", "archive": "dkf.eqg", "frame": 20, "frameCount": 44, "frameMilliseconds": 1333,
  }
  assert wave["dimensions"][2] > stand["dimensions"][2] + 0.5
  # The default stand is STND; hairStyle attaches the client's DKF_HAIR_<nn> piece.
  assert (stand["source"]["pose"]["resource"], stand["source"]["pieces"]) == ("STND_BA_1_DKF", ["dkf.mod", "DKF_HAIR_02"])
  # The DLL lowers the root by ROffset, so the feet meet the ground below an origin standing avatarHeight up.
  assert stand["location"][2] == round(stand["avatarHeight"], 3)
  assert 0 <= standDetail["worldMinimum"][2] < 0.1
  assert (walk["source"]["pose"]["animation"], walk["source"]["pose"]["resource"]) == ("WALK", "WALK_BA_1_GBN")
  assert "has frames 0-43, not 44" in lateFrame
  assert "its animations have no lettered variants" in lettered
  assert "only its hairStyle is read yet, got {'variation': 1}" in armor


def testDoorsAndObjectsComeFromTheZonesArchives(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    door = await session.expectSuccess("placeDoor", {"zone": "poknowledge", "model": "POKDOOR500", "name": "door", "x": 0, "y": 0, "z": 0, "heading": 0})
    bigDoor = await session.expectSuccess("placeDoor", {"zone": "poknowledge", "model": "POKDOOR500", "name": "bigDoor", "location": [50, 0, 0], "headingDegrees": 90, "scaleFactor": 150})
    kiln = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "IT10800_ACTORDEF", "name": "kiln", "location": [0, 30, 0], "headingDegrees": 0})
    tree = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "OBJ_TREEM", "name": "tree", "location": [30, 30, 0], "headingDegrees": 0})
    firstLoaded = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "IT67", "name": "it67", "location": [0, 60, 0], "headingDegrees": 0})
    chosen = await session.expectSuccess("placeObject", {"zone": "neighborhood", "model": "IT67", "name": "it67b", "location": [0, 70, 0], "headingDegrees": 0, "source": "gequip.s3d"})
    treeDetail = await session.expectSuccess("getObjectDetail", {"name": "tree"})
    return door, bigDoor, kiln, tree, firstLoaded, chosen, treeDetail

  door, bigDoor, kiln, tree, firstLoaded, chosen, treeDetail = stageBlenderServer.session(steps)
  assert (door["source"]["archive"], door["source"]["linkedBy"], door["source"]["kind"]) == ("poknowledge_obj.s3d", "zone load order", "wldStatic")
  assert door["location"] == [0.0, 0.0, 0.0]
  assert abs(bigDoor["dimensions"][2] - 1.5 * door["dimensions"][2]) < 0.002
  assert (kiln["source"]["archive"], kiln["source"]["linkedBy"]) == ("tradeskill_objects.eqg", "neighborhood_assets.txt")
  # neighborhood.eqg ships only the bark's normal map; no archive the zone loads has its diffuse, so it draws as missing.
  assert tree["source"]["missingTextures"] == ["ab_dg_treebark_c.dds"]
  assert "eq_missing_ab_dg_treebark_c.dds" in [entry["material"] for entry in treeDetail["materials"]]
  assert firstLoaded["source"]["archive"] == "equipment-01.eqg"
  assert chosen["source"]["archive"] == "gequip.s3d"


def testLuclinHeadsTakeTheClientsFaceHairAndBeard(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("setZoneProperties", {"newEngineZone": True})
    christine = await session.expectSuccess("placeSpawn", {
      "zone": "neighborhood", "model": "HUF", "name": "christine", "height": 6, "x": 2015.6, "y": -2849.1, "z": 3.3, "heading": 246,
      "faceStyle": 6, "hairStyle": 1, "hairColor": 4, "snapToGround": False,
    })
    christineDetail = await session.expectSuccess("getObjectDetail", {"name": "christine"})
    bearded = await session.expectSuccess("placeSpawn", {"zone": None, "model": "HUM", "name": "bearded", "height": 6, "location": [10, 0, 0], "headingDegrees": 0, "facialHair": 1, "facialHairColor": 2, "snapToGround": False})
    darkElf = await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAM", "name": "darkElf", "height": 6, "location": [20, 0, 0], "headingDegrees": 0, "facialHair": 0, "snapToGround": False})
    noStyle = await session.expectSuccess("placeSpawn", {"zone": None, "model": "HUF", "name": "noStyle", "height": 6, "location": [30, 0, 0], "headingDegrees": 0, "hairStyle": 9, "snapToGround": False})
    return christine, christineDetail, bearded, darkElf, noStyle

  christine, christineDetail, bearded, darkElf, noStyle = stageBlenderServer.session(steps)
  # The neighborhood's Christine as the live dump records her: face 6 swaps head parts 1, 4, and 5 (HUFHE0061, 64, 65), and hair
  # style 1 is item 1000 + 390 (human female block) + 1, tinted by color 4 (0x650B06).
  assert christine["source"]["pieces"] == ["HUFEYE_R_DMSPRITEDEF", "HUF_DMSPRITEDEF", "HUFEYE_L_DMSPRITEDEF", "IT1391"]
  assert christine["source"]["swappedMaterials"] == 3
  assert any(entry["material"].endswith("it1391har01.dds_650b06") for entry in christineDetail["materials"])
  # A human male takes hair item 1360 and beard item 2000 + 360 + 1; a dark elf male's race takes no beard.
  assert bearded["source"]["pieces"][-2:] == ["IT1360", "IT2361"]
  assert darkElf["source"]["pieces"][-1] == "IT1180"
  assert darkElf["source"]["unattached"] == []
  assert noStyle["source"]["unattached"] == [{"item": "IT1399", "reason": "no archive the client loads defines it"}]
