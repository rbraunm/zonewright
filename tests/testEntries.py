import io
import math
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import planDrawing
from playerScale import eyeHeight, playerHeight, walkableNormalZ
from conftest import writePNG
from testBoundaries import boundedPlot
from testHousing import decision, flatGround
from testModelsAndDressing import freshScene
from testWater import environment, liquidMaterials

# A ramp this much steeper than players walk (playerScale).
steepDegrees = math.degrees(math.acos(walkableNormalZ)) + 5
otherZone = {"zone": "qeynos2", "x": 0, "y": 0, "z": 0, "headingDegrees": 0}


async def groundGrid(session, folder, name="ground", size=200, location=(0, 0, 0)):
  await session.expectSuccess("createTerrainGrid", {"name": name, "size": [size, size], "spacing": 8, "location": list(location), "collection": "terrain"})
  await session.expectSuccess("createMaterial", {"name": f"{name}Grass", "diffuseTexture": str(writePNG(folder / f"{name}Grass.png", 4, 4, (90, 120, 60, 255)))})
  await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": f"{name}Grass"})
  await session.expectSuccess("projectUVs", {"objectName": name, "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})


async def entryPlot(session, folder):
  """Flat ground at 0, 200 across, with a dais 10 high at (60, 60), a ramp steeper than players walk at (-60, 0), a slab whose
  underside leaves less than a player's height over the ground at (-60, 60), a pond of undecided swimming at (-60, -60), a terrace
  floating over the ground at (0, 70), its rim 20 up and a pool of undecided swimming 16 up in its basin, whose floor is 12 up, a swim
  volume on the ground at (30, -70), one floating 20 over it at (70, -70) and one 3 over it at (-20, -30), a zone line on the ground at
  (-95, 0), and one 2 over it at (30, 30)."""
  await freshScene(session)
  await groundGrid(session, folder)
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "dais", "size": [20, 20, 10], "location": [60, 60, 0]})
  await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "ramp", "size": [20, 20, 0], "location": [-60, 0, 12], "rotationDegrees": [steepDegrees, 0, 0]})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "slab", "size": [20, 20, 1], "location": [-60, 60, playerHeight - 1]})
  await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-60, -60, -10]], "radius": 30, "strength": 1, "profile": [[0, 0], [1, 10]], "conformRim": False})
  await groundGrid(session, folder, name="terrace", size=40, location=(0, 70, 20))
  await session.expectSuccess("sculptAlongPath", {"objectName": "terrace", "mode": "carve", "path": [[0, 70, 12]], "radius": 16, "strength": 1, "profile": [[0, 0], [0.5, 0], [1, 8]], "conformRim": False})
  await liquidMaterials(session, folder)
  await session.expectSuccess("floodWater", {"name": "pond", "seed": [-60, -60], "level": -2, "material": "water"})
  await session.expectSuccess("floodWater", {"name": "terracePool", "seed": [0, 70], "level": 16, "material": "water", "within": [[-12, 58], [12, 58], [12, 82], [-12, 82]]})
  await session.expectSuccess("placeSwimVolume", {"name": "wading", "liquid": "water", "minimum": [20, -80, -5], "maximum": [40, -60, 10]})
  await session.expectSuccess("placeSwimVolume", {"name": "skyPool", "liquid": "water", "minimum": [60, -80, 20], "maximum": [80, -60, 30]})
  await session.expectSuccess("placeSwimVolume", {"name": "lowPool", "liquid": "water", "minimum": [-25, -35, 3], "maximum": [-15, -25, 10]})
  await session.expectSuccess("placeZoneLine", {"number": 1, "label": "west", "minimum": [-100, -10, -5], "maximum": [-90, 10, 40], "target": otherZone})
  await session.expectSuccess("placeZoneLine", {"number": 5, "label": "arch", "minimum": [25, 25, 2], "maximum": [35, 35, 12], "target": otherZone})
  await session.expectSuccess("setZoneProperties", environment)


