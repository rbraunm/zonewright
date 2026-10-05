"""Peridot's server files for the nav tests, and minimal private readers of them. They stand in for S1's tests/serverReference.py and
server/serverMapFiles.py (readMap, mapCollision, readWater, readNav) until that slice merges, and then go."""
import hashlib
import math
import os
import struct
import zlib
from pathlib import Path

import numpy

referenceRoot = Path(os.environ["LOCALAPPDATA"]) / "zonewrightTests" / "serverReference" / "fbd3b191"
referenceFiles = {
  "base/highpasshold.map": (1347982, "9d087e36a5c1b654776e95752f8bba45e2a606c3d485a82db5da5416ca494273"),
  "water/highpasshold.wtr": (382, "82cf3026b92ba67c5f64f91fd16dc1ad4f0264abc881eafe707573325012727d"),
  "nav/highpasshold.nav": (439488, "1fa5871b4296620068f444b521b9b6aeae8e58070ce07df1b6413889443d8432"),
  "base/thulehouse2.map": (4224747, "6443dcc7574173a69bbabaf244a297200991c2ff5835acfa0be1e7c764991d29"),
  "water/thulehouse2.wtr": (434, "4fbc62b68ab4bd9c65ac5b5be663d8bf59d5c78ed897835b427f81dac1f26f95"),
  "nav/thulehouse2.nav": (165838, "8b3f95e7c37be5621bc5b78a11faca586e2e1094a49e7c21ebb4511507819da0"),
}
tileHeaderFormat = "<4i I 10i 3f 3f 3f f"
tileHeaderFields = (
  "magic", "version", "x", "y", "layer", "userId", "polyCount", "vertCount", "maxLinkCount", "detailMeshCount", "detailVertCount",
  "detailTriCount", "bvNodeCount", "offMeshConCount", "offMeshBase", "walkableHeight", "walkableRadius", "walkableClimb",
)
polygonType = numpy.dtype([("firstLink", "<u4"), ("verts", "<u2", 6), ("neis", "<u2", 6), ("flags", "<u2"), ("vertCount", "u1"), ("areaAndType", "u1")])
detailMeshType = numpy.dtype([("vertBase", "<u4"), ("triBase", "<u4"), ("vertCount", "u1"), ("triCount", "u1"), ("pad", "u1", 2)])
linkBytes = 12


def referenceBytes(path):
  """A reference file's bytes, failing (never skipping) when it is missing or not the pinned file."""
  size, sha256 = referenceFiles[path]
  data = (referenceRoot / path).read_bytes()
  actual = hashlib.sha256(data).hexdigest()
  if len(data) != size or actual != sha256:
    raise AssertionError(f"{referenceRoot / path} is {len(data)} bytes with SHA-256 {actual}, not the pinned {size} bytes {sha256}")
  return data


def rotated(points, rx, ry, rz):
  x, y, z = points[:, 0].copy(), points[:, 1].copy(), points[:, 2].copy()
  y, z = math.cos(rx) * y - math.sin(rx) * z, math.sin(rx) * y + math.cos(rx) * z
  x, z = math.cos(ry) * x + math.sin(ry) * z, -math.sin(ry) * x + math.cos(ry) * z
  x, y = math.cos(rz) * x - math.sin(rz) * y, math.sin(rz) * x + math.cos(rz) * y
  return numpy.stack([x, y, z], axis=1)


def mapCollision(mapBytes):
  """A V2 .map's collidable triangles in server axes, in the server's order: the collidable list, then each placement's visible polygons
  turned about x, y, then z, scaled, moved, and swapped in x and y."""
  version, compressedSize, inflatedSize = struct.unpack_from("<3I", mapBytes, 0)
  assert version == 0x02000000 and 12 + compressedSize == len(mapBytes)
  raw = zlib.decompress(mapBytes[12:])
  assert len(raw) == inflatedSize
  counts = struct.unpack_from("<9I", raw, 0)
  vertexCount, indexCount, otherVertexCount, otherIndexCount, modelCount, placementCount, groupCount, terrainTiles, _ = counts
  assert groupCount == 0 and terrainTiles == 0
  offset = 40
  vertices = numpy.frombuffer(raw, "<f4", vertexCount * 3, offset).reshape(-1, 3)
  offset += vertexCount * 12
  indices = numpy.frombuffer(raw, "<u4", indexCount, offset)
  offset += indexCount * 4 + otherVertexCount * 12 + otherIndexCount * 4
  triangles = [vertices[indices].reshape(-1, 3, 3)]
  models = {}
  for _ in range(modelCount):
    end = raw.index(b"\0", offset)
    name = raw[offset:end].decode("latin-1")
    modelVertexCount, modelPolygonCount = struct.unpack_from("<2I", raw, end + 1)
    offset = end + 9
    modelVertices = numpy.frombuffer(raw, "<f4", modelVertexCount * 3, offset).reshape(-1, 3)
    offset += modelVertexCount * 12
    polygons = numpy.frombuffer(raw, numpy.dtype([("v", "<u4", 3), ("vis", "u1")]), modelPolygonCount, offset)
    offset += modelPolygonCount * 13
    models[name] = (modelVertices, polygons)
  for _ in range(placementCount):
    end = raw.index(b"\0", offset)
    name = raw[offset:end].decode("latin-1")
    x, y, z, rx, ry, rz, sx, sy, sz = struct.unpack_from("<9f", raw, end + 1)
    offset = end + 37
    modelVertices, polygons = models[name]
    visible = polygons[polygons["vis"] != 0]
    corners = modelVertices[visible["v"].reshape(-1)].astype(numpy.float64)
    corners = rotated(corners, rx, ry, rz).astype(numpy.float32)
    corners = corners * numpy.array([sx, sy, sz], dtype=numpy.float32) + numpy.array([x, y, z], dtype=numpy.float32)
    triangles.append(corners[:, [1, 0, 2]].reshape(-1, 3, 3))
  assert offset == len(raw), f"the .map ends at {len(raw)}, read to {offset}"
  return numpy.concatenate(triangles).astype(numpy.float32)


