import json
import struct
import sys
import zlib
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqgWriter
import serverMapDrawing
import serverMapFiles
from serverReference import referenceBytes

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])
float32 = numpy.float32


def clientZoneFiles(zoneName):
  """A client zone's files as its .map is built from them: its archive and the loose .zon the client loads, read in place."""
  return serverMapFiles.zoneFilesOf((everquestClient / f"{zoneName}.eqg").read_bytes(), (everquestClient / f"{zoneName}.zon").read_bytes())


def azoneRoundTrip(turns):
  # azone reads a placement's turns into degrees (eqg_loader.cpp:131, times 180 / 3.14159) and back (map.cpp:681, times 3.14159 / 180),
  # each a float32 multiply by a float32 constant: its build folded "* 3.14159f / 180.0f" into one multiply.
  degrees = (turns * float32(float32(180) / float32(3.14159))).astype(float32)
  return (degrees * float32(float32(3.14159) / float32(180))).astype(float32)


def turnsOf(content):
  return numpy.array([placement["rotation"] for placement in content["placements"]], dtype=float32)


def assertMapReproduces(zoneName):
  """Our .map of a client zone against Peridot's: the same version word and inflated size, our stream inflating to our payload, our
  turns the .zon's own, Peridot's azone's round trip of ours bit for bit, and every other payload byte identical. Returns the turn
  fields the round trip changed."""
  zoneFiles = clientZoneFiles(zoneName)
  ours, peridot = serverMapFiles.mapBytes(zoneFiles), referenceBytes(f"base/{zoneName}.map")
  assert ours[:4] == peridot[:4] == struct.pack("<I", 0x02000000) and ours[8:12] == peridot[8:12]
  assert zlib.decompress(ours[12:]) == serverMapFiles.mapPayload(serverMapFiles.mapContent(zoneFiles))
  ourContent, peridotContent = serverMapFiles.readMap(ours), serverMapFiles.readMap(peridot)
  zone = eqgFiles.parseZone(zoneFiles["zon"], zoneName)
  zoneTurns = [placement["rotation"][::-1] for placement in zone["placements"] if not placement["model"].endswith(".ter")]
  assert numpy.array_equal(turnsOf(ourContent), numpy.array(zoneTurns, dtype=float32))
  assert numpy.array_equal(azoneRoundTrip(turnsOf(ourContent)).view(numpy.uint32), turnsOf(peridotContent).view(numpy.uint32))
  changed = int((turnsOf(ourContent).view(numpy.uint32) != turnsOf(peridotContent).view(numpy.uint32)).sum())
  for ourPlacement, peridotPlacement in zip(ourContent["placements"], peridotContent["placements"]):
    ourPlacement["rotation"] = peridotPlacement["rotation"]
  assert serverMapFiles.mapPayload(ourContent) == zlib.decompress(peridot[12:])
  return changed


@pytest.mark.clientData("serverMaps")
def testMapReaderDecodesPeridotsHighpassHoldToTheLastByte():
  data = referenceBytes("base/highpasshold.map")
  payload = zlib.decompress(data[12:])
  assert len(payload) == 3_675_766 and struct.unpack_from("<9If", payload, 0) == (38539, 220695, 1782, 9600, 191, 1163, 0, 0, 0, 0.0)
  content = serverMapFiles.readMap(data)
  counts = [len(content[key]) for key in ("collidableVertices", "collidableIndices", "nonCollidableVertices", "nonCollidableIndices", "models", "placements")]
  assert counts == [38539, 220695, 1782, 9600, 191, 1163]
  assert serverMapFiles.mapPayload(content) == payload


@pytest.mark.clientData("serverMaps")
def testMapWriterReproducesPeridotsHighpassHold():
  assert assertMapReproduces("highpasshold") == 1026


@pytest.mark.clientData("serverMaps")
@pytest.mark.parametrize("zoneName", ["thulehouse2", "freeporteast", "freeportwest"])
def testMapWriterReproducesPeridotsOtherEQGZones(zoneName):
  assert assertMapReproduces(zoneName) > 0


