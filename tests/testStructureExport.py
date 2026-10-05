import collections
import io
import math
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import structurePlots
from testSpans import bridgeArguments, legs, ropes
from testWalls import turnPost, wallArguments

zoneName = "structplot"
plinth = {"material": "testPlotGround", "worldUnitsPerRepeat": 8, "sink": 2}
# Loose walls in the plaza (x 0 to 120, y -40 to 12 at z 0), each placed with its own facing.
loosePlacements = {"plazaWallA": ([30, -14, 0], 0), "plazaWallB": ([60, -14, 0], 225), "plazaWallC": ([95, -14, 0], 90)}
houseParts = ("Exterior", "Interior", "Roof")
slopeWallPath = [[30, -50], [30, -112.5], [80, -112.5]]
# From the plaza (a level section) up the slope (sheared ones) and along it.
shearedWallPath = [[30, -15], [30, -40], [30, -90], [80, -90]]


def archivePathIn(folder):
  return folder / f"{zoneName}.eqg"


async def exportTest(session, folder):
  return await session.expectSuccess("exportZone", {"path": str(archivePathIn(folder)), "purpose": "test"})


async def kitAndPlot(session, folder):
  kitPath = await structurePlots.testKit(session, folder)
  await structurePlots.testPlot(session, folder)
  return str(kitPath)


def readArchive(path):
  """The archive's entries, its .zon, and each model it holds."""
  archive = eqArchive.EQArchive(path)
  zone = eqgFiles.parseZone(archive.read(f"{path.stem}.zon"), path.name)
  models = {name: eqgFiles.parseModel(archive.read(name), name) for name in archive.entries if name.endswith((".mod", ".ter"))}
  return archive, zone, models


def placementsOf(zone, model):
  return [placement for placement in zone["placements"] if placement["model"] == model]


def objectModels(models):
  return sorted(name for name in models if name.endswith(".mod"))


def turnDifference(first, second):
  return abs((first - second + math.pi) % (2 * math.pi) - math.pi)


def passableCount(model):
  return int(((model["triangleFlags"] & eqgFiles.passableFlag) != 0).sum())


def shearModel(piece, rise):
  return f"obj_{piece.lower()}{'up' if rise > 0 else 'down'}{round(abs(rise) * 100)}.mod"


