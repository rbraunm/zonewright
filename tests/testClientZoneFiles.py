import json
import struct
import sys
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqgTerrain
import eqLinks
import eqModels
import eqRaces
import eqWorldFile
import eqZones
import loadTimeLight
import viewerLight
import zoneGeometry
import zoneSources

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])


def groundUnder(vertices, triangles, point):
  """The terrain height at a point's [x, y] nearest the point's own height, or None off the terrain."""
  corners = vertices[triangles]
  low, high = corners[:, :, :2].min(1), corners[:, :, :2].max(1)
  best = None
  for index in numpy.flatnonzero((low[:, 0] <= point[0]) & (high[:, 0] >= point[0]) & (low[:, 1] <= point[1]) & (high[:, 1] >= point[1])):
    a, b, c = corners[index]
    v0, v1, v2 = b[:2] - a[:2], c[:2] - a[:2], point[:2] - a[:2]
    denominator = v0[0] * v1[1] - v1[0] * v0[1]
    if abs(denominator) < 1e-9:
      continue
    u = (v2[0] * v1[1] - v1[0] * v2[1]) / denominator
    v = (v0[0] * v2[1] - v2[0] * v0[1]) / denominator
    if u >= -1e-6 and v >= -1e-6 and u + v <= 1 + 1e-6:
      z = a[2] + u * (b[2] - a[2]) + v * (c[2] - a[2])
      best = z if best is None or abs(z - point[2]) < abs(best - point[2]) else best
  return best


@pytest.mark.clientData("clientFiles")
def testClientTerrainStandsWhereItsVerticesAre():
  # Broodlands places its terrain turned a quarter and lowered 52.64; the client draws it where its vertices are, which is where the
  # trees, crates, and barrels placed on it stand.
  archive = eqArchive.EQArchive(everquestClient / "broodlands.eqg")
  zone = eqgFiles.parseZone(archive.read(next(name for name in archive.entries if name.endswith(".zon"))), "broodlands")
  terrain = next(placement for placement in zone["placements"] if placement["model"].endswith(".ter"))
  assert numpy.allclose(terrain["position"], (0, 0, -52.64), atol=0.01) and abs(terrain["rotation"][0] + numpy.pi / 2) < 1e-3
  matrix, offset = eqgFiles.drawnTransform(terrain)
  assert numpy.array_equal(matrix, numpy.identity(3)) and numpy.array_equal(offset, numpy.zeros(3))
  model = eqgFiles.parseModel(archive.read(terrain["model"]), terrain["model"])
  vertices = eqgFiles.placeVertices(model["vertices"], terrain)
  standing = [placement for placement in zone["placements"] if any(key in placement["model"] for key in ("tree", "crate", "barrel"))][:120]
  gaps = [abs(height - placement["position"][2]) for placement in standing if (height := groundUnder(vertices, model["triangles"], numpy.array(placement["position"]))) is not None]
  assert len(gaps) == len(standing) and sum(gap < 3 for gap in gaps) >= 0.75 * len(gaps)
  # Objects are still turned and moved by their placements.
  tree = standing[0]
  matrix, offset = eqgFiles.drawnTransform(tree)
  assert numpy.allclose(offset, tree["position"]) and numpy.allclose(matrix, eqgFiles.placementMatrix(tree))


def testClientWaterMaterialsReadAsLiquids():
  archive = eqArchive.EQArchive(everquestClient / "highpasshold.eqg")
  model = eqgFiles.parseModel(archive.read("ter_highpass05.ter"), "ter_highpass05.ter")
  liquids = {material["name"]: eqModels.eqgLiquid(material) for material in model["materials"]}
  water, falls = liquids["water"], liquids["waterfalls"]
  assert water["liquid"] == "water" and water["textures"] == {"normal": "rc_cavewater_n.dds", "environment": "env_tutorialb_noswap.dds"}
  # e_fWaterColor1 0xFF1A2433 and e_fWaterColor2 0xFF00457F, as 0-1 RGB.
  assert numpy.allclose(water["values"]["waterColor1"], [0x1A / 255, 0x24 / 255, 0x33 / 255])
  assert numpy.allclose(water["values"]["waterColor2"], [0, 0x45 / 255, 0x7F / 255])
  assert numpy.isclose(water["values"]["fresnelBias"], 0.3) and water["values"]["fresnelPower"] == 8.0
  assert falls == {"liquid": "waterfall", "values": {"slides": falls["values"]["slides"]}, "textures": {}} and numpy.allclose(falls["values"]["slides"], [-0.12, -0.32, 0.0, -0.5])
  assert all(liquid is None for name, liquid in liquids.items() if name not in ("water", "waterfalls"))