@pytest.mark.clientData("serverMaps")
def testMapCollisionAgreesWithTheZoneReader():
  zoneFiles = clientZoneFiles("highpasshold")
  zone = eqgFiles.parseZone(zoneFiles["zon"], "highpasshold")
  models = {name: eqgFiles.parseModel(data, name) for name, data in zoneFiles["models"].items()}
  terrain, placed = [], []
  for placement in zone["placements"]:
    model = models[placement["model"]]
    solid = model["triangles"][(model["triangleFlags"] & eqgFiles.passableFlag) == 0]
    (terrain if placement["model"].endswith(".ter") else placed).append(eqgFiles.placeVertices(model["vertices"], placement)[solid])
  reader = numpy.concatenate(terrain + placed)
  assert reader.shape == (272_230, 3, 3)
  peridot = serverMapFiles.inZoneAxes(serverMapFiles.mapCollision(referenceBytes("base/highpasshold.map")))
  ours = serverMapFiles.inZoneAxes(serverMapFiles.mapCollision(serverMapFiles.mapBytes(zoneFiles)))
  # The 73,565 terrain triangles are stored, not computed: exact. Placed ones are computed in float32, ours from the .zon's turns
  # (6.7e-5 measured) and Peridot's from azone's round trip of them (3.23e-4 measured).
  assert numpy.array_equal(peridot[:73_565], reader[:73_565]) and numpy.array_equal(ours[:73_565], reader[:73_565])
  assert numpy.abs(ours - reader).max() <= 1e-4 and numpy.abs(peridot - reader).max() <= 3.3e-4


@pytest.mark.clientData("serverMaps")
def testWaterWriterReproducesPeridotsEQGZones():
  for zoneName, size in (("highpasshold", 382), ("thulehouse2", 434), ("freeporteast", 642), ("freeportwest", 798)):
    regions = eqgFiles.parseZone((everquestClient / f"{zoneName}.zon").read_bytes(), zoneName)["regions"]
    assert all(region["rotation"][0] != 0 for region in regions)
    # Every region's turn is written 0: the awater that wrote Peridot's files (before zone-utilities 59e0552) wrote 0 for any turn, and
    # our writer refuses a turned region.
    unturned = [region | {"rotation": (0.0, 0.0, 0.0)} for region in regions]
    assert serverMapFiles.waterBytes(unturned) == referenceBytes(f"water/{zoneName}.wtr") and len(referenceBytes(f"water/{zoneName}.wtr")) == size


@pytest.mark.clientData("serverMaps")
def testAnEmptyWaterMapIsGuildhallsFile():
  assert serverMapFiles.waterBytes([]) == referenceBytes("water/guildhall.wtr") == b"EQEMUWATER" + struct.pack("<2I", 2, 0)
  assert serverMapFiles.readWater(referenceBytes("water/guildhall.wtr")) == []


@pytest.mark.clientData("serverMaps")
def testNavContainerRoundTrips():
  data = referenceBytes("nav/highpasshold.nav")
  nav = serverMapFiles.readNav(data)
  parameters = nav["parameters"]
  assert numpy.array_equal(numpy.array(parameters["origin"], dtype=float32), numpy.array([-839.33337, -410.35071, -1650.574], dtype=float32))
  assert parameters["tileWidth"] == parameters["tileHeight"] == float32(409.6) and (parameters["maximumTiles"], parameters["maximumPolygons"]) == (64, 65536)
  assert len(nav["tiles"]) == 28
  areas, counts = numpy.unique(numpy.concatenate([tile["polygons"]["areaAndType"] & 0x3F for tile in nav["tiles"]]), return_counts=True)
  assert dict(zip(areas.tolist(), counts.tolist())) == {0: 7692, 1: 452, 11: 53}
  assert serverMapFiles.navPayload(nav) == zlib.decompress(data[21:])


def modelFile(kind, positions, triangles):
  positions = numpy.array(positions, dtype=numpy.float32)
  normals = numpy.tile(numpy.array([0, 0, 1], dtype=numpy.float32), (len(positions), 1))
  return eqgWriter.modelBytes(kind, [], positions, normals, numpy.zeros((len(positions), 2)), triangles, [eqgFiles.noMaterial] * len(triangles), [0] * len(triangles))


def squareTerrain(half, height=0.0, around=(0.0, 0.0)):
  x, y = around
  return [[x - half, y - half, height], [x + half, y - half, height], [x + half, y + half, height], [x - half, y + half, height]], [[0, 1, 2], [0, 2, 3]]