def entryArguments(name, at, kind="landing", headingDegrees=90, **more):
  return {"name": name, "at": at, "headingDegrees": headingDegrees, "kind": kind} | more


def testPlaceEntryStandsOnShippedFooting(stageBlenderServer, tmp_path):
  async def steps(session):
    await entryPlot(session, tmp_path)
    view, placed = await session.expectImage("placeEntry", entryArguments("fromQeynos", [0, 0], "zoneIn", fromZone="qeynos2", fromNumber=3))
    _, dais = await session.expectImage("placeEntry", entryArguments("daisTop", [60, 60]))
    _, underPool = await session.expectImage("placeEntry", entryArguments("underSkyPool", [70, -70]))
    _, underTerrace = await session.expectImage("placeEntry", entryArguments("underTerrace", [0, 70, 0]))
    _, again = await session.expectImage("placeEntry", entryArguments("fromQeynos", [10, 0], "zoneIn", fromZone="qeynos2"))
    refusals = {
      "noFooting": await session.expectError("placeEntry", entryArguments("lost", [500, 500])),
      "steep": await session.expectError("placeEntry", entryArguments("onTheRamp", [-60, 0])),
      "headroom": await session.expectError("placeEntry", entryArguments("underTheSlab", [-60, 60, 0])),
      "undecided": await session.expectError("placeEntry", entryArguments("inThePond", [-60, -60])),
      "terracePool": await session.expectError("placeEntry", entryArguments("inTheTerracePool", [0, 70])),
      "swimBox": await session.expectError("placeEntry", entryArguments("inTheShallows", [30, -70])),
      "zoneLine": await session.expectError("placeEntry", entryArguments("inTheGate", [-95, 0])),
      "swimBoxOverhead": await session.expectError("placeEntry", entryArguments("underTheLowPool", [-20, -30])),
      "zoneLineOverhead": await session.expectError("placeEntry", entryArguments("underTheArch", [30, 30])),
      "clash": await session.expectError("placeEntry", entryArguments("dais", [0, 30])),
      "kind": await session.expectError("placeEntry", entryArguments("portal", [0, 30], "portal")),
      "noFromZone": await session.expectError("placeEntry", entryArguments("nowhere", [0, 30], "zoneIn")),
      "badFromZone": await session.expectError("placeEntry", entryArguments("spaced", [0, 30], "zoneIn", fromZone="Qeynos 2")),
      "badFromNumber": await session.expectError("placeEntry", entryArguments("zeroed", [0, 30], "zoneIn", fromZone="qeynos2", fromNumber=0)),
      "landingFrom": await session.expectError("placeEntry", entryArguments("landed", [0, 30], fromZone="qeynos2")),
    }
    listed = await session.expectSuccess("getEntries", {})
    return view, placed, dais, underPool, underTerrace, again, refusals, listed

  view, placed, dais, underPool, underTerrace, again, refusals, listed = stageBlenderServer.session(steps)
  assert {key: placed[key] for key in ("name", "kind", "at", "headingDegrees", "fromZone", "fromNumber", "isolated", "state", "replaced")} == {
    "name": "fromQeynos", "kind": "zoneIn", "at": [0.0, 0.0, 0.0], "headingDegrees": 90.0, "fromZone": "qeynos2", "fromNumber": 3, "isolated": False,
    "state": "onFooting", "replaced": False,
  }
  # Its arrival view stands on the footing at a player's eye, facing its heading, with the scale figure ahead.
  assert placed["arrivalView"]["view"] == {"standOn": [0.0, 0.0, 0.0], "headingDegrees": 90.0, "pitchDegrees": 0.0}
  assert placed["arrivalView"]["eye"] == [0.0, 0.0, eyeHeight] and placed["arrivalView"]["figure"][0] > 0
  assert Image.open(io.BytesIO(view)).size == (960, 540) and Path(placed["arrivalView"]["outputPath"]).is_file()
  # [x, y] takes the highest footing: the dais's top.
  assert dais["at"] == [60.0, 60.0, 10.0] and dais["state"] == "onFooting"
  # Under a floating swim volume, with air between, players arrive on the ground: the box, not the water over it, decides swimming.
  assert underPool["at"] == [70.0, -70.0, 0.0]
  # Under the floating terrace's pool, its basin between, players arrive on the ground; in the basin they would arrive in its water.
  assert underTerrace["at"] == [0.0, 70.0, 0.0] and underTerrace["state"] == "onFooting"
  assert "The footing at [0.0, 70.0, 12.0] lies under the surface of 'terracePool', whose swimming is undecided" in refusals["terracePool"]
  assert again["replaced"] is True and again["at"] == [10.0, 0.0, 0.0] and again["fromNumber"] is None
  assert [entry["name"] for entry in listed["entries"]] == ["daisTop", "fromQeynos", "underSkyPool", "underTerrace"]
  assert "No ground the zone ships lies at [500.0, 500.0]" in refusals["noFooting"]
  assert f"slopes {steepDegrees:.1f} degrees, steeper than players walk ({math.degrees(math.acos(walkableNormalZ)):.1f}" in refusals["steep"]
  assert f"has {playerHeight - 1:.2f} of headroom, under a player's height ({playerHeight:g}" in refusals["headroom"]
  assert "under the surface of 'pond', whose swimming is undecided" in refusals["undecided"]
  # A box reaching into the player's height over the footing refuses it, whether it holds the footing or starts over it: the client
  # tests a player's origin, which stands somewhere in that height.
  standing = f"({playerHeight:g} tall, playerScale) reaches into"
  assert f"A player standing on the footing at [30.0, -70.0, 0.0] {standing} swim volume 'AWT_wading': players would arrive in its water" in refusals["swimBox"]
  assert f"A player standing on the footing at [-20.0, -30.0, 0.0] {standing} swim volume 'AWT_lowPool'" in refusals["swimBoxOverhead"]
  assert f"A player standing on the footing at [-95.0, 0.0, 0.0] {standing} zone line 'ATP_1_west'" in refusals["zoneLine"]
  assert f"A player standing on the footing at [30.0, 30.0, 0.0] {standing} zone line 'ATP_5_arch'" in refusals["zoneLineOverhead"]
  assert "'dais' is the name of 'dais', which is not an entry" in refusals["clash"]
  assert "An entry's kind is one of ['zoneIn', 'landing'], got 'portal'" in refusals["kind"]
  assert "A zoneIn needs fromZone" in refusals["noFromZone"]
  assert "fromZone is a zone's short name" in refusals["badFromZone"] and "'Qeynos 2'" in refusals["badFromZone"]
  assert "fromNumber is the neighbour's zone_points number, a whole number of at least 1, got 0" in refusals["badFromNumber"]
  assert "fromZone and fromNumber are a zoneIn's" in refusals["landingFrom"]


