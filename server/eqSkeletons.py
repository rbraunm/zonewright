"""WLD skeletal actors: bone transforms from track frames, and skinned or attached meshes placed by their bones."""
import math
import struct

import numpy

skinListFlag = 0x200
shortFrameFlag = 0x8
frameBytes = 16
frameDelayFlag = 0x1
# EQGraphicsDX9.dll 0x1001b190 decodes a frame's eight shorts: rotation w, x, y, z over 16384 (normalized here), translation x, y, z
# over 256, and an unsigned uniform scale over 256.
translationUnits = 256.0
scaleUnits = 256.0


def quaternionMatrix(w, x, y, z):
  length = math.sqrt(w * w + x * x + y * y + z * z)
  w, x, y, z = w / length, x / length, y / length, z / length
  return numpy.array([
    [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
  ])


def trackInstance(worldFile, reference):
  """A track instance (0x13): its definition (0x12) and, when it sets one, its milliseconds per frame."""
  instance = worldFile.fragment(reference, 0x13)
  definitionReference, flags = struct.unpack_from("<iI", instance.body, 4)
  millisecondsPerFrame = struct.unpack_from("<I", instance.body, 12)[0] if flags & frameDelayFlag else None
  return worldFile.fragment(definitionReference, 0x12), millisecondsPerFrame


def trackFrames(worldFile, definition):
  """A track definition's frames as rows of eight shorts."""
  flags, frameCount = struct.unpack_from("<II", definition.body, 4)
  if not flags & shortFrameFlag or len(definition.body) != 12 + frameBytes * frameCount:
    raise ValueError(f"{worldFile.sourceName}: track '{definition.name}' (flags {flags:#x}, {frameCount} frames) is not in the short frame layout")
  if frameCount < 1:
    raise ValueError(f"{worldFile.sourceName}: track '{definition.name}' has no frames")
  return numpy.frombuffer(definition.body, dtype="<i2", count=frameCount * 8, offset=12).reshape(frameCount, 8)


def frameTransform(values):
  transform = numpy.eye(4)
  scale = (int(values[7]) & 0xFFFF) / scaleUnits
  transform[:3, :3] = quaternionMatrix(*(float(value) for value in values[:4])) * scale
  transform[:3, 3] = values[4:7].astype(numpy.float64) / translationUnits
  return transform


def bindTransforms(worldFile, dags):
  """Each bone's local transform from frame 0 of its own track."""
  return [frameTransform(trackFrames(worldFile, trackInstance(worldFile, dag["track"])[0])[0]) for dag in dags]


def readSkeleton(worldFile, skeletonFragment):
  """Bones (dags) with their tracks, children, and attachment; and, when flag 0x200 is set, the skinned meshes' references (0x2D)."""
  body = skeletonFragment.body
  flags, dagCount, _ = struct.unpack_from("<IIi", body, 4)
  position = 16 + (12 if flags & 1 else 0) + (4 if flags & 2 else 0)
  dags = []
  for _ in range(dagCount):
    nameReference, _, trackReference, attachmentReference, childCount = struct.unpack_from("<iIiiI", body, position)
    position += 20
    children = struct.unpack_from(f"<{childCount}I", body, position)
    position += 4 * childCount
    dags.append({"name": worldFile.lookupName(nameReference), "track": trackReference, "attachment": attachmentReference, "children": children})
  skins = ()
  if flags & skinListFlag:
    skinCount = struct.unpack_from("<I", body, position)[0]
    skins = struct.unpack_from(f"<{skinCount}i", body, position + 4)
    position += 4 + 8 * skinCount
  if position != len(body):
    raise ValueError(f"{worldFile.sourceName}: skeleton '{skeletonFragment.name}' used {position} of {len(body)} bytes")
  return dags, skins


def boneAttachments(worldFile, dags):
  """Meshes attached rigidly to a bone (a 0x2D reference on the dag), as (bone, mesh fragment); and the count of particle clouds (0x34), which are not drawn."""
  attached, particleClouds = [], 0
  for index, dag in enumerate(dags):
    if dag["attachment"] == 0:
      continue
    # A negative reference names a fragment; a particle cloud (_PCD) named here may be defined in another file.
    if dag["attachment"] < 0 and worldFile.lookupName(dag["attachment"]).endswith("_PCD") and not any(fragment.name == worldFile.lookupName(dag["attachment"]) for fragment in worldFile.fragments):
      particleClouds += 1
      continue
    reference = worldFile.referenced(dag["attachment"])
    if reference.fragmentType == 0x34:
      particleClouds += 1
    elif reference.fragmentType == 0x2D:
      attached.append((index, worldFile.fragments[struct.unpack_from("<i", reference.body, 4)[0] - 1]))
    else:
      raise ValueError(f"{worldFile.sourceName}: bone '{dag['name']}' attaches fragment type {reference.fragmentType:#x}")
  return attached, particleClouds


def poseSkeleton(dags, localTransforms):
  """World transform per bone: its local transform under its parent's."""
  worldTransforms = [None] * len(dags)

  def walk(index, parentTransform):
    worldTransforms[index] = parentTransform @ localTransforms[index]
    for child in dags[index]["children"]:
      walk(child, worldTransforms[index])

  walk(0, numpy.eye(4))
  return worldTransforms


def meshVertexPieces(worldFile, meshFragment):
  body = meshFragment.body
  vertexCount, uvCount, normalCount, colorCount, polygonCount, pieceCount = struct.unpack_from("<10H", body, 76)[:6]
  offset = 96 + vertexCount * 6 + uvCount * (4 if worldFile.isOldFormat else 8) + normalCount * 3 + colorCount * 4 + polygonCount * 8
  return numpy.frombuffer(body, dtype="<u2", count=pieceCount * 2, offset=offset).reshape(-1, 2)


def skinMeshes(worldFile, skins):
  """The mesh fragments a skeleton's skin references (0x2D) point at."""
  return [worldFile.fragments[struct.unpack_from("<i", worldFile.fragment(reference, 0x2D).body, 4)[0] - 1] for reference in skins]


def posedSkeleton(worldFile, skeletonFragment, skinned, meshArrays, localTransforms=None):
  """Skinned meshes (0x36 fragments rigged to this skeleton) and the meshes attached to its bones, posed by its bones at localTransforms
  (the bind pose when None); meshArrays turns one posed mesh into its part. Also returns each bone's posed transform by name."""
  dags, _ = readSkeleton(worldFile, skeletonFragment)
  worldTransforms = poseSkeleton(dags, bindTransforms(worldFile, dags) if localTransforms is None else localTransforms)
  parts = []
  for meshFragment in skinned:
    mesh = worldFile.mesh(meshFragment)
    posed = numpy.empty_like(mesh["vertices"])
    start = 0
    for count, bone in meshVertexPieces(worldFile, meshFragment):
      if bone >= len(worldTransforms):
        raise ValueError(f"{worldFile.sourceName}: mesh '{mesh['name']}' uses bone {bone} of a {len(worldTransforms)}-bone skeleton")
      transform = worldTransforms[bone]
      posed[start:start + count] = mesh["vertices"][start:start + count] @ transform[:3, :3].T + transform[:3, 3]
      start += count
    if start != len(posed):
      raise ValueError(f"{worldFile.sourceName}: bone pieces of '{mesh['name']}' cover {start} of {len(posed)} vertices")
    parts.append(meshArrays(mesh | {"vertices": posed}))
  attached, particleClouds = boneAttachments(worldFile, dags)
  for bone, meshFragment in attached:
    mesh = worldFile.mesh(worldFile.fragment(meshFragment.index, 0x36))
    transform = worldTransforms[bone]
    parts.append(meshArrays(mesh | {"vertices": mesh["vertices"] @ transform[:3, :3].T + transform[:3, 3]}))
  return parts, particleClouds, {dag["name"]: transform for dag, transform in zip(dags, worldTransforms)}
