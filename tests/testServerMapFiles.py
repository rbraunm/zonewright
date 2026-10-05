import json
import struct
import sys
import zlib
from pathlib import Path

import numpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
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
@pytest.mark.parametrize(("zoneName", "changedTurns"), [("thulehouse2", 2365), ("freeporteast", 2320), ("freeportwest", 1946)])
def testMapWriterReproducesPeridotsOtherEQGZones(zoneName, changedTurns):
  assert assertMapReproduces(zoneName) == changedTurns


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
  brokenCrate = modelFile("mod", [[0, 0, 0], [2, 0, 0], [0, float("nan"), 0], [0, 0, 2]], [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
  unreadable = zoneFiles(base | {"obj_crate.mod": brokenCrate}, [terrainPlacement, ("obj_crate.mod", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0)])
  with pytest.raises(ValueError, match=r"^Model obj_crate\.mod's vertices holds a number that is not finite$"):
    serverMapFiles.mapBytes(unreadable)
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
    for index, name in enumerate(["AWT_pond", "ALV_pit", "ATP_10_west", "APK_arena"])]
  records = serverMapFiles.readWater(serverMapFiles.waterBytes(regions))
  assert [record["type"] for record in records] == [1, 2, 3, 4]
  assert all(record["position"] == region["center"] and record["rotation"] == (0, 0, 0) and record["scale"] == (1, 1, 1) and record["halfExtents"] == (4, -6, 3)
    for record, region in zip(records, regions))


def testWaterWriterRefusesAnUnknownPrefixATurnAndAZeroExtent():
  def region(name="AWT_pond", rotation=(0.0, 0.0, 0.0), halfExtents=(4.0, 6.0, 3.0), center=(0.0, 0.0, 0.0)):
    return {"name": name, "center": center, "rotation": rotation, "halfExtents": halfExtents}
  # awater matches prefixes in their case (water_map.cpp:258), and how the client reads another case is untraced.
  for name in ("ASL_goo", "awt_pond", "Apk_arena"):
    with pytest.raises(ValueError, match=rf"^Region '{name}': no server region type for its prefix \(known, in this case: AWT_, ALV_, ATP_, APK_\)$"):
      serverMapFiles.waterBytes([region(), region(name)])
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


def mapFileOf(stream, compressedSize, inflatedSize):
  return struct.pack("<3I", 0x02000000, compressedSize, inflatedSize) + stream


def crateZoneFiles():
  return zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50)), "obj_crate.mod": crateFile()},
    [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0), ("obj_crate.mod", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0)])


def testMapReaderRefusesTrailingAndMissingBytesInTheFileTheStreamAndThePayload():
  payload = serverMapFiles.mapPayload(serverMapFiles.mapContent(crateZoneFiles()))
  stream = zlib.compress(payload)
  assert len(serverMapFiles.mapCollision(mapFileOf(stream, len(stream), len(payload)))) == 2 + 4
  with pytest.raises(ValueError, match=rf"^the \.map: the header's {len(stream)}-byte stream from byte 12 ends at byte {12 + len(stream)}, the file at byte {13 + len(stream)}$"):
    serverMapFiles.readMap(mapFileOf(stream, len(stream), len(payload)) + b"\0")
  longer, shorter = zlib.compress(payload + b"\0"), zlib.compress(payload[:-1])
  with pytest.raises(ValueError, match=rf"^the \.map \(inflated\): 1 bytes follow the placements, from byte {len(payload)}$"):
    serverMapFiles.readMap(mapFileOf(longer, len(longer), len(payload) + 1))
  with pytest.raises(ValueError, match=rf"^the \.map \(inflated\): placement 0 \(obj_crate\.mod\) at byte {len(payload) - 36} needs 36 bytes; 35 remain$"):
    serverMapFiles.readMap(mapFileOf(shorter, len(shorter), len(payload) - 1))
  # The server inflates without checking the result (map.cpp:456); the reader checks the stream's end, what follows it, and its size.
  with pytest.raises(ValueError, match=r"^the \.map: the zlib stream from byte 12 ends before its last block$"):
    serverMapFiles.readMap(mapFileOf(stream[:-4], len(stream) - 4, len(payload)))
  with pytest.raises(ValueError, match=rf"^the \.map: 2 bytes follow the zlib stream, from byte {12 + len(stream)}$"):
    serverMapFiles.readMap(mapFileOf(stream + b"\0\0", len(stream) + 2, len(payload)))
  with pytest.raises(ValueError, match=rf"^the \.map: the zlib stream from byte 12 inflates to {len(payload)} bytes; the header says {len(payload) + 5}$"):
    serverMapFiles.readMap(mapFileOf(stream, len(stream), len(payload) + 5))
  with pytest.raises(ValueError, match="a V1 map"):
    serverMapFiles.readMap(struct.pack("<I", 0x01000000) + bytes(48))