def testEntryFootingIgnoresReferenceContent(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await groundGrid(session, tmp_path, size=96)
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "kiln", "location": [20, 20, 0], "headingDegrees": 0})
    await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "farKiln", "location": [300, 300, 0], "headingDegrees": 0})
    measured = await session.expectSuccess("measure", {"points": [[20, 20, 50], [300, 300, 50]], "snapToSurface": True})
    _, besideKiln = await session.expectImage("placeEntry", entryArguments("byTheKiln", [20, 20]))
    overFarKiln = await session.expectError("placeEntry", entryArguments("onTheKiln", [300, 300]))
    return measured, besideKiln, overFarKiln

  measured, besideKiln, overFarKiln = stageBlenderServer.session(steps)
  # The scene's own lookups stand on the client's kiln; an entry stands on what the zone ships, the ground under it, and over the far
  # kiln there is none.
  assert measured["points"][0][2] > 1 and measured["points"][1][2] > 1
  assert besideKiln["at"] == [20.0, 20.0, 0.0]
  # Its arrival view stands on that footing, not on the kiln over it.
  assert besideKiln["arrivalView"]["eye"] == [20.0, 20.0, eyeHeight]
  assert "No ground the zone ships lies at [300.0, 300.0]" in overFarKiln


def testGetEntriesListsDerivedEntriesAndOffFooting(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [-200, -200, 0, 45], "underworld": -100})
    await session.expectSuccess("setZoneHousing", decision)
    await session.expectSuccess("placePlot", {"address": "101 Test Street", "center": [0, 0], "facingDegrees": 0, "size": [60, 80]})
    await session.expectSuccess("placeZoneLine", {"number": 2, "label": "loop", "minimum": [300, -20, -5], "maximum": [320, 20, 40], "target": {"zone": "entryplot", "x": 200, "y": 200, "z": 0, "headingDegrees": 270}})
    await session.expectSuccess("placeZoneLine", {"number": 3, "label": "kept", "minimum": [-320, -20, -5], "maximum": [-300, 20, 40], "target": {"zone": "entryplot", "x": "keep", "y": 0, "z": 0, "headingDegrees": 0}})
    await session.expectSuccess("placeZoneLine", {"number": 4, "label": "away", "minimum": [-20, 300, -5], "maximum": [20, 320, 40], "target": otherZone})
    unnamed = await session.expectSuccess("getEntries", {})
    await session.expectSuccess("setZoneProperties", {"shortName": "entryplot"})
    await session.expectImage("placeEntry", entryArguments("dock", [100, -100], headingDegrees=0, isolated=True))
    await session.expectSuccess("transformObjects", {"names": ["dock"], "translate": [0, 0, 3]})
    listed = await session.expectSuccess("getEntries", {})
    badNames = [await session.expectError("setZoneProperties", {"shortName": name}) for name in ("entry_plot", "a" * 32)]
    longTarget = await session.expectError("placeZoneLine", {"number": 6, "label": "far", "minimum": [0, 300, -5], "maximum": [20, 320, 40], "target": otherZone | {"zone": "a" * 32}})
    return unnamed, listed, badNames, longTarget

  unnamed, listed, badNames, longTarget = stageBlenderServer.session(steps)
  # Without the zone's short name no zone line can be told to lead back into it.
  assert [entry["kind"] for entry in unnamed["entries"]] == ["safePoint", "plotEntrance"]
  assert [line["zoneLine"] for line in unnamed["notFollowed"]] == ["ATP_2_loop", "ATP_3_kept", "ATP_4_away"]
  assert all("shortName is not set" in line["why"] for line in unnamed["notFollowed"])
  assert listed["entries"] == [
    {
      "name": "dock", "kind": "landing", "at": [100.0, -100.0, 3.0], "headingDegrees": 0.0, "source": "placeEntry", "fromZone": None, "fromNumber": None,
      "isolated": True, "state": "offFooting", "footing": [100.0, -100.0, 0.0], "distance": 3.0,
    },
    {"name": "safe point", "kind": "safePoint", "at": [-200.0, -200.0, 0.0], "headingDegrees": 45, "source": "setZoneProperties safePoint", "state": "onFooting"},
    {"name": "T2", "kind": "teleport", "at": [200.0, 200.0, 0.0], "headingDegrees": 270, "source": "zone line ATP_2_loop", "state": "onFooting"},
    # The plot faces +Y: its entrance is 10 out from the middle of its entrance side, 80 along, facing back into it.
    {"name": "101 Test Street", "kind": "plotEntrance", "at": [0.0, 50.0, 0.0], "headingDegrees": 180.0, "source": "plot 101 Test Street", "state": "onFooting"},
  ]
  assert listed["notFollowed"] == [{"zoneLine": "ATP_3_kept", "why": "its target keeps the player's own x, so where it lands is not one point"}]
  # One rule names a zone, for the zone's own short name, a zone line's target, and an export's archive (testZoneExport).
  assert all("shortName is 1 to 31 lowercase letters and digits" in badName for badName in badNames)
  assert "target zone is a zone short name, 1 to 31 lowercase letters and digits" in longTarget


