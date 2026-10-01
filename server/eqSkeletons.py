"""WLD skeletal actors: bone transforms from each track's first frame, and skinned meshes placed by their bones."""
import math
import struct

import numpy

import eqWorldFile

armsDownDegrees = 70.0
upperArmMarkers = {"BIBICEPL": 1, "BIBICEPR": -1}
skinListFlag = 0x200


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
  _, frameCount = struct.unpack_from("<II", trackDefinition.body, 4)
  if frameCount < 1:
    raise ValueError(f"{worldFile.sourceName}: track '{trackDefinition.name}' has no frames")
  rotationW, rotationX, rotationY, rotationZ, shiftX, shiftY, shiftZ, _ = struct.unpack_from("<8h", trackDefinition.body, 12)
  transform = numpy.eye(4)
  transform[:3, :3] = quaternionMatrix(rotationW, rotationX, rotationY, rotationZ)
  transform[:3, 3] = numpy.array([shiftX, shiftY, shiftZ]) / 256.0
  return transform


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


def subtree(dags, index):
  indices = [index]
  for child in dags[index]["children"]:
    indices += subtree(dags, child)
  return indices


def poseSkeleton(worldFile, dags):
  """World transform per bone from the bind pose, with Luclin upper arms lowered so a figure stands rather than T-poses."""
  worldTransforms = [None] * len(dags)

  def walk(index, parentTransform):
    worldTransforms[index] = parentTransform @ trackFirstFrame(worldFile, dags[index]["track"])
    for child in dags[index]["children"]:
      walk(child, worldTransforms[index])

  walk(0, numpy.eye(4))
  lowered = 0
  for index, dag in enumerate(dags):
    side = next((sign for marker, sign in upperArmMarkers.items() if marker in dag["name"]), None)
    if side is None:
      continue
    joint = worldTransforms[index][:3, 3].copy()
    angle = math.radians(-armsDownDegrees * side)
    adjustment = numpy.eye(4)
    adjustment[:3, :3] = numpy.array([[1, 0, 0], [0, math.cos(angle), -math.sin(angle)], [0, math.sin(angle), math.cos(angle)]])
    adjustment[:3, 3] = joint - adjustment[:3, :3] @ joint
    for descendant in subtree(dags, index):
      worldTransforms[descendant] = adjustment @ worldTransforms[descendant]
    lowered += 1
  return worldTransforms, "armsLowered" if lowered == len(upperArmMarkers) else "bind"


def meshVertexPieces(worldFile, meshFragment):
  body = meshFragment.body
  vertexCount, uvCount, normalCount, colorCount, polygonCount, pieceCount = struct.unpack_from("<10H", body, 76)[:6]
  offset = 96 + vertexCount * 6 + uvCount * (4 if worldFile.isOldFormat else 8) + normalCount * 3 + colorCount * 4 + polygonCount * 8
  return numpy.frombuffer(body, dtype="<u2", count=pieceCount * 2, offset=offset).reshape(-1, 2)


def skinMeshes(worldFile, skins):
  """The mesh fragments a skeleton's skin references (0x2D) point at."""
  return [worldFile.fragments[struct.unpack_from("<i", worldFile.fragment(reference, 0x2D).body, 4)[0] - 1] for reference in skins]


def posedSkeleton(worldFile, skeletonFragment, skinned, meshArrays):
  """Skinned meshes (0x36 fragments rigged to this skeleton) and the meshes attached to its bones, posed by its bones; meshArrays turns one posed mesh into its part."""
  dags, _ = readSkeleton(worldFile, skeletonFragment)
  worldTransforms, pose = poseSkeleton(worldFile, dags)
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
  return parts, pose, particleClouds