def zoneFiles(modelFiles, placements, regions=()):
  """A small zone's files: model files by their .zon spelling (stored under the lowercase name, as an archive keys them) and placements
  of them, each (model, name, position, rotation, scale)."""
  records = [{"model": model, "name": name, "position": position, "rotation": rotation, "scale": scale} for model, name, position, rotation, scale in placements]
  zon = eqgWriter.zoneBytes(list(modelFiles), records, list(regions), [])
  return {"zon": zon, "models": {name.lower(): data for name, data in modelFiles.items()}}


def crateFile():
  return modelFile("mod", [[0, 0, 0], [2, 0, 0], [0, 2, 0], [0, 0, 2]], [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])


def testMapWriterRefusesWhatTheServerWouldDrop():
  terrain = modelFile("ter", *squareTerrain(50))
  base = {"ter_test.ter": terrain}
  terrainPlacement = ("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)
  missing = zoneFiles(base | {"obj_crate.mod": crateFile()}, [terrainPlacement, ("obj_crate.mod", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0)])
  del missing["models"]["obj_crate.mod"]
  with pytest.raises(ValueError, match=r"Placement 1 'OBJ_crate01' of obj_crate.mod: the zone's files hold no obj_crate.mod"):
    serverMapFiles.mapBytes(missing)
  coinciding = zoneFiles(base | {"obj_a).mod": crateFile(), "obj_a_.mod": crateFile()},
    [terrainPlacement, ("obj_a).mod", "OBJ_a01", (5, 5, 0), (0, 0, 0), 1.0), ("obj_a_.mod", "OBJ_a02", (9, 5, 0), (0, 0, 0), 1.0)])
  with pytest.raises(ValueError, match=r"Models obj_a\)\.mod and obj_a_\.mod share the map name obj_a_\.mod"):
    serverMapFiles.mapBytes(coinciding)
  namedTerrain = zoneFiles(base | {"obj_crate.mod": crateFile()}, [terrainPlacement, ("obj_crate.mod", "TER_crate", (5, 5, 0), (0, 0, 0), 1.0)])
  with pytest.raises(ValueError, match=r"Placement 1 'TER_crate' of obj_crate.mod: its name starts TER, so azone would bake it"):
    serverMapFiles.mapBytes(namedTerrain)
  unplaceable = zoneFiles(base | {"obj_crate.mod": crateFile()}, [terrainPlacement, ("obj_crate.mod", "OBJ_crate01", (5, float("inf"), 0), (0, 0, 0), 1.0)])
  with pytest.raises(ValueError, match=r"Placement 1 'OBJ_crate01' of obj_crate.mod holds a number that is not finite"):
    serverMapFiles.mapBytes(unplaceable)
  upperTerrain = zoneFiles({"TER_test.TER": terrain}, [("TER_test.TER", "ground", (0, 0, 0), (0, 0, 0), 1.0)])
  with pytest.raises(ValueError, match=r"Placement 0 'ground' of TER_test.TER: azone would place a terrain"):
    serverMapFiles.mapBytes(upperTerrain)
  # A model spelled with ')' takes '_' in the model list and in every placement of it, the one string the server matches them by.
  parenthesized = zoneFiles(base | {"OBJ_Crate).MOD": crateFile()},
    [terrainPlacement, ("OBJ_Crate).MOD", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0), ("OBJ_Crate).MOD", "OBJ_crate02", (9, 5, 0), (0, 0, 0), 1.0)])
  content = serverMapFiles.readMap(serverMapFiles.mapBytes(parenthesized))
  assert [model["name"] for model in content["models"]] == ["OBJ_Crate_.MOD"]
  assert [placement["name"] for placement in content["placements"]] == ["OBJ_Crate_.MOD", "OBJ_Crate_.MOD"]
  assert len(serverMapFiles.mapCollision(serverMapFiles.mapBytes(parenthesized))) == 2 + 2 * 4


def testZoneFilesOfReadsTheZonAndTheModelsItNamesFromArchiveBytes():
  files = zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50)), "obj_crate.mod": crateFile()},
    [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0), ("obj_crate.mod", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0)])
  archive = eqgWriter.archiveBytes(files["models"] | {"test.zon": files["zon"], "obj_spare.mod": crateFile()})
  assert serverMapFiles.zoneFilesOf(archive) == files
  loose = zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50))}, [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)])["zon"]
  assert serverMapFiles.zoneFilesOf(archive, loose) == {"zon": loose, "models": {"ter_test.ter": files["models"]["ter_test.ter"]}}
  twoZones = eqgWriter.archiveBytes(files["models"] | {"test.zon": files["zon"], "other.zon": loose})
  with pytest.raises(ValueError, match="The zone archive holds 2 .zon files"):
    serverMapFiles.zoneFilesOf(twoZones)