def triangleCorners(archivePath, zoneName):
  """The archive's triangles placed as the client places them (eqgFiles.placeVertices): solid ones and passable ones."""
  archive = eqArchive.EQArchive(archivePath)
  zone = eqgFiles.parseZone(archive.read(f"{zoneName}.zon"), f"{zoneName}.zon")
  solid, passable = [], []
  for placement in zone["placements"]:
    model = eqgFiles.parseModel(archive.read(placement["model"]), placement["model"])
    corners = eqgFiles.placeVertices(model["vertices"], placement)[model["triangles"]]
    flagged = (model["triangleFlags"] & eqgFiles.passableFlag) != 0
    solid.append(corners[~flagged])
    passable.append(corners[flagged])
  return numpy.concatenate(solid), numpy.concatenate(passable)


def unmatchedTriangle(first, second, tolerance):
  """The index of the first triangle of first with no unused triangle of second at the same corners (within tolerance, the same way
  round), or None when each of first has its own in second."""
  rotations = [second, second[:, [1, 2, 0]], second[:, [2, 0, 1]]]
  centroids = second.mean(axis=1)
  used = numpy.zeros(len(second), dtype=bool)
  for start in range(0, len(first), 256):
    chunk = first[start:start + 256]
    distances = numpy.linalg.norm(chunk.mean(axis=1)[:, None, :] - centroids[None], axis=2)
    for offset, row in enumerate(distances):
      triangle = chunk[offset]
      match = next((candidate for candidate in numpy.flatnonzero(row <= 2 * tolerance) if not used[candidate] and any(numpy.abs(rotated[candidate] - triangle).max() <= tolerance for rotated in rotations)), None)
      if match is None:
        return start + offset
      used[match] = True
  return None