def testMapReaderRefusesIndicesPastTheirVerticesARepeatedModelAndPlacementGroupsOrTerrainTiles():
  content = serverMapFiles.readMap(serverMapFiles.mapBytes(crateZoneFiles()))
  assert [len(content[key]) for key in ("collidableVertices", "collidableIndices", "nonCollidableVertices", "nonCollidableIndices")] == [4, 6, 0, 0]

  def refusal(changed):
    with pytest.raises(ValueError) as refused:
      serverMapFiles.readMap(serverMapFiles.mapFile(serverMapFiles.mapPayload(content | changed)))
    return str(refused.value)
  pastEnd = content["collidableIndices"].copy()
  pastEnd[4] = 4
  indicesStart = 40 + 4 * 12
  assert refusal({"collidableIndices": pastEnd}) == f"the .map (inflated): the 6 collidableIndices from byte {indicesStart} are not whole triangles of indices under 4"
  assert refusal({"collidableIndices": content["collidableIndices"][:5]}) == f"the .map (inflated): the 5 collidableIndices from byte {indicesStart} are not whole triangles of indices under 4"
  polygonStart = indicesStart + 6 * 4 + len(b"obj_crate.mod\0") + 8 + 4 * 12
  crate = content["models"][0]
  polygons = crate["polygons"].copy()
  polygons["indices"][3, 2] = 4
  assert refusal({"models": [crate | {"polygons": polygons}]}) == f"the .map (inflated): model obj_crate.mod's polygons from byte {polygonStart} index past its 4 vertices"
  assert refusal({"models": [crate, crate]}) == f"the .map (inflated): model 1 at byte {polygonStart + 4 * 13} is named obj_crate.mod, as model 0 is; the server keeps the last"
  payload = serverMapFiles.mapPayload(content)
  for offset, layout, value, counts in ((24, "<I", 1, "1 placement groups and 0 terrain tiles (0 quads per tile, 0.0"),
      (28, "<I", 2, "0 placement groups and 2 terrain tiles (0 quads per tile, 0.0"), (32, "<I", 3, "0 placement groups and 0 terrain tiles (3 quads per tile, 0.0"),
      (36, "<f", 1.0, "0 placement groups and 0 terrain tiles (0 quads per tile, 1.0")):
    header = bytearray(payload[:40])
    struct.pack_into(layout, header, offset, value)
    with pytest.raises(ValueError) as refused:
      serverMapFiles.readMap(serverMapFiles.mapFile(bytes(header) + payload[40:]))
    assert str(refused.value) == f"the .map (inflated): {counts} units per vertex); zonewright reads EQG zone maps, which hold none"


def testMapCollisionRefusesAPlacementNamingNoModel():
  content = serverMapFiles.readMap(serverMapFiles.mapBytes(crateZoneFiles()))
  content["placements"][0]["name"] = "obj_barrel.mod"
  with pytest.raises(ValueError, match=r"^the \.map: placement 0 names model obj_barrel\.mod, which the map does not hold; the server would skip it$"):
    serverMapFiles.mapCollision(serverMapFiles.mapFile(serverMapFiles.mapPayload(content)))