def testMapCollisionPlacesATurnedModelAsTheZoneReaderDoes():
  turns = (0.7, -0.3, 0.2)
  files = zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50)), "obj_crate.mod": crateFile()},
    [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0), ("obj_crate.mod", "OBJ_crate01", (12, -7, 3), turns, 2.5)])
  collision = serverMapFiles.inZoneAxes(serverMapFiles.mapCollision(serverMapFiles.mapBytes(files)))
  zone = eqgFiles.parseZone(files["zon"], "test.zon")
  crate = eqgFiles.parseModel(files["models"]["obj_crate.mod"], "obj_crate.mod")
  expected = eqgFiles.placeVertices(crate["vertices"], zone["placements"][1])[crate["triangles"]]
  terrainCorners = numpy.array(squareTerrain(50)[0], dtype=numpy.float32)[[[0, 1, 2], [0, 2, 3]]]
  assert numpy.array_equal(collision[:2], terrainCorners) and numpy.abs(collision[2:] - expected).max() < 1e-5


def testWaterWriterWritesEachPrefixsTypeAndTheExtentsAsStored():
  regions = [{"name": name, "center": (10.0 * index, -5.0, 2.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (4.0, -6.0, 3.0)}
    for index, name in enumerate(["AWT_pond", "ALV_pit", "ATP_10_west", "apk_arena"])]
  records = serverMapFiles.readWater(serverMapFiles.waterBytes(regions))
  assert [record["type"] for record in records] == [1, 2, 3, 4]
  assert all(record["position"] == region["center"] and record["rotation"] == (0, 0, 0) and record["scale"] == (1, 1, 1) and record["halfExtents"] == (4, -6, 3)
    for record, region in zip(records, regions))


def testWaterWriterRefusesAnUnknownPrefixATurnAndAZeroExtent():
  def region(name="AWT_pond", rotation=(0.0, 0.0, 0.0), halfExtents=(4.0, 6.0, 3.0), center=(0.0, 0.0, 0.0)):
    return {"name": name, "center": center, "rotation": rotation, "halfExtents": halfExtents}
  with pytest.raises(ValueError, match=r"Region 'ASL_goo': no server region type for its prefix .*awater would write it as Water"):
    serverMapFiles.waterBytes([region(), region("ASL_goo")])
  with pytest.raises(ValueError, match=r"Region 'AWT_pond' is turned \[-128.0, 0.0, 0.0\]"):
    serverMapFiles.waterBytes([region(rotation=(-128.0, 0.0, 0.0))])
  with pytest.raises(ValueError, match=r"Region 'AWT_pond' has a zero half extent: \[4.0, 0.0, 3.0\]"):
    serverMapFiles.waterBytes([region(halfExtents=(4.0, 0.0, 3.0))])
  with pytest.raises(ValueError, match=r"Region 'AWT_pond' holds a number that is not finite"):
    serverMapFiles.waterBytes([region(center=(0.0, float("nan"), 0.0))])


def oneTileNav():
  header = {
    "magic": serverMapFiles.detourMagic, "version": serverMapFiles.detourVersion, "x": 0, "y": 0, "layer": 0, "userID": 0,
    "polygonCount": 1, "vertexCount": 3, "maximumLinkCount": 3, "detailMeshCount": 1, "detailVertexCount": 0, "detailTriangleCount": 1,
    "boundingVolumeNodeCount": 0, "offMeshConnectionCount": 0, "offMeshBase": 1, "walkableHeight": 6.5, "walkableRadius": 1.25,
    "walkableClimb": 6.5, "boundsLow": (0.0, 0.0, 0.0), "boundsHigh": (10.0, 1.0, 10.0), "quantizeFactor": 1.25,
  }
  polygons = numpy.zeros(1, dtype=serverMapFiles.navPolygonType)
  polygons["vertices"][0, :3], polygons["vertexCount"], polygons["flags"] = (0, 1, 2), 3, 1
  tile = {
    "reference": 1, "header": header, "vertices": numpy.array([[0, 0, 0], [10, 0, 0], [0, 0, 10]], dtype=numpy.float32), "polygons": polygons,
    "links": numpy.zeros(3, dtype=serverMapFiles.navLinkType), "detailMeshes": numpy.zeros(1, dtype=serverMapFiles.detailMeshType),
    "detailVertices": numpy.zeros((0, 3), dtype=numpy.float32), "detailTriangles": numpy.array([[0, 1, 2, 0]], dtype=numpy.uint8),
    "boundingVolumeNodes": numpy.zeros(0, dtype=serverMapFiles.boundingVolumeNodeType), "offMeshConnections": numpy.zeros(0, dtype=serverMapFiles.offMeshConnectionType),
  }
  return {"parameters": {"origin": (0.0, 0.0, 0.0), "tileWidth": 409.5, "tileHeight": 409.5, "maximumTiles": 1, "maximumPolygons": 4}, "tiles": [tile]}


def testReadersRefuseTrailingAndMissingBytes():
  files = zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50))}, [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)])
  data = serverMapFiles.mapBytes(files)
  assert len(serverMapFiles.mapCollision(data)) == 2
  with pytest.raises(ValueError, match=rf"stream from byte 12 ends at byte {len(data)}, the file at byte {len(data) + 1}"):
    serverMapFiles.readMap(data + b"\0")
  water = serverMapFiles.waterBytes([{"name": "AWT_pond", "center": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (4.0, 6.0, 3.0)}])
  assert len(serverMapFiles.readWater(water)) == 1
  with pytest.raises(ValueError, match=r"region 0 at byte 18 needs 52 bytes; 51 remain"):
    serverMapFiles.readWater(water[:-1])
  nav = oneTileNav()
  decoded = serverMapFiles.readNav(serverMapFiles.navFile(serverMapFiles.navPayload(nav)))
  assert decoded["tiles"][0]["reference"] == 1 and numpy.array_equal(decoded["tiles"][0]["vertices"], nav["tiles"][0]["vertices"])
  nav["tiles"][0]["reference"] = 0
  with pytest.raises(ValueError, match=r"tile 0 at byte 32 has reference 0 and size \d+; the server drops the whole mesh"):
    serverMapFiles.readNav(serverMapFiles.navFile(serverMapFiles.navPayload(nav)))