nestedInstance = """
inner = bpy.data.collections.new('innerKit')
inner.objects.link(bpy.data.objects.new('innerCrate', bpy.data.objects['crate'].data))
outer = bpy.data.collections.new('outerKit')
outer.objects.link(bpy.data.objects.new('outerCrate', bpy.data.objects['crate'].data))
nested = bpy.data.objects.new('nestedInner', None)
nested.instance_type = 'COLLECTION'
nested.instance_collection = inner
nested.location = (3, 2, 8)
nested.rotation_euler = (0, 0, 0.5)
outer.objects.link(nested)
placed = bpy.data.objects.new('outerPlaced', None)
placed.instance_type = 'COLLECTION'
placed.instance_collection = outer
placed.location = (-60, -100, 0)
placed.rotation_euler = (0, 0, 0.7)
placed.scale = (1.5, 1.5, 1.5)
bpy.context.scene.collection.objects.link(placed)
passed = bpy.data.objects.new('passablePlaced', None)
passed.instance_type = 'COLLECTION'
passed.instance_collection = outer
passed.location = (60, -100, 0)
bpy.context.scene.collection.objects.link(passed)
"""
collectCode = """
import bridgeExport
collected = bridgeExport.collisionTriangles()
result = {'corners': collected['positions'][collected['triangles']].tolist(), 'owners': sorted({collected['ownerNames'][owner] for owner in collected['owners'].tolist()})}
"""