def testTrianglesDrawnByAStandInAreCountedByStandIn(tmp_path):
  # Highpass Hold's terrain draws 73,069 triangles with Opaque_MPLBump2UV materials, which the preview draws by their diffuse alone,
  # and its 2,996 water triangles opaque; its 204 waterfall triangles draw as the client draws them, and 496 name no material.
  archive = eqArchive.EQArchive(everquestClient / "highpasshold.eqg")
  model = eqgFiles.parseModel(archive.read("ter_highpass05.ter"), "ter_highpass05.ter")
  colors = numpy.tile(numpy.array(eqZones.unlitColor, dtype=numpy.uint8), (len(model["vertices"]), 1))
  part = eqZones.placedEQGPart(model, numpy.identity(3), numpy.zeros(3), colors)
  written = eqModels.writePartsCache(tmp_path, [part], [archive], "Highpass Hold's terrain")
  assert len(model["triangles"]) == 76765 and len(part["triangles"]) == 76765 - 496
  assert written["drawnOtherwise"] == {"mplByDiffuseAlone": 73069, "waterOpaque": 2996}


@pytest.mark.clientData("clientFiles")
def testWhatATerrainZoneHoldsAndThePreviewDoesNotDrawIsNamed():
  # Every one of Ocean of Tears' 8,840 tiles stores the level -300 (its sea), 8,664 a water sheet rectangle; its tiles place 34
  # campfire lights and a haunted light, and the ecosystems on them name 36 flora entries. Commonlands' Druid Ring lake: 42 tiles at -40
  # and one water.dat sheet. A zone without them names none.
  found = {}
  for zoneName in ("oceanoftears", "commonlands", "steamfontmts"):
    source = zoneSources.loadedVariant(everquestClient, zoneName)[1]
    terrain = eqgTerrain.parseTerrain(*zoneSources.terrainFiles(source), zoneName)
    ecosystems = {layer["ecosystem"] for tile in terrain["tiles"] for layer in tile["layers"]}
    found[zoneName] = eqZones.terrainNotDrawn(terrain, ecosystems, eqArchive.EQArchive(source["archive"]))
  assert found["oceanoftears"] == {
    "waterNotDrawn": {"tilesWithLevel": 8840, "levels": {"-300.0": 8840}, "tilesWithSheetRectangle": 8664, "waterDatSheets": 0},
    "radialFloraNotDrawn": {"floraEntries": 36}, "lightsNotDrawn": {"lights": 35, "byDefinition": {"campfire": 34, "haunted_light": 1}},
  }
  assert found["commonlands"]["waterNotDrawn"] == {"tilesWithLevel": 42, "levels": {"-40.0": 42}, "tilesWithSheetRectangle": 0, "waterDatSheets": 1}
  assert found["commonlands"]["lightsNotDrawn"] is None and found["commonlands"]["radialFloraNotDrawn"] == {"floraEntries": 15}
  assert found["steamfontmts"]["waterNotDrawn"] is None and found["steamfontmts"]["radialFloraNotDrawn"] == {"floraEntries": 4}


def testClientArtPlayersPassThroughAndCollisionShellsWithoutMaterialsRead():
  # Highpass Hold's lamp posts are solid art the client flags passable (0x1) on every triangle; the survey reads them as passable.
  archive = eqArchive.EQArchive(everquestClient / "highpasshold.eqg")
  lamp = eqgFiles.parseModel(archive.read("obp_lamposta.mod"), "obp_lamposta.mod")
  builder = zoneGeometry.GeometryBuilder()
  zoneGeometry.addEQGModel(builder, lamp, {"model": "obp_lamposta.mod", "position": (0, 0, 0), "rotation": (0, 0, 0), "scale": 1.0}, True)
  assert zoneGeometry.eqgMaterialSurface(lamp["materials"][0]) == zoneGeometry.surfaceCode["solid"] and (lamp["triangleFlags"] & eqgFiles.passableFlag).all()
  assert builder.build()["triangleSurfaces"].tolist() == [zoneGeometry.surfaceCode["passable"]] * 98
  # Ocean of Tears Green Village's hut collision shell has no materials and names material 0 on its 176 triangles: none drawn.
  data = eqArchive.EQArchive(everquestClient / "oceangreenvillage.eqg").read("obj_hutoutside_col.mod")
  raw = numpy.frombuffer(data, dtype=eqgFiles.modelTriangleType, count=176, offset=len(data) - 176 * eqgFiles.modelTriangleType.itemsize)
  shell = eqgFiles.parseModel(data, "obj_hutoutside_col.mod")
  assert raw["material"].tolist() == [0] * 176 and shell["materials"] == [] and shell["triangleMaterials"].tolist() == [-1] * 176


