import json
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
import eqWorldFile
import eqZones
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
  placed, short = eqZones.staticPlacementParts(parts, carts[1], "Zone 'qeynos'")
  assert not short and placed[0]["takesAllLights"] is False and numpy.array_equal(placed[0]["lighting"]["colors"], carts[1]["colors"])
  # Without colors of its own a placement gets colors the client computes at load, not drawn yet: it keeps the mesh's vertex light,
  # and holding baked light either way it takes no light lights.wld places.
  bare, _ = eqZones.staticPlacementParts(parts, carts[1] | {"colors": None}, "Zone 'qeynos'")
  assert bare[0]["takesAllLights"] is False and numpy.array_equal(bare[0]["lighting"]["colors"], parts[0]["lighting"]["colors"])


def testPlacementColorsRunningShortLeaveTheRestOfTheMeshItsOwnLight():
  # North Freeport's BARRELONSIDE placements give 78 colors to an 81-vertex mesh; the client reads past them into its memory pool, so the
  # last three vertices keep the mesh's own light here and the build names the actor.
  archive = eqArchive.EQArchive(everquestClient / "freportn.s3d")
  placements = eqZones.objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), "objects.wld"))
  barrel = next(placement for placement in placements if placement["actor"] == "BARRELONSIDE_ACTORDEF")
  parts = eqModels.wldStaticParts(eqArchive.EQArchive(everquestClient / "freportn_obj.s3d"), classicActor("freportn_obj.s3d", "BARRELONSIDE_ACTORDEF"), {}, None)["parts"]
  placed, short = eqZones.staticPlacementParts(parts, barrel, "Zone 'freportn'")
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