def testCollisionTrianglesEqualTheArchivesSolidTriangles(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "collisionplot.eqg"

  async def steps(session):
    await boundedPlot(session, tmp_path)
    await session.expectSuccess("runPython", {"code": nestedInstance})
    await session.expectSuccess("markPassable", {"objects": ["passablePlaced"]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "collisionplot.blend")})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    collected = (await session.expectSuccess("runPython", {"code": collectCode}))["result"]
    return exported, collected

  exported, collected = stageBlenderServer.session(steps)
  assert exported["failures"] == []
  archiveSolid, archivePassable = triangleCorners(archivePath, "collisionplot")
  corners = numpy.array(collected["corners"])
  # The pool, the fall, the cutout card, the crate marked passable, and the instance marked passable (its two crates, one nested) are
  # passed through whole, so none of them owns a triangle; the archive flags theirs, and its other triangles, placed as the client
  # places them, are the collector's, one for one.
  assert collected["owners"] == ["crate", "eastNorth", "eastSouth", "ground", "outerPlaced", "westLid"]
  passableInstance = numpy.all(numpy.abs(archivePassable.mean(axis=1)[:, :2] - [60, -100]) < 20, axis=1)
  assert passableInstance.sum() == 2 * 12 and len(archivePassable) > passableInstance.sum()
  assert len(corners) == len(archiveSolid)
  assert unmatchedTriangle(corners, archiveSolid, 1e-4) is None
  assert unmatchedTriangle(archiveSolid, corners, 1e-4) is None


def testRegionsRequireAccessAndListUndecided(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "accessplot.eqg"

  async def steps(session):
    await freshScene(session)
    await groundGrid(session, tmp_path, size=96)
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "accessplot.blend")})
    bare = {purpose: await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": purpose}) for purpose in ("game", "test")}
    outline = [[-40, -40], [40, -40], [40, 40], [-40, 40]]
    missing = await session.expectError("createRegion", {"name": "yard", "outline": outline, "bottom": -10, "top": 30, "intent": "the yard"})
    wrong = await session.expectError("createRegion", {"name": "yard", "outline": outline, "bottom": -10, "top": 30, "intent": "the yard", "access": "walk"})
    created = await session.expectSuccess("createRegion", {"name": "yard", "outline": outline, "bottom": -10, "top": 30, "intent": "the yard", "access": "play"})
    await session.expectSuccess("createRegion", {"name": "rim", "outline": [[40, -40], [50, -40], [50, 40], [40, 40]], "bottom": -10, "top": 30, "intent": "the rim", "access": "view"})
    # A region made before access was decided holds none.
    await session.expectSuccess("runPython", {"code": "del bpy.data.objects['rim']['zonewrightRegionAccess']"})
    listed = await session.expectSuccess("getRegions", {})
    await session.expectImage("placeEntry", entryArguments("gate", [10, 10]))
    await session.expectSuccess("transformObjects", {"names": ["gate"], "translate": [0, 0, 2]})
    await session.expectSuccess("saveFile", {})
    undecided = {purpose: await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": purpose}) for purpose in ("game", "test")}
    edited = await session.expectSuccess("editRegion", {"name": "rim", "access": "none"})
    await session.expectImage("placeEntry", entryArguments("gate", [10, 10]))
    await session.expectSuccess("saveFile", {})
    decided = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})
    return bare, missing, wrong, created, listed, undecided, edited, decided

  bare, missing, wrong, created, listed, undecided, edited, decided = stageBlenderServer.session(steps)

  def spaceGaps(listing, key):
    return [{name: value for name, value in entry.items() if name != "message"} for entry in listing if entry.get(key) in ("no regions", "region access undecided", "entry off its footing")]

  assert spaceGaps(bare["game"]["failures"], "failure") == [{"failure": "no regions"}]
  assert spaceGaps(bare["test"]["findings"], "finding") == [{"finding": "no regions"}] and spaceGaps(bare["test"]["failures"], "failure") == []
  assert "access" in missing
  assert "A region's access is one of ['play', 'view', 'none']" in wrong and "got 'walk'" in wrong
  assert created["access"] == "play"
  assert listed["undecided"] == ["rim"] and [(region["name"], region["access"]) for region in listed["regions"]] == [("yard", "play"), ("rim", None)]
  offFooting = {"entry": "gate", "at": [10.0, 10.0, 2.0], "footing": [10.0, 10.0, 0.0], "distance": 2.0}
  assert spaceGaps(undecided["game"]["failures"], "failure") == [{"failure": "region access undecided", "region": "rim"}, {"failure": "entry off its footing"} | offFooting]
  assert spaceGaps(undecided["test"]["findings"], "finding") == [{"finding": "region access undecided", "region": "rim"}, {"finding": "entry off its footing"} | offFooting]
  assert spaceGaps(undecided["test"]["failures"], "failure") == []
  assert edited["access"] == "none"
  assert spaceGaps(decided["failures"], "failure") == []