def waterRecords(waterBytes):
  """A V2 .wtr's records in zone axes."""
  assert waterBytes[:10] == b"EQEMUWATER" and struct.unpack_from("<I", waterBytes, 10)[0] == 2
  count = struct.unpack_from("<I", waterBytes, 14)[0]
  assert len(waterBytes) == 18 + 52 * count
  records = []
  for index in range(count):
    values = struct.unpack_from("<I12f", waterBytes, 18 + 52 * index)
    records.append({"type": values[0], "position": values[1:4], "rotation": values[4:7], "scale": values[7:10], "halfExtents": values[10:13]})
  return records


def align4(value):
  return (value + 3) & ~3


def navTiles(navFile):
  """A .nav's raw params and its tiles keyed by their header's (x, y, layer): the header, the raw sections, and the decoded polygons."""
  assert navFile[:9] == b"EQNAVMESH"
  raw = zlib.decompress(navFile[21:])
  tileCount = struct.unpack_from("<I", raw, 0)[0]
  params = raw[4:32]
  offset = 32
  tiles = {}
  order = []
  for _ in range(tileCount):
    reference, size = struct.unpack_from("<Ii", raw, offset)
    data = raw[offset + 8:offset + 8 + size]
    offset += 8 + size
    values = struct.unpack_from(tileHeaderFormat, data, 0)
    header = dict(zip(tileHeaderFields, values[:18])) | {"bmin": values[18:21], "bmax": values[21:24], "bvQuantFactor": values[24]}
    sections = {}
    position = align4(struct.calcsize(tileHeaderFormat))
    for name, length in (
      ("verts", header["vertCount"] * 12), ("polys", header["polyCount"] * polygonType.itemsize), ("links", header["maxLinkCount"] * linkBytes),
      ("detailMeshes", header["detailMeshCount"] * detailMeshType.itemsize), ("detailVerts", header["detailVertCount"] * 12),
      ("detailTris", header["detailTriCount"] * 4), ("bvTree", header["bvNodeCount"] * 16), ("offMesh", header["offMeshConCount"] * 36),
    ):
      sections[name] = data[position:position + length]
      position += align4(length)
    assert position == size, f"tile ({header['x']}, {header['y']}) decodes to {position} of {size} bytes"
    key = (header["x"], header["y"], header["layer"])
    tiles[key] = {
      "reference": reference, "header": header, "sections": sections,
      "verts": numpy.frombuffer(sections["verts"], "<f4").reshape(-1, 3),
      "polys": numpy.frombuffer(sections["polys"], polygonType),
      "detailMeshes": numpy.frombuffer(sections["detailMeshes"], detailMeshType),
      "detailVerts": numpy.frombuffer(sections["detailVerts"], "<f4").reshape(-1, 3),
      "detailTris": numpy.frombuffer(sections["detailTris"], "u1").reshape(-1, 4),
    }
    order.append(key)
  assert offset == len(raw)
  return {"params": params, "tiles": tiles, "order": order}


def polygonOutlines(tile):
  """Each polygon's corners in zone plan axes (x, y) and its area."""
  outlines = []
  for polygon in tile["polys"]:
    corners = tile["verts"][polygon["verts"][:polygon["vertCount"]]]
    outlines.append({"points": [(float(corner[2]), float(corner[0])) for corner in corners], "area": int(polygon["areaAndType"] & 0x3F)})
  return outlines


def linkSets(nav):
  """Each polygon's links as a set of (target tile key, target polygon, edge, side, bmin, bmax), by (tile key, polygon): the mesh's
  connectivity, free of the link pool's order and of tile references, which both follow the order tiles were added."""
  maxTiles = struct.unpack_from("<i", nav["params"], 20)[0]
  tileBits = maxTiles.bit_length() - 1
  polygonBits = 22 - tileBits
  keyOfSlot = {(tile["reference"] >> polygonBits) & ((1 << tileBits) - 1): key for key, tile in nav["tiles"].items()}
  linkType = numpy.dtype([("ref", "<u4"), ("next", "<u4"), ("edge", "u1"), ("side", "u1"), ("bmin", "u1"), ("bmax", "u1")])
  result = {}
  for key, tile in nav["tiles"].items():
    links = numpy.frombuffer(tile["sections"]["links"], linkType)
    for index, polygon in enumerate(tile["polys"]):
      found = set()
      link = int(polygon["firstLink"])
      while link != 0xFFFFFFFF:
        entry = links[link]
        reference = int(entry["ref"])
        target = keyOfSlot[(reference >> polygonBits) & ((1 << tileBits) - 1)]
        found.add((target, reference & ((1 << polygonBits) - 1), int(entry["edge"]), int(entry["side"]), int(entry["bmin"]), int(entry["bmax"])))
        link = int(entry["next"])
      result[(key, index)] = found
  return result


def detailOf(tile, index):
  """A polygon's detail mesh: its own detail vertices and its triangles, as bytes."""
  mesh = tile["detailMeshes"][index]
  vertices = tile["detailVerts"][mesh["vertBase"]:mesh["vertBase"] + mesh["vertCount"]]
  triangles = tile["detailTris"][mesh["triBase"]:mesh["triBase"] + mesh["triCount"]]
  return vertices.tobytes() + triangles.tobytes()