def classicActor(archiveName, actor):
  return {"archive": archiveName, "wld": archiveName.replace(".s3d", ".wld"), "actor": actor}


def testPlacementsLightTheirMeshWithTheirOwnVertexColors():
  # Every Qeynos placement carries its own vertex colors (objects.wld flag 0x100): one per vertex of its actor's mesh, as baked light
  # and the share of scene light, different for each placement.
  archive = eqArchive.EQArchive(everquestClient / "qeynos.s3d")
  placements = eqZones.objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld"))
  carts = [placement for placement in placements if placement["actor"] == "CART_ACTORDEF"]
  assert len(placements) == 476 and all(placement["colors"] is not None for placement in placements)
  assert [placement["colors"][0].tolist() for placement in carts] == [[82, 82, 19, 190], [0, 0, 0, 201], [51, 51, 11, 181]]
  parts = eqModels.wldStaticParts(eqArchive.EQArchive(everquestClient / "qeynos_obj.s3d"), classicActor("qeynos_obj.s3d", "CART_ACTORDEF"), {}, None)["parts"]
  assert [len(part["vertices"]) for part in parts] == [168] and all(len(placement["colors"]) == 168 for placement in carts)
  placed, short = eqZones.staticPlacementParts(parts, carts[1], carts[1]["colors"], "Zone 'qeynos'")
  assert not short and placed[0]["takesAllLights"] is False and numpy.array_equal(placed[0]["lighting"]["colors"], carts[1]["colors"])


def d3dColors(rgba):
  rgba = rgba.astype(numpy.uint32)
  return (rgba[:, 3] << 24) | (rgba[:, 0] << 16) | (rgba[:, 1] << 8) | rgba[:, 2]


def fnv1a(values):
  hashed = 0x811C9DC5
  for byte in numpy.asarray(values, dtype="<u4").tobytes():
    hashed = ((hashed ^ byte) * 0x01000193) & 0xFFFFFFFF
  return hashed


@pytest.mark.clientData("clientFiles")
def testPlacedObjectsWithoutColorsTakeTheLightTheClientGivesThemAtLoad():
  # Each placement's first color and the FNV-1a hash of all its colors as MQPeridotEmu read them from the RoF2 client: Grimling Forest's
  # cave rocks take the share of scene light of the cave floor a drop beneath them finds (alpha 0x43 to 0x5b), or the least share, 0.1,
  # where it finds no visible floor (0x19), and a little light from the zone's lights; the Plane of Knowledge's rock at scale 0.25 is lit
  # on its unscaled vertices; Crystal Caverns' rock tilted 326/512 of a turn is lit turned the client's way.
  rocks = {
    "grimling": [
      ("LROCK311A_ACTORDEF", (873.13, 1858.009), 0x52000000, 0xA0547C91), ("LROCK311A_ACTORDEF", (867.318, 1856.254), 0x5B0A0A02, 0xC5A9A4A6),
      ("LROCK311A_ACTORDEF", (862.585, 1880.739), 0x430D0D03, 0x13AF68D4), ("LROCK311A_ACTORDEF", (846.449, 1855.322), 0x5B090902, 0xB4AB71BF),
      ("LROCK311A_ACTORDEF", (891.251, 1832.583), 0x190D0D03, 0x2DDCF082), ("LROCK311A_ACTORDEF", (929.805, 1863.853), 0x19000000, 0xBAFEFEF8),
    ],
    "poknowledge": [("POTRANQROCK505_ACTORDEF", (12.368, 554.238), 0xFE040300, 0xB05EE7F1)],
    "crystal": [("CCROCK204_ACTORDEF", (-225.812, 839.006), 0x19010203, 0x522B6DA2)],
  }
  for zoneName, placed in rocks.items():
    archive = eqArchive.EQArchive(everquestClient / f"{zoneName}.s3d")
    floors = loadTimeLight.ShareFloors(eqWorldFile.WorldFile(archive.read(f"{zoneName}.wld"), f"{zoneName}.wld").meshes(), eqZones.colorlessRegionColor[3])
    lights = eqZones.zoneLights(everquestClient, zoneName)
    placements = eqZones.objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld"))
    for actor, (x, y), first, hashed in placed:
      placement = next(placement for placement in placements if placement["actor"] == actor and abs(placement["position"][0] - x) < 0.01 and abs(placement["position"][1] - y) < 0.01)
      assert placement["colors"] is None
      part = eqModels.wldStaticParts(eqArchive.EQArchive(everquestClient / f"{zoneName}_obj.s3d"), classicActor(f"{zoneName}_obj.s3d", actor), {}, None)["parts"][0]
      colors = d3dColors(loadTimeLight.placedColors(part, placement, eqZones.placementRotation(placement), lights, floors))
      assert (int(colors[0]), fnv1a(colors)) == (first, hashed), (zoneName, actor, x, y)