def testSafePointGroundIgnoresReferenceContent(stageBlenderServer, tmp_path):
  referencePath = tmp_path / "neighbour.eqg"
  check = {"path": str(tmp_path / "ownplot.eqg"), "purpose": "test"}

  async def steps(session):
    await freshScene(session)
    await groundGrid(session, tmp_path, size=64)
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "neighbour.blend")})
    await session.expectSuccess("exportZone", {"path": str(referencePath), "purpose": "test"})
    await freshScene(session)
    await session.expectSuccess("importZoneFile", {"path": str(referencePath)})
    await groundGrid(session, tmp_path, name="own", size=64, location=(500, 0, 0))
    await session.expectSuccess("setZoneProperties", environment | {"safePoint": [0, 0, 0, 0], "underworld": -50})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "ownplot.blend")})
    overReference = await session.expectSuccess("checkExport", check)
    measured = await session.expectSuccess("measure", {"points": [[0, 0, 10]], "snapToSurface": True})
    await session.expectSuccess("setZoneProperties", {"safePoint": [500, 0, 0, 0]})
    await session.expectSuccess("saveFile", {})
    overOwn = await session.expectSuccess("checkExport", check)
    return overReference, measured, overOwn

  overReference, measured, overOwn = stageBlenderServer.session(steps)
  # The imported zone is ground to stand on in the scene, but the export ships none of it.
  assert measured["points"] == [[0.0, 0.0, 0.0]]
  assert [finding["at"] for finding in overReference["findings"] if finding.get("finding") == "safe point over no ground"] == [[0, 0, 0]]
  assert [finding for finding in overOwn["findings"] if finding.get("finding") == "safe point over no ground"] == []


sketchRegions = {
  "backdrop": ("view", [[30, -220], [120, -220], [120, -120], [30, -120]]),
  "rimRock": ("none", [[30, 120], [120, 120], [120, 220], [30, 220]]),
  "square": ("play", [[-120, 120], [-30, 120], [-30, 220], [-120, 220]]),
  "old": ("undecided", [[-120, -220], [-30, -220], [-30, -120], [-120, -120]]),
}
# Inside each region, clear of its label at its middle, of its outline, and of the grid's lines.
sketchSamples = {"backdrop": [110, -135], "rimRock": [110, 135], "square": [-110, 135], "old": [-110, -135]}
# Each entry's point, heading, and whether it is isolated.
sketchEntries = {
  "safe point": ([0, 0], 90, False), "zoneIn": ([-75, 60], 0, False), "landing": ([75, -60], 90, True), "T2": ([-75, -60], 0, False),
  "entrance": ([0, 140], 0, False),
}
sketchView = {"center": [0, 0], "width": 480, "spotHeights": False}


async def sketchPlot(session, folder):
  """Flat ground with a region of each access, the safe point, a zoneIn, an isolated landing, a teleport's landing, and a plot whose
  entrance faces into it along +Y: the plan the sketch test and its picture draw."""
  await freshScene(session)
  await flatGround(session, folder)
  await session.expectSuccess("setZoneProperties", environment | {"safePoint": [0, 0, 0, 90], "underworld": -100, "shortName": "sketchplot"})
  for name, (access, outline) in sketchRegions.items():
    await session.expectSuccess("createRegion", {"name": name, "outline": outline, "bottom": -20, "top": 60, "intent": f"a {access} area", "access": "play" if access == "undecided" else access})
  await session.expectSuccess("runPython", {"code": "del bpy.data.objects['old']['zonewrightRegionAccess']"})
  await session.expectImage("placeEntry", {"name": "fromQeynos", "at": [-75, 60], "headingDegrees": 0, "kind": "zoneIn", "fromZone": "qeynos2"})
  await session.expectImage("placeEntry", {"name": "dock", "at": [75, -60], "headingDegrees": 90, "kind": "landing", "isolated": True})
  await session.expectSuccess("placeZoneLine", {"number": 2, "label": "loop", "minimum": [100, -10, -5], "maximum": [110, 10, 40], "target": {"zone": "sketchplot", "x": -75, "y": -60, "z": 0, "headingDegrees": 0}})
  await session.expectSuccess("setZoneHousing", decision)
  await session.expectSuccess("placePlot", {"address": "1 Sketch Lane", "center": [0, 170], "facingDegrees": 180, "size": [40, 40]})