def testDrawCollisionShowsTheTopSurfaceAndTheBoxes():
  floor, floorTriangles = squareTerrain(50)
  plateau, plateauTriangles = squareTerrain(10, 10.0, (-25, -35))
  terrain = modelFile("ter", floor + plateau, floorTriangles + [[index + 4 for index in triangle] for triangle in plateauTriangles])
  files = zoneFiles({"ter_test.ter": terrain}, [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)])
  collision = serverMapFiles.mapCollision(serverMapFiles.mapBytes(files))
  regions = serverMapFiles.readWater(serverMapFiles.waterBytes([{"name": "AWT_pond", "center": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (10.0, 10.0, 5.0)}]))
  frame = serverMapDrawing.collisionFrame(collision, 200)
  assert frame.size == (200, 200)
  image = serverMapDrawing.drawCollision(collision, regions, frame)

  def at(point):
    return image.getpixel(tuple(int(value) for value in frame.pixel(point)))
  # The floor in the lowest height's color, the plateau over it in the highest's, both lit from the north-west; the margin bare;
  # the box's east side in the water color.
  assert at((25, 25)) == (45, 73, 104) and at((-25, -35)) == (212, 211, 204)
  assert at((-52, -52)) == serverMapDrawing.backgroundColor
  assert at((0, -10)) == serverMapDrawing.regionColors[1]
  # Raised plateau triangles differ, in red on the third plan; the floor matches and stays faded.
  raised = collision.copy()
  raised[2:, :, 2] += 5
  sheet = serverMapDrawing.drawCollisionComparison(collision, raised, regions, ("before", "after"), 200)
  assert sheet.size == (3 * 200 + 2 * serverMapDrawing.panelGap, 200 + serverMapDrawing.titleHeight)

  def onDifference(point):
    x, y = frame.pixel(point)
    return sheet.getpixel((int(x) + 2 * (200 + serverMapDrawing.panelGap), int(y) + serverMapDrawing.titleHeight))
  assert onDifference((-25, -35)) == serverMapDrawing.differenceColor and onDifference((25, 25)) not in (serverMapDrawing.differenceColor, (45, 73, 104))