def testMapWriterMergesNegativeZeroWithZeroKeepingTheFirstSeen():
  for first, second in ((-0.0, 0.0), (0.0, -0.0)):
    positions = [[-50, -50, first], [50, -50, 0], [50, 50, 0], [-50, -50, second], [50, 50, 0], [-50, 50, 0]]
    files = zoneFiles({"ter_test.ter": modelFile("ter", positions, [[0, 1, 2], [3, 4, 5]])}, [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)])
    content = serverMapFiles.readMap(serverMapFiles.mapBytes(files))
    assert content["collidableVertices"].tolist() == [[-50, -50, 0], [-50, 50, 0], [50, 50, 0], [50, -50, 0]]
    assert content["collidableIndices"].tolist() == [0, 1, 2, 0, 2, 3]
    assert numpy.signbit(content["collidableVertices"][:, 2]).tolist() == [bool(numpy.signbit(first)), False, False, False]


def testMapWriterNamesEachPlacementsModelAsItsModelEntrySpellsIt():
  # azone names each .zon model entry by its own spelling and each placement by its entry's (eqg_loader.cpp:103-138), so two spellings
  # of one file are two models of the same shape.
  files = zoneFiles({"ter_test.ter": modelFile("ter", *squareTerrain(50)), "OBJ_Crate.MOD": crateFile(), "obj_crate.mod": crateFile()},
    [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0), ("OBJ_Crate.MOD", "OBJ_crate01", (5, 5, 0), (0, 0, 0), 1.0),
      ("obj_crate.mod", "OBJ_crate02", (9, 5, 0), (0, 0, 0), 1.0), ("OBJ_Crate.MOD", "OBJ_crate03", (13, 5, 0), (0, 0, 0), 1.0)])
  content = serverMapFiles.readMap(serverMapFiles.mapBytes(files))
  assert [model["name"] for model in content["models"]] == ["OBJ_Crate.MOD", "obj_crate.mod"]
  assert numpy.array_equal(content["models"][0]["vertices"], content["models"][1]["vertices"])
  assert [placement["name"] for placement in content["placements"]] == ["OBJ_Crate.MOD", "obj_crate.mod", "OBJ_Crate.MOD"]
  assert len(serverMapFiles.collisionTriangles(content)) == 2 + 3 * 4


def testWaterReaderRefusesTrailingAndMissingBytesAndOtherFormats():
  water = serverMapFiles.waterBytes([{"name": "AWT_pond", "center": (1.0, 2.0, 3.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (4.0, 6.0, 3.0)}])
  assert serverMapFiles.readWater(water) == [{"type": 1, "position": (1.0, 2.0, 3.0), "rotation": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0), "halfExtents": (4.0, 6.0, 3.0)}]
  with pytest.raises(ValueError, match=r"^the \.wtr: 1 bytes follow the 1 regions, from byte 70$"):
    serverMapFiles.readWater(water + b"\0")
  # Cut short, the server would log "Loaded Water Map" and drop it.
  with pytest.raises(ValueError, match=r"^the \.wtr: region 0 at byte 18 needs 52 bytes; 51 remain$"):
    serverMapFiles.readWater(water[:-1])
  with pytest.raises(ValueError, match="a V1 water map"):
    serverMapFiles.readWater(b"EQEMUWATER" + struct.pack("<2I", 1, 0))
  with pytest.raises(ValueError, match=r"^the \.wtr: version 3 at byte 10 is not 2$"):
    serverMapFiles.readWater(b"EQEMUWATER" + struct.pack("<2I", 3, 0))
  with pytest.raises(ValueError, match=r"^the \.wtr: magic b'EQEMUWATRR' at byte 0 is not b'EQEMUWATER'$"):
    serverMapFiles.readWater(b"EQEMUWATRR" + water[10:])


def framedNav(payload, tail=b""):
  """An EQNAVMESH file around a payload, framed by hand so the encoder's own check does not refuse it first; tail follows the stream."""
  compressed = zlib.compress(payload) + tail
  return struct.pack("<9s3I", b"EQNAVMESH", 2, len(compressed), len(payload)) + compressed