def testALooseKitPieceExportsOneModelSharedByEveryPlacement(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    for name, (location, facing) in loosePlacements.items():
      await session.expectSuccess("placeKitPiece", {"name": name, "kitPath": kitPath, "piece": "testKitWall25", "location": location, "facingDegrees": facing})
    await session.expectSuccess("saveFile", {})
    return await exportTest(session, tmp_path)

  exported = stageBlenderServer.session(steps)
  _, zone, models = readArchive(archivePathIn(tmp_path))
  assert objectModels(models) == ["obj_testkitwall25.mod"] and exported["modelTriangles"] == {"obj_testkitwall25.mod": 10}
  wall = models["obj_testkitwall25.mod"]
  # The model is the piece about its base center, as every placement shares it.
  assert wall["vertices"].min(0).round(4).tolist() == [-12.5, -5.0, 0.0] and wall["vertices"].max(0).round(4).tolist() == [12.5, 5.0, 30.0]
  placed = {tuple(round(value, 4) for value in placement["position"]): placement for placement in placementsOf(zone, "obj_testkitwall25.mod")}
  assert len(placed) == 3
  for location, facing in loosePlacements.values():
    placement = placed[tuple(float(value) for value in location)]
    assert turnDifference(placement["rotation"][0], math.radians(-facing)) < 1e-5, (facing, placement["rotation"])
    assert placement["rotation"][1:] == (0.0, 0.0) and placement["scale"] == 1.0


def testAPrefabExportsOneModelPerPartSharedByItsPlacements(stageBlenderServer, tmp_path):
  pieceNames = {piece for piece, _, _ in (structurePlots.houseWalls | structurePlots.housePosts).values()} | {"testKitFloor", "testKitGableRoof"}

  async def steps(session):
    kitPath = str(await structurePlots.testPrefab(session, tmp_path))
    pieces = {name: (await session.expectSuccess("getObjectDetail", {"name": name}))["kitPiece"]["triangles"] for name in sorted(pieceNames)}
    await structurePlots.testPlot(session, tmp_path)
    plaza = await session.expectSuccess("placePrefab", {"name": "housePlaza", "kitPath": kitPath, "prefab": "testKitHouse", "location": [60, -14], "facingDegrees": 0})
    slope = await session.expectSuccess("placePrefab", {"name": "houseSlope", "kitPath": kitPath, "prefab": "testKitHouse", "location": [55, -85], "facingDegrees": 90, "plinth": plinth})
    await session.expectSuccess("saveFile", {})
    return pieces, plaza, slope, await exportTest(session, tmp_path)

  pieces, plaza, slope, exported = stageBlenderServer.session(steps)
  _, zone, models = readArchive(archivePathIn(tmp_path))
  pieceOf = {name: piece for name, (piece, _, _) in (structurePlots.houseWalls | structurePlots.housePosts).items()}
  expected = {
    "obj_testkithouseexterior.mod": sum(pieces[pieceOf[name]] for name in structurePlots.houseParts["exterior"]),
    "obj_testkithouseinterior.mod": pieces["testKitFloor"], "obj_testkithouseroof.mod": pieces["testKitGableRoof"],
    "obj_houseslopeplinth.mod": slope["plinth"]["triangles"],
  }
  assert objectModels(models) == sorted(expected)
  assert {name: len(models[name]["triangles"]) for name in expected} == expected
  # A part's model holds its nested pieces about the building's origin: the exterior spans the corner posts' outer corners and their height.
  half = structurePlots.houseFootprintHalf
  exterior = models["obj_testkithouseexterior.mod"]["vertices"]
  assert exterior.min(0).round(3).tolist() == [-half[0], -half[1], 0.0] and exterior.max(0).round(3).tolist() == [half[0], half[1], 34.0]
  for part in houseParts:
    placed = placementsOf(zone, f"obj_testkithouse{part.lower()}.mod")
    assert sorted(placement["name"] for placement in placed) == [f"OBJ_testkithouse{part.lower()}01", f"OBJ_testkithouse{part.lower()}02"]
    at = sorted((placement["position"], placement["rotation"][0]) for placement in placed)
    assert numpy.allclose(at[0][0], [55, -85, slope["floor"]], atol=1e-3) and turnDifference(at[0][1], math.radians(-90)) < 1e-5
    assert numpy.allclose(at[1][0], [60, -14, plaza["floor"]], atol=1e-3) and turnDifference(at[1][1], 0.0) < 1e-5
  assert len(placementsOf(zone, "obj_houseslopeplinth.mod")) == 1 and exported["placements"] == 7


def testASpanExportsOneModelWithItsRopeAndRailTrianglesPassable(stageBlenderServer, tmp_path):
  bars = {"piece": "testKitRail", "height": 2.5}

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildBridge", bridgeArguments(kitPath, "ropeBridge", posts=legs, rails=ropes))
    railed = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, "railBridge", start=[-100, 40, 0], end=[-20, 40, 0], posts=legs, rails=bars))
    found = await structurePlots.faces(session, "ropeBridge", "railBridge")
    await session.expectSuccess("saveFile", {})
    return railed, found, await exportTest(session, tmp_path)

  railed, found, exported = stageBlenderServer.session(steps)
  _, _, models = readArchive(archivePathIn(tmp_path))
  assert objectModels(models) == ["obj_railbridge.mod", "obj_ropebridge.mod"]
  for name in ("ropeBridge", "railBridge"):
    faces = [face for face in found if face["object"] == name]
    model = models[f"obj_{name.lower()}.mod"]
    assert len(model["triangles"]) == sum(len(face["points"]) - 2 for face in faces)
    assert passableCount(model) == sum(len(face["points"]) - 2 for face in faces if face["passable"]) > 0, name
  rope = models["obj_ropebridge.mod"]
  materialNames = numpy.array([material["name"] for material in rope["materials"]] + [None], dtype=object)
  assert numpy.array_equal((rope["triangleFlags"] & eqgFiles.passableFlag) != 0, materialNames[rope["triangleMaterials"]] == "testKitRope")
  # Each rail bar is a bar of six quads, flagged passable by its face attribute, not by a cutout material.
  barsLaid = sum(len([post for post in railed["posts"] if post["side"] == side]) - 1 for side in ("left", "right"))
  assert passableCount(models["obj_railbridge.mod"]) == 12 * barsLaid
  assert passableCount(models[f"ter_{zoneName}.ter"]) == 0
  assert exported["passableTriangles"] == {"obj_ropebridge.mod": passableCount(rope), "obj_railbridge.mod": 12 * barsLaid}