@pytest.mark.clientData("clientFiles")
def testAViewsCharacterTakesTheSpecialAmbientOfTheFloorItStandsOn(tmp_path):
  # The RoF2 player's actor held these shares of scene light in MQPeridotEmu's dumps: 0.1992185 standing in Grimling Forest's cave
  # (its floor's corners at alpha 51), 0.9960927 on the Plane of Knowledge's cobbles. The special ambient is 0.08 times one less the
  # share, each channel truncated to a byte: 16/255 in the cave, nothing on the cobbles.
  body = viewerLight.viewerBody(everquestClient, tmp_path, False)
  assert abs(body["sphere"]["radius"] - 5.17805) < 1e-4 and abs(body["avatarHeight"] - 3.75) < 1e-9
  for zoneName, feet, share, level in (("grimling", (869.1, 1905.9, -271.58), 0.1992185, 16), ("poknowledge", (575.2, 672.0, -120.75), 0.9960927, 0)):
    archive = eqArchive.EQArchive(everquestClient / f"{zoneName}.s3d")
    floors = loadTimeLight.ShareFloors(eqWorldFile.WorldFile(archive.read(f"{zoneName}.wld"), f"{zoneName}.wld").meshes(), eqZones.colorlessRegionColor[3])
    found = viewerLight.viewerShare(floors, feet, body)
    assert abs(found - share) < 1e-6 and viewerLight.specialAmbient(found) == [level / 255] * 3


@pytest.mark.clientData("clientFiles")
def testATiltedPlacementTurnsAsTheClientsActorMatrixDoes():
  # Crystal Caverns places CCROCK204 with heading 384 and tilt 326 (512ths of a turn); the RoF2 client's actor holds this matrix (rows
  # are where +X, +Y, and +Z go; CSimpleActor + 0xe4, read by MQPeridotEmu), which turns the rock's +Z down toward -Y.
  archive = eqArchive.EQArchive(everquestClient / "crystal.s3d")
  placements = eqZones.objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld"))
  rock = next(placement for placement in placements if placement["actor"] == "CCROCK204_ACTORDEF" and abs(placement["position"][0] + 225.812) < 0.01)
  assert (rock["heading"], rock["tilt"]) == (384, 326)
  clientRows = numpy.array([[0.0, 0.653173, -0.757209], [1.0, 0.0, 0.0], [0.0, -0.757209, -0.653173]])
  assert numpy.abs(eqZones.placementRotation(rock).T - clientRows).max() < 1e-5


def testPlacementColorsRunningShortLeaveTheRestOfTheMeshItsOwnLight():
  # North Freeport's BARRELONSIDE placements give 78 colors to an 81-vertex mesh; the client reads past them into its memory pool, so the
  # last three vertices keep the mesh's own light here and the build names the actor.
  archive = eqArchive.EQArchive(everquestClient / "freportn.s3d")
  placements = eqZones.objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld"))
  barrel = next(placement for placement in placements if placement["actor"] == "BARRELONSIDE_ACTORDEF")
  parts = eqModels.wldStaticParts(eqArchive.EQArchive(everquestClient / "freportn_obj.s3d"), classicActor("freportn_obj.s3d", "BARRELONSIDE_ACTORDEF"), {}, None)["parts"]
  placed, short = eqZones.staticPlacementParts(parts, barrel, barrel["colors"], "Zone 'freportn'")
  colors = placed[0]["lighting"]["colors"]
  assert short and len(barrel["colors"]) == 78 and len(colors) == 81
  assert numpy.array_equal(colors[:78], barrel["colors"]) and numpy.array_equal(colors[78:], parts[0]["lighting"]["colors"][78:])