def testRenderSketchDrawsEntriesAndAccess(stageBlenderServer, tmp_path):
  async def steps(session):
    await sketchPlot(session, tmp_path)
    plan, drawn = await session.expectImage("renderSketch", sketchView)
    bare, _ = await session.expectImage("renderSketch", sketchView | {"layers": ["plots", "water", "boundaries", "zoneLines", "entries"]})
    return plan, drawn, bare

  plan, drawn, bare = stageBlenderServer.session(steps)
  planPixels = numpy.asarray(Image.open(io.BytesIO(plan)).convert("RGB"), dtype=numpy.int64)
  barePixels = numpy.asarray(Image.open(io.BytesIO(bare)).convert("RGB"), dtype=numpy.int64)
  frame = planDrawing.PlanFrame(sketchView["center"], sketchView["width"], (planPixels.shape[1], planPixels.shape[0]))
  # Each region's fill shifts the relief toward its access's color: amber for view, red for none, grey for undecided, nothing for play.
  shifts = {}
  for name, point in sketchSamples.items():
    x, y = (round(value) for value in frame.pixel(point))
    shifts[name] = (planPixels[y - 10:y + 10, x - 10:x + 10] - barePixels[y - 10:y + 10, x - 10:x + 10]).reshape(-1, 3).mean(axis=0)
  red, green, blue = shifts["backdrop"]
  assert red - blue > 40 and green - blue > 15, shifts["backdrop"]
  red, green, blue = shifts["rimRock"]
  assert red - green > 40 and abs(green - blue) < 6, shifts["rimRock"]
  red, green, blue = shifts["old"]
  assert min(abs(red), abs(green), abs(blue)) > 3 and max(abs(red - green), abs(green - blue)) < 3, shifts["old"]
  assert numpy.abs(shifts["square"]).max() < 1, shifts["square"]
  def planPixel(point, headingDegrees=0, pixels=0):
    """The drawing's pixel `pixels` along a heading from a world point, stepping in the world (0 = +Y, clockwise)."""
    heading, units = math.radians(headingDegrees), pixels / frame.length(1)
    x, y = frame.pixel([point[0] + math.sin(heading) * units, point[1] + math.cos(heading) * units])
    return planPixels[round(y), round(x)]

  # Each entry is a triangle about its point, its tip 18 pixels ahead along its heading and its back 9 behind: dark, or hollow (white
  # inside, inside a dark edge that fills its narrow end) when isolated. 11 pixels ahead lies inside it, 11 behind outside it on the
  # grey relief.
  for name, (point, heading, isolated) in sketchEntries.items():
    inside, ahead, behind = planPixel(point), planPixel(point, heading, 11), planPixel(point, heading, -11)
    assert (inside.min() >= 200) if isolated else (inside.max() <= 60), (name, inside)
    assert ahead.max() <= 60 and behind.min() >= 80, (name, ahead, behind)
  # The plot faces -Y, the plan's right: its own entrance mark points out of the middle of that side, 11 pixels long, rightward, where
  # its entrance's arrow stands 10 beyond. 8 pixels out it is the plot's brown, beyond a mark lying along the side, 14 across.
  red, green, blue = planPixel([0, 150], 180, 8)
  assert red - blue > 80 and red > green > blue, (red, green, blue)
  assert drawn["width"] == sketchView["width"]