def testShearedWallSectionsShareAModelPerRiseAndLevelOnesThePiecesModel(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    built = await session.expectSuccess("buildWall", wallArguments(kitPath, "slopeWall", shearedWallPath, posts=turnPost))
    await session.expectSuccess("saveFile", {})
    return built, await exportTest(session, tmp_path)

  built, exported = stageBlenderServer.session(steps)
  _, zone, models = readArchive(archivePathIn(tmp_path))
  sections = built["sections"]
  wanted = collections.Counter(f"obj_{section['piece'].lower()}.mod" if section["rise"] == 0 else shearModel(section["piece"], section["rise"]) for section in sections)
  wanted["obj_testkitpost.mod"] += len(built["posts"])
  sheared = {(section["piece"], section["rise"]) for section in sections if section["rise"] != 0}
  level = {section["piece"] for section in sections if section["rise"] == 0}
  assert sheared and level
  assert objectModels(models) == sorted(wanted) and len(wanted) == len(level) + len(sheared) + 1
  assert collections.Counter(placement["model"] for placement in zone["placements"] if not placement["model"].endswith(".ter")) == wanted
  assert exported["placements"] == len(sections) + len(built["posts"])
  # A sheared model is its piece with each vertex raised in proportion to its place along the piece, verticals kept vertical: its
  # foot at the piece's +X end stands its rise over the foot at its -X end.
  for piece, rise in sheared:
    vertices = models[shearModel(piece, rise)]["vertices"]
    half = 12.5 if piece == "testKitWall25" else 6.25
    assert sorted({(round(x, 4), round(y, 4)) for x, y, _ in vertices}) == sorted({(sign * half, side * 5.0) for sign in (-1, 1) for side in (-1, 1)})
    footAt = {sign: vertices[numpy.abs(vertices[:, 0] - sign * half) < 1e-4][:, 2].min() for sign in (-1, 1)}
    assert abs(footAt[1] - footAt[-1] - rise) <= 1e-4, (piece, rise, footAt)


def testAWalkwayInTheTerrainCollectionExportsAsTerrain(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildWalkway", {
      "name": "plazaDeck", "kitPath": kitPath, "points": [[20, -10, 0.5], [60, -10, 0.5]], "width": 4, "deck": "testKitPlank", "collection": "terrain",
    })
    triangles = {name: (await session.expectSuccess("getObjectDetail", {"name": name}))["triangles"] for name in ("ground", "plazaDeck")}
    await session.expectSuccess("saveFile", {})
    return triangles, await exportTest(session, tmp_path)

  triangles, exported = stageBlenderServer.session(steps)
  _, zone, models = readArchive(archivePathIn(tmp_path))
  assert objectModels(models) == [] and [placement["model"] for placement in zone["placements"]] == [f"ter_{zoneName}.ter"]
  assert exported["terrainTriangles"] == len(models[f"ter_{zoneName}.ter"]["triangles"]) == triangles["ground"] + triangles["plazaDeck"]
  assert exported["structures"] == [{"structure": "plazaDeck", "kind": "walkway", "models": [], "triangles": triangles["plazaDeck"], "terrain": True}]
  assert exported["placedTriangles"] == 0


