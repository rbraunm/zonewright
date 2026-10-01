"""Classic WLD character models (global<code>_chr.s3d): skeleton posing and texture extraction into a per-model cache."""
import json
import math
import struct

import numpy

import eqArchive
import eqWorldFile

characterCacheFormat = 1
armsDownDegrees = 70.0
upperArmMarkers = {"BIBICEPL": 1, "BIBICEPR": -1}


def quaternionMatrix(w, x, y, z):
  length = math.sqrt(w * w + x * x + y * y + z * z)
  w, x, y, z = w / length, x / length, y / length, z / length
  return numpy.array([
    [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
  ])


def trackFirstFrame(worldFile, trackInstanceReference):
  """A bone's local transform: frame 0 of its track, a quaternion and a translation in 1/256 units."""
  trackDefinition = worldFile.fragment(struct.unpack_from("<i", worldFile.fragment(trackInstanceReference, 0x13).body, 4)[0], 0x12)
  flags, frameCount = struct.unpack_from("<II", trackDefinition.body, 4)
  if frameCount < 1:
    raise ValueError(f"{worldFile.sourceName}: track '{trackDefinition.name}' has no frames")
  rotationW, rotationX, rotationY, rotationZ, shiftX, shiftY, shiftZ, shiftDenominator = struct.unpack_from("<8h", trackDefinition.body, 12)
  transform = numpy.eye(4)
  transform[:3, :3] = quaternionMatrix(rotationW, rotationX, rotationY, rotationZ)
  transform[:3, 3] = numpy.array([shiftX, shiftY, shiftZ]) / 256.0
  return transform


def readSkeleton(worldFile):
  skeletons = worldFile.fragmentsOfType(0x10)
  if len(skeletons) != 1:
    raise ValueError(f"{worldFile.sourceName}: expected one skeleton, found {len(skeletons)}")
  body = skeletons[0].body
  flags, dagCount, _ = struct.unpack_from("<IIi", body, 4)
  position = 16 + (12 if flags & 1 else 0) + (4 if flags & 2 else 0)
  dags = []
  for _ in range(dagCount):
    nameReference, _, trackReference, _, childCount = struct.unpack_from("<iIiiI", body, position)
    position += 20
    children = struct.unpack_from(f"<{childCount}I", body, position)
    position += 4 * childCount
    dags.append({"name": worldFile.lookupName(nameReference), "track": trackReference, "children": children})
  skinCount = struct.unpack_from("<I", body, position)[0]
  skins = struct.unpack_from(f"<{skinCount}i", body, position + 4)
  return dags, skins


def poseSkeleton(worldFile, dags):
  """World transform per bone from the bind pose, with the upper arms lowered so the figure stands rather than T-poses."""
  worldTransforms = [None] * len(dags)

  def walk(index, parentTransform):
    worldTransforms[index] = parentTransform @ trackFirstFrame(worldFile, dags[index]["track"])
    for child in dags[index]["children"]:
      walk(child, worldTransforms[index])

  walk(0, numpy.eye(4))
  lowered = []
  for index, dag in enumerate(dags):
    side = next((sign for marker, sign in upperArmMarkers.items() if marker in dag["name"]), None)
    if side is None:
      continue
    joint = worldTransforms[index][:3, 3].copy()
    angle = math.radians(-armsDownDegrees * side)
    rotation = numpy.eye(4)
    rotation[:3, :3] = numpy.array([[1, 0, 0], [0, math.cos(angle), -math.sin(angle)], [0, math.sin(angle), math.cos(angle)]])
    about = numpy.eye(4)
    about[:3, 3] = joint
    back = numpy.eye(4)
    back[:3, 3] = -joint
    adjustment = about @ rotation @ back
    for descendant in subtree(dags, index):
      worldTransforms[descendant] = adjustment @ worldTransforms[descendant]
    lowered.append(dag["name"])
  return worldTransforms, "armsLowered" if len(lowered) == len(upperArmMarkers) else "bind"


def subtree(dags, index):
  indices = [index]
  for child in dags[index]["children"]:
    indices += subtree(dags, child)
  return indices


def meshVertexPieces(worldFile, meshFragment):
  body = meshFragment.body
  vertexCount, uvCount, normalCount, colorCount, polygonCount, pieceCount = struct.unpack_from("<10H", body, 76)[:6]
  offset = 96 + vertexCount * 6 + uvCount * (4 if worldFile.isOldFormat else 8) + normalCount * 3 + colorCount * 4 + polygonCount * 8
  return numpy.frombuffer(body, dtype="<u2", count=pieceCount * 2, offset=offset).reshape(-1, 2)


def repairDDS(ddsBytes):
  """EQ's DDS headers declare mipmaps with a count of 0; set the count to the levels actually present so readers accept them."""
  data = bytearray(ddsBytes)
  if data[:4] != b"DDS " or struct.unpack_from("<I", data, 28)[0] != 0:
    return bytes(data)
  height, width = struct.unpack_from("<II", data, 12)
  blockBytes = 8 if data[84:88] == b"DXT1" else 16
  remaining = len(data) - 128
  levels = 0
  while remaining > 0 and width >= 1 and height >= 1:
    levelBytes = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * blockBytes
    if levelBytes > remaining:
      break
    remaining -= levelBytes
    levels += 1
    width, height = width // 2, height // 2
  if levels == 0:
    raise ValueError("DDS data is smaller than its top level")
  struct.pack_into("<I", data, 28, levels)
  return bytes(data)


def characterArchive(clientRoot, modelCode):
  path = clientRoot / f"global{modelCode.lower()}_chr.s3d"
  if not path.is_file():
    raise ValueError(f"No character archive {path.name} in {clientRoot} for model {modelCode}")
  return path


def buildCharacter(clientRoot, modelCode, cacheRoot):
  """Pose a character model and write its geometry (model.npz) and textures into cacheRoot/<code>, reusing a cache built from the same archive."""
  archivePath = characterArchive(clientRoot, modelCode)
  status = archivePath.stat()
  modelFolder = cacheRoot / modelCode.upper()
  stampPath = modelFolder / "source.json"
  stamp = {"cacheFormat": characterCacheFormat, "archive": archivePath.name, "size": status.st_size, "modifiedNanoseconds": status.st_mtime_ns, "armsDownDegrees": armsDownDegrees}
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored[key] for key in stamp} == stamp:
      return modelFolder, stored
  archive = eqArchive.EQArchive(archivePath)
  wldNames = [name for name in archive.names() if name.endswith(".wld")]
  if len(wldNames) != 1:
    raise ValueError(f"{archivePath.name}: expected one .wld, found {wldNames}")
  worldFile = eqWorldFile.WorldFile(archive.read(wldNames[0]), wldNames[0])
  dags, skins = readSkeleton(worldFile)
  worldTransforms, pose = poseSkeleton(worldFile, dags)
  vertexChunks, triangleChunks, uvChunks, textureNames, cutouts = [], [], [], [], []
  vertexOffset = 0
  for skinReference in skins:
    meshFragment = worldFile.fragment(struct.unpack_from("<i", worldFile.fragment(skinReference, 0x2D).body, 4)[0], 0x36)
    mesh = worldFile.mesh(meshFragment)
    if mesh["uvs"] is None:
      raise ValueError(f"{archivePath.name}: mesh '{mesh['name']}' has no per-vertex UVs")
    posed = numpy.empty_like(mesh["vertices"])
    start = 0
    for count, bone in meshVertexPieces(worldFile, meshFragment):
      transform = worldTransforms[bone]
      posed[start:start + count] = mesh["vertices"][start:start + count] @ transform[:3, :3].T + transform[:3, 3]
      start += count
    if start != len(posed):
      raise ValueError(f"{archivePath.name}: bone pieces of '{mesh['name']}' cover {start} of {len(posed)} vertices")
    materials = mesh["materials"]
    visible = numpy.array([materials[index]["renderMethod"] != eqWorldFile.invisibleRenderMethod and bool(materials[index]["textureNames"]) for index in mesh["triangleMaterials"]], dtype=bool)
    vertexChunks.append(posed)
    triangleChunks.append(mesh["triangles"][visible] + vertexOffset)
    uvChunks.append(mesh["uvs"])
    for index in mesh["triangleMaterials"][visible]:
      textureNames.append(materials[index]["textureNames"][0].removesuffix("_layer"))
      cutouts.append(materials[index]["renderMethod"] & 0xFF == 0x13)
    vertexOffset += len(posed)
  vertices = numpy.concatenate(vertexChunks)
  modelFolder.mkdir(parents=True, exist_ok=True)
  for textureName in sorted(set(textureNames)):
    if textureName not in archive.entries:
      raise ValueError(f"{archivePath.name}: texture '{textureName}' is not in the archive")
    textureBytes = archive.read(textureName)
    (modelFolder / textureName).write_bytes(repairDDS(textureBytes) if textureName.endswith(".dds") else textureBytes)
  numpy.savez(modelFolder / "model.npz", vertices=vertices, triangles=numpy.concatenate(triangleChunks), uvs=numpy.concatenate(uvChunks), textureNames=numpy.array(textureNames), cutouts=numpy.array(cutouts))
  details = stamp | {
    "modelCode": modelCode.upper(),
    "pose": pose,
    "height": round(float(vertices[:, 2].max() - vertices[:, 2].min()), 3),
    "footHeight": round(float(vertices[:, 2].min()), 3),
    "facing": "+X",
  }
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return modelFolder, details
