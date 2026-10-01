"""EQG skeletons posed and skinned as EQGraphicsDX9.dll builds them: bone matrices from .mod/.mds bones and .ani keyframes, and vertices blended by their bone weights."""
import numpy

rootBoneName = "ROOT_BONE"


def boneParents(bones, sourceName):
  """Each bone's parent index (-1 for a root), from the first-child and next-sibling links, and an order with parents first. Roots
  are the bones no link reaches, not always bone 0; every bone must be reached exactly once."""
  boneCount = len(bones["names"])
  linked = {int(bones["firstChild"][bone]) for bone in range(boneCount) if bones["childCount"][bone]} | {int(bone) for bone in bones["next"]}
  parents = numpy.full(boneCount, -2, dtype=numpy.int64)
  order = []
  pending = [(root, -1) for root in range(boneCount) if root not in linked]
  while pending:
    sibling, parent = pending.pop()
    while sibling != -1:
      if not 0 <= sibling < boneCount or parents[sibling] != -2:
        raise ValueError(f"{sourceName}: bone links reach bone {sibling} twice or outside its {boneCount} bones")
      parents[sibling] = parent
      order.append(sibling)
      if bones["childCount"][sibling]:
        pending.append((int(bones["firstChild"][sibling]), sibling))
      sibling = int(bones["next"][sibling])
  if len(order) != boneCount:
    raise ValueError(f"{sourceName}: bone links reach {len(order)} of its {boneCount} bones")
  return parents, order


def localMatrices(positions, rotations, scales):
  """Row-vector matrices scale, rotation, then translation. The DLL transposes D3DXMatrixRotationQuaternion's matrix (0x1003efc8), so
  the stored quaternion (x, y, z, w) rotates a row vector by the standard column-vector matrix."""
  x, y, z, w = (rotations[:, index] for index in range(4))
  rotation = numpy.stack([
    numpy.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], axis=1),
    numpy.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], axis=1),
    numpy.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], axis=1),
  ], axis=1)
  matrices = numpy.zeros((len(positions), 4, 4))
  matrices[:, :3, :3] = scales[:, :, None] * rotation
  matrices[:, 3, :3] = positions
  matrices[:, 3, 3] = 1
  return matrices


def worldMatrices(locals_, parents, order):
  worlds = numpy.empty_like(locals_)
  for bone in order:
    worlds[bone] = locals_[bone] if parents[bone] == -1 else locals_[bone] @ worlds[parents[bone]]
  return worlds


def bindLocals(bones):
  return localMatrices(bones["position"], bones["rotation"], bones["scale"])


def animatedLocals(bones, tracks, frame, rootDrop):
  """Bone local matrices at one frame of an animation: a bone's track key at that frame (a one-key track holds still), or its bind
  transform when the animation has no track for it. rootDrop lowers ROOT_BONE's translation, as the DLL lowers its keys by the
  model's moddat.ini ROffset (0x1003cd5b)."""
  positions, rotations, scales = bones["position"].copy(), bones["rotation"].copy(), bones["scale"].copy()
  for index, name in enumerate(bones["names"]):
    frames = tracks.get(name)
    if frames is None:
      continue
    key = frames[frame if len(frames) > 1 else 0]
    positions[index], rotations[index], scales[index] = key["position"], key["rotation"], key["scale"]
    if name == rootBoneName:
      positions[index, 2] -= rootDrop
  return localMatrices(positions, rotations, scales)


def skinVertices(vertices, weights, skinMatrices, sourceName):
  """Each vertex blended by its weighted bones: the sum over its influences of weight times the vertex through bind-inverse-then-pose.
  Also returns which vertices have no weights, which the caller must not draw."""
  counts = weights["count"].astype(numpy.int64)
  if (counts > 4).any():
    raise ValueError(f"{sourceName}: {int((counts > 4).sum())} vertices have more than 4 weights")
  bones = weights["influences"]["bone"].astype(numpy.int64)
  usedInfluences = numpy.arange(4)[None, :] < counts[:, None]
  if ((bones < 0) | (bones >= len(skinMatrices)))[usedInfluences].any():
    raise ValueError(f"{sourceName}: weights name bones outside its {len(skinMatrices)} bones")
  homogeneous = numpy.concatenate([vertices, numpy.ones((len(vertices), 1))], axis=1)
  skinned = numpy.zeros((len(vertices), 4))
  for influence in range(4):
    used = influence < counts
    weight = numpy.where(used, weights["influences"]["weight"][:, influence], 0.0)
    skinned += weight[:, None] * numpy.einsum("ni,nij->nj", homogeneous, skinMatrices[numpy.where(used, bones[:, influence], 0)])
  return skinned[:, :3], counts == 0


def skinMatrices(bindWorlds, poseWorlds):
  return numpy.linalg.inv(bindWorlds) @ poseWorlds