def testAZoneMeshStoringMoreNormalsThanVerticesReadsOnePerVertex():
  # Acrylia's R17 stores 128 normals and colors for 120 vertices; the client reads the first 120 of each (EQGraphicsDX9.dll 0x1001f630).
  worldFile = eqWorldFile.WorldFile(eqArchive.EQArchive(everquestClient / "acrylia.s3d").read("acrylia.wld"), "acrylia.wld")
  fragment = next(fragment for fragment in worldFile.fragmentsOfType(0x36) if fragment.name == "R17_DMSPRITEDEF")
  vertexCount, uvCount, normalCount, colorCount = (int(value) for value in numpy.frombuffer(fragment.body, dtype="<u2", count=4, offset=76))
  assert (vertexCount, uvCount, normalCount, colorCount) == (120, 120, 128, 128)
  normalsOffset = 96 + 6 * vertexCount + (4 if worldFile.isOldFormat else 8) * uvCount
  stored = numpy.frombuffer(fragment.body, dtype=numpy.int8, count=3 * normalCount, offset=normalsOffset).reshape(-1, 3) / 127.0
  mesh = worldFile.mesh(fragment)
  assert numpy.array_equal(mesh["normals"], stored[:120]) and mesh["colors"].shape == (120, 4)
  part = eqModels.wldMeshPart(mesh, {}, eqZones.colorlessRegionColor)
  assert len(part["triangles"]) == 62 and part["lighting"]["normals"].shape == (120, 3)


def testSkinsWithoutUVsDrawAtTheirTexturesCorner():
  # The Plane of Air's wine rack skin CYLINDER01 stores no UVs: the client builds every vertex at (0, 0) (EQGraphicsDX9.dll 0x1004ad50),
  # Blender's (0, 1). Skins take no vertex colors: no baked light and the full share of scene light.
  parts, _ = eqModels.wldSkeletalBindParts(eqArchive.EQArchive(everquestClient / "poair_obj.s3d"), classicActor("poair_obj.s3d", "POAWINE500_ACTORDEF"))
  cylinder = parts[0]
  assert len(cylinder["vertices"]) == 58 and len(cylinder["triangles"]) == 60
  assert numpy.array_equal(cylinder["uvs"], numpy.tile((0.0, 1.0), (58, 1)))
  assert all((part["lighting"]["colors"] == (0, 0, 0, 255)).all() for part in parts)


def testTheClientLoadsAZonesEQGOverItsClassicFiles():
  assert zoneSources.loadedVariant(everquestClient, "arena")[0] == "arena:eqgz:loose"
  assert zoneSources.loadedVariant(everquestClient, "tutorialb")[0] == "tutorialb:eqgz:loose"
  # nektulos.eqg, a copy of nektulos.s3d, holds no model, terrain, or zone file the client loads, so it loads the classic zone.
  assert zoneSources.loadedVariant(everquestClient, "nektulos")[0] == "nektulos:wld"
  arenaLinks = [link["archive"] for link in eqLinks.zoneLinks(everquestClient, "arena")["archives"]]
  assert arenaLinks[0] == "arena.eqg" and not any(name.startswith("arena") and name.endswith(".s3d") for name in arenaLinks)
  assert "nektulos_obj.s3d" in [link["archive"] for link in eqLinks.zoneLinks(everquestClient, "nektulos")["archives"]]


def testALightOfRadiusZeroLightsNothing():
  lights = eqZones.zoneLights(everquestClient, "runnyeye")
  assert len(lights) == 383 and [light["name"] for light in lights if eqZones.lightsNothing(light)] == ["L277_LDEF"]


def testALuclinZonesLooseDataIsNamedAsNeverOpened():
  assert eqZones.unreadZoneFiles(everquestClient, "dawnshroud") == ["dawnshroud.dat"]
  assert eqZones.unreadZoneFiles(everquestClient, "qeynos") == []