def testNavReaderAndEncoderRefuseWhatTheServerWouldMisread():
  nav = oneTileNav()
  payload = serverMapFiles.navPayload(nav)
  data = serverMapFiles.navFile(payload)
  assert data == framedNav(payload)
  decoded = serverMapFiles.readNav(data)
  assert decoded["tiles"][0]["reference"] == 1 and numpy.array_equal(decoded["tiles"][0]["vertices"], nav["tiles"][0]["vertices"])
  # One tile: the tile count and parameters (32 bytes), its reference and size (8), then its data to the payload's end: the 100-byte
  # header, 3 vertices, 1 polygon, 3 links, 1 detail mesh and 1 detail triangle.
  tileSize = len(payload) - 40
  assert struct.unpack_from("<Ii", payload, 32) == (1, tileSize) and tileSize == 100 + 3 * 12 + 32 + 3 * 12 + 12 + 4
  tileData = payload[40:]

  def withTileEntry(reference, size, data=tileData):
    return payload[:32] + struct.pack("<Ii", reference, size) + data

  def refusals(brokenPayload):
    """What the reader says of a file holding the payload, and what the encoder says when asked to write one."""
    with pytest.raises(ValueError) as reading:
      serverMapFiles.readNav(framedNav(brokenPayload))
    with pytest.raises(ValueError) as writing:
      serverMapFiles.navFile(brokenPayload)
    return str(reading.value), str(writing.value)
  negativeCount = bytearray(tileData)
  struct.pack_into("<i", negativeCount, 48, -1)
  for brokenPayload, refusal in (
    (payload + b"\0", f": 1 bytes follow the 1 tiles, from byte {len(payload)}"),
    (withTileEntry(1, tileSize + 1, tileData + b"\0"), f", tile 0 (reference 1) (0, 0, layer 0) is {tileSize + 1} bytes, but its header's counts make {tileSize}"),
    # The size agrees with the bytes that follow, but the header counts more: Detour's addTile would read and write past the tile.
    (withTileEntry(1, 104, tileData[:104]), f", tile 0 (reference 1) (0, 0, layer 0) is 104 bytes, but its header's counts make {tileSize}"),
    (withTileEntry(1, tileSize, bytes(negativeCount)), ", tile 0 (reference 1) (0, 0, layer 0): its header's boundingVolumeNodeCount is -1, below zero"),
    # The server drops the whole mesh on a zero tile reference or size (pathfinder_nav_mesh.cpp:473-491).
    (withTileEntry(0, tileSize), f": tile 0 at byte 32 has reference 0 and size {tileSize}; the server drops the whole mesh on a zero reference or size"),
    (withTileEntry(1, 0), ": tile 0 at byte 32 has reference 1 and size 0; the server drops the whole mesh on a zero reference or size"),
  ):
    assert refusals(brokenPayload) == (f"the .nav (inflated){refusal}", f"the nav payload{refusal}")
  for magic, version in ((0x44414E56, serverMapFiles.detourVersion), (serverMapFiles.detourMagic, 8)):
    foreign = oneTileNav()
    foreign["tiles"][0]["header"] |= {"magic": magic, "version": version}
    refusal = f", tile 0 (reference 1): magic {magic:#x} and version {version} at byte 0 are not Detour's 0x444e4156 and 7"
    assert refusals(serverMapFiles.navPayload(foreign)) == (f"the .nav (inflated){refusal}", f"the nav payload{refusal}")

  for brokenFile, refusal in (
    (data + b"\0", f"the header's {len(data) - 21}-byte stream from byte 21 ends at byte {len(data)}, the file at byte {len(data) + 1}"),
    (framedNav(payload, b"garbage"), f"7 bytes follow the zlib stream, from byte {len(data)}"),
    (data[:13] + struct.pack("<I", len(data) - 31) + data[17:-10], "the zlib stream from byte 21 ends before its last block"),
    (data[:11], "file header at byte 0 needs 21 bytes; 11 remain"),
    (b"EQNAVMESX" + data[9:], "magic b'EQNAVMESX' at byte 0 is not b'EQNAVMESH'"),
    (data[:9] + struct.pack("<I", 3) + data[13:], "version 3 at byte 9 is not 2"),
  ):
    with pytest.raises(ValueError) as reading:
      serverMapFiles.readNav(brokenFile)
    assert str(reading.value) == f"the .nav: {refusal}"