def testKitMaterialsExportBesideZoneMaterialsOfTheSameName(stageBlenderServer, tmp_path):
  async def steps(session):
    await structurePlots.sameNamedMaterials(session, tmp_path)
    checked = await session.expectSuccess("checkExport", {"path": str(archivePathIn(tmp_path)), "purpose": "test"})
    return checked, await exportTest(session, tmp_path)

  checked, exported = stageBlenderServer.session(steps)
  archive, _, models = readArchive(archivePathIn(tmp_path))

  def textures(model):
    return {material["name"]: material["properties"]["e_TextureDiffuse0"] for material in models[model]["materials"]}

  assert sorted(exported["materials"]) == ["testKitStone", "testKitStone_testKit", "testKitTrim", "testPlotGround"]
  assert textures("obj_zoneblock.mod") == {"testKitStone": "zonestone.dds"}
  assert textures("obj_testkitwall25.mod") == {"testKitStone_testKit": "testkitstone.dds", "testKitTrim": "testkittrim.dds"}
  for texture, color in (("zonestone.dds", structurePlots.zoneStoneColor), ("testkitstone.dds", (180, 160, 130, 255))):
    assert numpy.asarray(Image.open(io.BytesIO(archive.read(texture))).convert("RGBA"))[0, 0].tolist() == list(color)
  # Each material keeps its own usual repeat: the zone's at 2 a repeat beside the kit's at 12.5 is neither stretched nor squeezed.
  assert checked["failures"] == [] and "texture stretched or squeezed" not in [finding["finding"] for finding in checked["findings"]]
  assert checked["coverage"]["stretch"] == 0


def testStaleStructuresAreConfirmedByATestExportAndFailAGameExport(stageBlenderServer, tmp_path):
  target = {"path": str(archivePathIn(tmp_path))}

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("buildWall", wallArguments(kitPath, "slopeWall", [[30, -50], [30, -112.5]]))
    fresh = await session.expectSuccess("checkExport", target | {"purpose": "test"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [30, -80, structurePlots.slopeHeight(-80)], "radius": 10, "strength": 3, "direction": [0, 0, 1]})
    await session.expectSuccess("saveFile", {})
    test = await session.expectSuccess("checkExport", target | {"purpose": "test"})
    game = await session.expectSuccess("checkExport", target | {"purpose": "game"})
    exported = await exportTest(session, tmp_path)
    return fresh, test, game, exported

  fresh, test, game, exported = stageBlenderServer.session(steps)
  assert fresh["toConfirm"] == []
  assert test["toConfirm"] == exported["toConfirm"] == [{"structure": "slopeWall", "why": ["ground"]}]
  assert "stale structure" not in [failure["failure"] for failure in test["failures"]] and exported["failures"] == []
  stale = [failure for failure in game["failures"] if failure["failure"] == "stale structure"]
  assert [(failure["structure"], failure["why"]) for failure in stale] == [("slopeWall", ["ground"])]
  assert "editStructure lays it again" in stale[0]["message"]
  assert "containment not checked" in [failure["failure"] for failure in game["failures"]]


def testAMissingKitFailsBothExports(stageBlenderServer, tmp_path):
  target = {"path": str(archivePathIn(tmp_path))}

  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    await session.expectSuccess("placeKitPiece", {"name": "plazaWall", "kitPath": kitPath, "piece": "testKitWall25", "location": [30, -14, 0], "facingDegrees": 0})
    await session.expectSuccess("buildBridge", bridgeArguments(kitPath, posts=legs, rails=ropes))
    await session.expectSuccess("saveFile", {})
    zonePath = tmp_path / "testPlot.blend"
    Path(kitPath).rename(tmp_path / "movedKit.blend")
    await session.expectSuccess("openFile", {"path": str(zonePath), "discardUnsavedChanges": True})
    reports = {purpose: await session.expectSuccess("checkExport", target | {"purpose": purpose}) for purpose in ("test", "game")}
    refused = await session.expectError("exportZone", target | {"purpose": "test"})
    return kitPath, reports, refused

  kitPath, reports, refused = stageBlenderServer.session(steps)
  for purpose, report in reports.items():
    missing = [failure for failure in report["failures"] if failure["failure"] == "kit missing"]
    placed = [failure for failure in missing if "kit" in failure]
    laid = [failure for failure in missing if "structure" in failure]
    assert len(placed) == 1 and Path(placed[0]["kit"]) == Path(kitPath), purpose
    assert placed[0]["collections"] == ["testKitWall25"] and placed[0]["placedBy"] == ["plazaWall"] and placed[0]["placements"] == 1
    assert "is gone" in placed[0]["message"]
    assert [failure["structure"] for failure in laid] == ["gorgeBridge"] and laid[0]["missing"], purpose
  assert not archivePathIn(tmp_path).exists() and "kit missing" in refused