@pytest.mark.clientData("clientFiles")
def testALooseTerrainZoneFileStandsOverTheArchives():
  # oldcommons.eqg holds commonlands.zon and .dat and an oldcommons.zon without its .dat; the loose oldcommons.zon beside it, an EQ
  # terrain project named commonlands, is the one the client loads, and its *NAME picks the .dat.
  variants = zoneSources.zoneVariants(everquestClient, "oldcommons")
  assert {key: (source.get("zon"), source.get("zonPath"), source["dat"]) for key, source in variants.items()} == {
    "oldcommons:eqtzp": ("commonlands.zon", None, "commonlands.dat"),
    "oldcommons:eqtzp:loose": (None, everquestClient / "oldcommons.zon", "commonlands.dat"),
  }
  assert zoneSources.loadedVariant(everquestClient, "oldcommons")[0] == "oldcommons:eqtzp:loose"
  zonText, _ = zoneSources.terrainFiles(variants["oldcommons:eqtzp:loose"])
  assert "*MIN_EXTENTS -2176.000 -5376.000 -136.563" in zonText


@pytest.mark.clientData("clientFiles")
def testAPlacementPastItsTilesEdgeTakesItsOwnTilesGround():
  # Elddar Forest lists a bridge 396.406 units along y from its tile's origin, past that tile's 160-unit side: the client reads the
  # ground at 76.406 in the listing tile, about 45 units above the ground under the bridge, which then spans its ravine.
  source = zoneSources.loadedVariant(everquestClient, "elddar")[1]
  terrain = eqgTerrain.parseTerrain(*zoneSources.terrainFiles(source), "elddar")
  tilesByOrigin = {(tile["x"], tile["y"]): tile for tile in terrain["tiles"]}
  bridge = next(placement for placement in terrain["placements"] if placement["model"] == "obj_bridge.mod" and abs(placement["offset"][1] - 396.406) < 1e-3)
  listing = tilesByOrigin[bridge["listingTile"]]
  x, y, _ = bridge["position"]
  under = tilesByOrigin[(x // terrain["tileSize"] * terrain["tileSize"], y // terrain["tileSize"] * terrain["tileSize"])]
  placed = eqgTerrain.placedPosition(terrain, tilesByOrigin, bridge)
  assert abs(placed[2] - (eqgTerrain.tileHeight(terrain, listing, bridge["offset"][0], 76.406) + bridge["offset"][2])) < 1e-3
  assert placed[2] - (eqgTerrain.tileHeight(terrain, under, x - under["x"], y - under["y"]) + bridge["offset"][2]) > 40


def drawnZoneParts(zoneName):
  """The variant importZone draws: its library, its .zon, and its placements as the build makes them, with the build's details."""
  source = eqZones.zoneSource(everquestClient, zoneName)
  library = zoneSources.ModelLibrary(zoneSources.assetArchivePaths(everquestClient, source)[0], zoneName)
  zone = eqZones.clientEQGZone(source, zoneName)
  return library, zone, *eqZones.eqgZoneParts(everquestClient, library, zone, library.archives[0], zoneName)


@pytest.mark.clientData("clientFiles")
def testShortLitFilesGiveTheColorsTheyHold():
  # Delve's door caps count their model's 1152 vertices in 400 bytes: the client reads the 98 colors the file holds and takes them, the
  # rest of its buffer whatever its memory pool held, which the preview draws as no baked light.
  litBytes = eqArchive.EQArchive(everquestClient / "delvea.eqg").read("obj_doorcaps6.lit")
  count, colors = eqZones.eqgLitColors(litBytes, "obj_doorcaps6.lit")
  assert (count, len(litBytes), len(colors)) == (1152, 400, 98) and colors.tolist() == list(struct.unpack_from("<98I", litBytes, 8))
  _, zone, parts, details = drawnZoneParts("delvea")
  assert details["bakedLightPastFileEnd"] == {"obj_doorcaps.mod": 6, "obp_dragvert.mod": 10, "obp_dragvert_.mod": 1} and details["bakedLightNotFitting"] == {}
  part = parts[next(index for index, placement in enumerate(zone["placements"]) if placement["name"].lower() == "obj_doorcaps6")]
  drawn = part["lighting"]["colors"]
  assert len(drawn) == 1152 and numpy.array_equal(drawn[:98], eqZones.bytesRGBA(colors)) and (drawn[98:] == eqZones.unlitColor).all()
  # Its count fits its model, so it is baked geometry: only the lights marked for it reach it.
  assert part["takesAllLights"] is False


@pytest.mark.clientData("clientFiles")
def testPlacedSkinnedModelsStandAtTheFirstKeyOfTheirDefaultAnimation():
  library, zone, parts, details = drawnZoneParts("guardian")
  assert details["animatedModels"] == {
    "obj_ceilingfan.mod": {"placements": 17, "animation": "OBJ_CEILINGFAN_DEFAULT"}, "obj_cntrl_panel.mod": {"placements": 12, "animation": "OBJ_CNTRL_PANEL_DEFAULT"},
    "obj_gearfurnacea.mod": {"placements": 9, "animation": "OBJ_GEARFURNACEA_DEFAULT"}, "obj_turbine.mod": {"placements": 2, "animation": "OBJ_TURBINE_DEFAULT"},
  }
  # The gear furnace's animation starts at its bind pose; loading it, the DLL lowers ROOT_BONE's keys by moddat.ini's ROffset for the
  # name's last three letters, [ULT], which the client's moddat.ini lacks: the default 3.125.
  model = library.model("obj_gearfurnacea.mod")
  tracks = library.animationTracks("OBJ_GEARFURNACEA_DEFAULT")
  bones = model["bones"]
  assert all(
    numpy.allclose(tracks[name][0]["position"], bones["position"][index], atol=1e-4) and numpy.allclose(tracks[name][0]["rotation"], bones["rotation"][index], atol=1e-4)
    for index, name in enumerate(bones["names"])
  )
  offsets = eqRaces.avatarOffsets(everquestClient)
  assert "ULT" not in offsets and eqModels.animationRootDrop("OBJ_GEARFURNACEA_DEFAULT", offsets) == 3.125
  index, placement = next((index, placement) for index, placement in enumerate(zone["placements"]) if placement["model"] == "obj_gearfurnacea.mod")
  matrix, offset = eqgFiles.drawnTransform(placement)
  part = parts[index]
  assert numpy.allclose(part["vertices"], (model["vertices"] - (0, 0, 3.125)) @ matrix.T + offset, atol=1e-4)
  # A CHierarchicalActor takes no baked light and every point light; the skinned effects read its texture coordinates unflipped.
  assert part["takesAllLights"] is True and (part["lighting"]["colors"] == eqZones.unlitColor).all() and numpy.array_equal(part["uvs"], model["uvs"])
  # Names holding _MT_, or whose first _IT is followed by a digit, are not lowered; others take their last three letters' ROffset.
  assert offsets["BAS"] == 0.577 and eqModels.animationRootDrop("L01_BA_1_BAS", offsets) == 0.577
  assert eqModels.animationRootDrop("C05_MT_BAS", offsets) == 0.0 and eqModels.animationRootDrop("X_IT7_BAS", offsets) == 0.0
  assert eqModels.animationRootDrop("X_ITA_IT7_BAS", offsets) == 0.577


@pytest.mark.clientData("clientFiles")
def testZoneBuildsCountWhatTheyDrawOtherwiseByModel():
  # Plane of Shadow's 13618 placements whose baked light does not fit come from 13 models; counted by model, the details stay small.
  _, _, _, details = drawnZoneParts("poshadow")
  notFitting = details["bakedLightNotFitting"]
  assert sum(notFitting.values()) == 13618 and len(notFitting) == 13 and notFitting["obp_florab.mod"] == 3703
  assert len(json.dumps(details, indent=2)) < 2000


@pytest.mark.clientData("clientFiles")
def testImportedEmitterListsNameTheLinesTheClientMakesNoEmitterFor(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "discordtower"})

  imported = stageBlenderServer.session(steps)
  # Every line comes in; Discord Tower's three negative indices and five lifespans of 0 make no emitter in the client.
  assert imported["emitters"] == 160
  negative = "the client makes no emitter for a negative definition index"
  assert imported["emittersNotMade"] == [
    {"reason": f"definition -101: {negative}", "count": 1, "emitters": ["brazier32"]},
    {"reason": f"definition -497: {negative}", "count": 1, "emitters": ["brazier02"]},
    {"reason": f"definition -7: {negative}", "count": 1, "emitters": ["brazier35"]},
    {"reason": "lifespan 0: the client makes an emitter only for a lifespan above 0", "count": 5, "emitters": ["brazier37", "brazier34", "brazier33", "brazier31", "brazier17"]},
  ]