def testDrawCollisionShowsTheTopSurfaceAndEachBoxWhereItIs():
  floor, floorTriangles = squareTerrain(50)
  plateau, plateauTriangles = squareTerrain(10, 10.0, (-25, -35))
  terrain = modelFile("ter", floor + plateau, floorTriangles + [[index + 4 for index in triangle] for triangle in plateauTriangles])
  files = zoneFiles({"ter_test.ter": terrain}, [("ter_test.ter", "TER_test", (0, 0, 0), (0, 0, 0), 1.0)])
  collision = serverMapFiles.mapCollision(serverMapFiles.mapBytes(files))
  # Two boxes off the origin, of two types, one longer along x and one along y, so a box drawn at the origin, with x and y swapped, or
  # labelled with another box's type puts its sides or its label elsewhere.
  regions = serverMapFiles.readWater(serverMapFiles.waterBytes([
    {"name": "AWT_pond", "center": (20.0, -15.0, 0.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (12.0, 5.0, 5.0)},
    {"name": "ALV_pit", "center": (-20.0, 20.0, 0.0), "rotation": (0.0, 0.0, 0.0), "halfExtents": (5.0, 15.0, 5.0)},
  ]))
  water, lava = serverMapDrawing.regionColors[1], serverMapDrawing.regionColors[2]
  frame = serverMapDrawing.collisionFrame(collision, 400)
  assert frame.size == (400, 400)
  image = serverMapDrawing.drawCollision(collision, regions, frame)

  def at(picture, point, left=0, top=0):
    x, y = frame.pixel(point)
    return picture.getpixel((int(x) + left, int(y) + top))

  def sidesOf(region):
    """A point just inside the middle of each side of a box: north, south, west, east."""
    (x, y, _), (halfX, halfY, _) = region["position"], region["halfExtents"]
    return [(x + halfX - 0.25, y), (x - halfX + 0.25, y), (x, y + halfY - 0.25), (x, y - halfY + 0.25)]

  def labelColorsAbove(picture, region, left=0, top=0):
    """The region colors in the strip just above a box, where its number and type are written."""
    (x, y, _), (halfX, _, _) = region["position"], region["halfExtents"]
    column, row = frame.pixel((x + halfX, y))
    strip = numpy.asarray(picture)[int(row) + top - 22:int(row) + top - 2, int(column) + left - 30:int(column) + left + 30]
    return [color for color in (water, lava) if (strip == color).all(axis=2).any()]
  # The floor in the lowest height's color, the plateau over it in the highest's, both lit from the north-west; the margin bare.
  assert at(image, (35, 25)) == (45, 73, 104) and at(image, (-25, -35)) == (212, 211, 204)
  assert at(image, (-52, -52)) == serverMapDrawing.backgroundColor
  assert [at(image, point) for point in sidesOf(regions[0])] == [water] * 4 and [at(image, point) for point in sidesOf(regions[1])] == [lava] * 4
  assert labelColorsAbove(image, regions[0]) == [water] and labelColorsAbove(image, regions[1]) == [lava]
  # Each panel draws its own boxes; raised plateau triangles differ, in red on the third plan; the floor matches and stays faded.
  raised = collision.copy()
  raised[2:, :, 2] += 5
  sheet = serverMapDrawing.drawCollisionComparison(collision, regions[:1], raised, regions[1:], ("before", "after"), 400)
  assert sheet.size == (3 * 400 + 2 * serverMapDrawing.panelGap, 400 + serverMapDrawing.titleHeight)
  first, second, difference = [(index * (400 + serverMapDrawing.panelGap), serverMapDrawing.titleHeight) for index in range(3)]
  assert [at(sheet, point, *first) for point in sidesOf(regions[0])] == [water] * 4 and lava not in [at(sheet, point, *first) for point in sidesOf(regions[1])]
  assert [at(sheet, point, *second) for point in sidesOf(regions[1])] == [lava] * 4 and water not in [at(sheet, point, *second) for point in sidesOf(regions[0])]
  assert labelColorsAbove(sheet, regions[0], *first) == [water] and labelColorsAbove(sheet, regions[1], *second) == [lava]
  assert at(sheet, (-25, -35), *difference) == serverMapDrawing.differenceColor and at(sheet, (35, 25), *difference) not in (serverMapDrawing.differenceColor, (45, 73, 104))