def testCheckExportReportsStructureTrianglesAndThePlacedTotal(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = str(await structurePlots.testPrefab(session, tmp_path))
    await structurePlots.testPlot(session, tmp_path)
    await session.expectSuccess("placeKitPiece", {"name": "stripWall", "kitPath": kitPath, "piece": "testKitWall25", "location": [-10, 60, 0], "facingDegrees": 90})
    bridge = await session.expectSuccess("buildBridge", bridgeArguments(kitPath, profile={"sag": 8}, posts=legs, rails=ropes))
    wall = await session.expectSuccess("buildWall", wallArguments(kitPath, "slopeWall", slopeWallPath, posts=turnPost))
    house = await session.expectSuccess("placePrefab", {"name": "housePlaza", "kitPath": kitPath, "prefab": "testKitHouse", "location": [60, -14], "facingDegrees": 0})
    await session.expectSuccess("saveFile", {})
    checked = await session.expectSuccess("checkExport", {"path": str(archivePathIn(tmp_path)), "purpose": "test"})
    return bridge, wall, house, checked, await exportTest(session, tmp_path)

  bridge, wall, house, checked, exported = stageBlenderServer.session(steps)
  _, zone, models = readArchive(archivePathIn(tmp_path))
  structures = {entry["structure"]: entry for entry in checked["structures"]}
  assert list(structures) == ["gorgeBridge", "slopeWall", "housePlaza"]
  bridgeTriangles = bridge["parts"][0]["triangles"]
  assert structures["gorgeBridge"] == {
    "structure": "gorgeBridge", "kind": "bridge", "models": [{"file": "obj_gorgebridge.mod", "triangles": bridgeTriangles, "placements": 1}], "triangles": bridgeTriangles, "terrain": False,
  }
  wallModels = sorted(({"file": file} | model for file, model in wall["models"].items()), key=lambda model: model["file"])
  assert structures["slopeWall"]["models"] == wallModels
  assert structures["slopeWall"]["triangles"] == sum(model["triangles"] * model["placements"] for model in wallModels)
  houseModels = sorted(({"file": part["model"], "triangles": part["triangles"], "placements": 1} for part in house["parts"]), key=lambda model: model["file"])
  assert structures["housePlaza"]["kind"] == "prefab" and structures["housePlaza"]["models"] == houseModels
  # Every placement's model triangles, read back from the archive, add up to the placed total; the loose wall is 10 of them.
  fromArchive = sum(len(models[placement["model"]]["triangles"]) for placement in zone["placements"] if not placement["model"].endswith(".ter"))
  assert checked["placedTriangles"] == exported["placedTriangles"] == fromArchive
  assert fromArchive == 10 + sum(entry["triangles"] for entry in structures.values())


def testStructuresComeBackFromTheArchiveAsBuilt(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = await kitAndPlot(session, tmp_path)
    for name, (location, facing) in loosePlacements.items():
      await session.expectSuccess("placeKitPiece", {"name": name, "kitPath": kitPath, "piece": "testKitWall25", "location": location, "facingDegrees": facing})
    await session.expectSuccess("buildBridge", bridgeArguments(kitPath, posts=legs, rails=ropes))
    await session.expectSuccess("buildWall", wallArguments(kitPath, "slopeWall", slopeWallPath, posts=turnPost))
    await session.expectSuccess("saveFile", {})
    exported = await exportTest(session, tmp_path)
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZoneFile", {"path": str(archivePathIn(tmp_path))})
    across = await session.expectSuccess("walkRoute", {"path": [[-110, 0, 0], [-10, 0, 0]]})
    offTheSide = await session.expectSuccess("walkRoute", {"path": [[-70, 0, 0], [-70, 14, 0]]})
    return exported, imported, across, offTheSide

  exported, imported, across, offTheSide = stageBlenderServer.session(steps)
  source = imported["source"]
  assert source["placements"] == source["placedObjects"] == exported["placements"] + 1
  assert source["missingModels"] == [] and source["droppedTriangles"] == 0
  assert imported["passableTriangles"] == exported["passableTriangles"] and exported["passableTriangles"]["obj_gorgebridge.mod"] > 0
  assert across["walkable"], across["problems"]
  # The ropes let a player through, as they did before export: off the deck's side the walk drops to the gorge floor.
  assert offTheSide["problems"] == [] and [(step["kind"], step["height"]) for step in offTheSide["oneWay"]] == [("ledge", 40.0)]
