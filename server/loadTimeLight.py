"""The vertex light the RoF2 client gives a classic zone's placed static object that carries no colors of its own, computed once the zone
has loaded (EQGraphicsDX9.dll 0x100530f0): RGB from the zone's point lights (0x10010260) and, in alpha, the share of scene light of
the floor a drop under the object lands on (0x1003a4c0). docs/clientRendering.md, Placements and their vertex light, gives the rules
and how they were checked against MQPeridotEmu's RoF2 dumps."""
import math

import numpy

import eqWorldFile

# Single-precision constants the DLL holds: the drop's sphere (0x10131c1c, handed to the sweep at 0x1003ad80), a third (0x101338cc),
# the least share (0x1013377c), the lights' scale (0x10132e6c), and the shortest normal it normalizes (0x10131c14).
sweepRadius = 0.5
floorThird = 0.33333298563957214
leastShare = 0.10000000149011612
lightScale = 0.05000000074505806
shortestNormal = 1.1920928955078125e-07
gridCell = 32.0
cellBits = 21
# The client's object builder reads a normal byte through a 32-entry table (0x100c1c10): (byte >> 3) & 31 gives k / 15 for 0-15 and
# (k - 31) / 15 for 16-31.
normalSteps = numpy.array([k / 15 for k in range(16)] + [(k - 31) / 15 for k in range(16, 32)])


def chopped(value):
  """value as a single-precision float rounded toward zero, as the client's FPU rounds while it computes the share."""
  single = numpy.float32(value)
  if abs(float(single)) > abs(value):
    single = numpy.nextafter(single, numpy.float32(0))
  return single


def dropSweeps(center, radius):
  """The drop's eight downward sweeps in the order the client tries them, each (x, y, top, bottom) of the sphere's center: from the
  bounding sphere's top to below its bottom, from above it to its top, from its bottom far down, from high above to its top, then high
  to far down 10 units off along x and y."""
  x, y, z = center
  sideways = [(x + 10, y), (x - 10, y), (x, y + 10), (x, y - 10)]
  return [(x, y, z + radius, z - radius - 20), (x, y, z + radius + 20, z + radius), (x, y, z - radius, z - radius - 400), (x, y, z + radius + 100, z + radius)] + [
    (sideX, sideY, z + radius + 100, z - radius - 400) for sideX, sideY in sideways
  ]


def sphereContactHeights(x, y, corners):
  """For each triangle (corners, n x 3 x 3), the highest height at which a sphere of sweepRadius moving straight down through (x, y)
  touches it, or -inf: on its face, along an edge, or at a corner."""
  a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
  best = numpy.full(len(corners), -numpy.inf)
  normal = numpy.cross(b - a, c - a)
  normal /= numpy.maximum(numpy.linalg.norm(normal, axis=1, keepdims=True), 1e-30)
  with numpy.errstate(divide="ignore", invalid="ignore"):
    faceHeight = a[:, 2] - (normal[:, 0] * (x - a[:, 0]) + normal[:, 1] * (y - a[:, 1])) / normal[:, 2] + sweepRadius / normal[:, 2]
    touchX, touchY = x - sweepRadius * normal[:, 0], y - sweepRadius * normal[:, 1]
    across, along = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
    denominator = across[:, 0] * along[:, 1] - along[:, 0] * across[:, 1]
    u = ((touchX - a[:, 0]) * along[:, 1] - along[:, 0] * (touchY - a[:, 1])) / denominator
    v = (across[:, 0] * (touchY - a[:, 1]) - (touchX - a[:, 0]) * across[:, 1]) / denominator
  onFace = (normal[:, 2] > 1e-6) & (numpy.abs(denominator) > 1e-12) & (u >= 0) & (v >= 0) & (u + v <= 1)
  best = numpy.where(onFace, faceHeight, best)
  for start, end in ((a, b), (b, c), (c, a)):
    edge = end - start
    lengthSquared = (edge ** 2).sum(1)
    offsetX, offsetY = x - start[:, 0], y - start[:, 1]
    projected = offsetX * edge[:, 0] + offsetY * edge[:, 1]
    with numpy.errstate(divide="ignore", invalid="ignore"):
      quadratic = (edge[:, 0] ** 2 + edge[:, 1] ** 2) / lengthSquared
      linear = -2 * edge[:, 2] * projected / lengthSquared
      constant = offsetX ** 2 + offsetY ** 2 - projected ** 2 / lengthSquared - sweepRadius ** 2
      discriminant = linear ** 2 - 4 * quadratic * constant
      rise = (-linear + numpy.sqrt(discriminant)) / (2 * quadratic)
      position = (projected + rise * edge[:, 2]) / lengthSquared
    onEdge = (quadratic > 1e-9) & (discriminant >= 0) & (position >= 0) & (position <= 1)
    best = numpy.where(onEdge, numpy.maximum(best, start[:, 2] + rise), best)
  for corner in (a, b, c):
    horizontal = (x - corner[:, 0]) ** 2 + (y - corner[:, 1]) ** 2
    with numpy.errstate(invalid="ignore"):
      cornerHeight = corner[:, 2] + numpy.sqrt(sweepRadius ** 2 - horizontal)
    best = numpy.where(horizontal <= sweepRadius ** 2, numpy.maximum(best, cornerHeight), best)
  return best


class ShareFloors:
  """The zone polygons a drop lands on, each with the sum of its corners' shares of scene light (the alpha bytes, 0x1001fa60): those
  solid to players, drawn with a material that is not invisible, and facing up. Measured against the dumps: sweeps pass through
  passable, invisible (render method 0), and downward-facing polygons; the step that leaves them out is not traced."""

  def __init__(self, meshes, colorlessAlpha):
    cornerChunks, sumChunks = [], []
    for mesh in meshes:
      invisible = numpy.array([material["renderMethod"] == eqWorldFile.invisibleRenderMethod for material in mesh["materials"]], dtype=bool)
      corners = mesh["vertices"][mesh["triangles"]]
      facesUp = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])[:, 2] > 0
      keep = ~mesh["isPassable"] & ~invisible[mesh["triangleMaterials"]] & facesUp
      alphas = mesh["colors"][:, 3].astype(numpy.int64) if mesh["colors"] is not None else numpy.full(len(mesh["vertices"]), colorlessAlpha)
      cornerChunks.append(corners[keep])
      sumChunks.append(alphas[mesh["triangles"][keep]].sum(1))
    self.corners = numpy.concatenate(cornerChunks) if cornerChunks else numpy.zeros((0, 3, 3))
    self.alphaSums = numpy.concatenate(sumChunks) if sumChunks else numpy.zeros(0, dtype=numpy.int64)
    low = numpy.floor(self.corners[:, :, :2].min(1) / gridCell).astype(numpy.int64)
    spans = numpy.floor(self.corners[:, :, :2].max(1) / gridCell).astype(numpy.int64) - low + 1
    counts = spans[:, 0] * spans[:, 1]
    owners = numpy.repeat(numpy.arange(len(self.corners)), counts)
    steps = numpy.arange(counts.sum()) - numpy.repeat(numpy.cumsum(counts) - counts, counts)
    keys = self.cellKey(low[owners, 0] + steps % spans[owners, 0], low[owners, 1] + steps // spans[owners, 0])
    order = numpy.argsort(keys, kind="stable")
    self.cellKeys, self.cellTriangles = keys[order], owners[order]

  @staticmethod
  def cellKey(cellX, cellY):
    return ((numpy.asarray(cellX, dtype=numpy.int64) + (1 << (cellBits - 1))) << cellBits) | (numpy.asarray(cellY, dtype=numpy.int64) + (1 << (cellBits - 1)))

  def candidates(self, x, y):
    cells = {(math.floor(cornerX / gridCell), math.floor(cornerY / gridCell)) for cornerX in (x - sweepRadius, x + sweepRadius) for cornerY in (y - sweepRadius, y + sweepRadius)}
    found = []
    for cellX, cellY in cells:
      key = self.cellKey(cellX, cellY)
      found.append(self.cellTriangles[numpy.searchsorted(self.cellKeys, key, "left"):numpy.searchsorted(self.cellKeys, key, "right")])
    return numpy.unique(numpy.concatenate(found))

  def landing(self, x, y, top, bottom):
    """The floor a sweep from top down to bottom touches first, or None."""
    candidates = self.candidates(x, y)
    if len(candidates) == 0:
      return None
    heights = sphereContactHeights(x, y, self.corners[candidates])
    touched = (heights <= top) & (heights >= bottom)
    if not touched.any():
      return None
    return int(candidates[numpy.flatnonzero(touched)[numpy.argmax(heights[touched])]])

  def share(self, center, radius):
    """The share of scene light a drop from a bounding sphere finds: the first sweep that lands gives the mean of its floor's corner
    alphas over 256, never below leastShare; none landing gives leastShare."""
    for x, y, top, bottom in dropSweeps(center, radius):
      floor = self.landing(x, y, top, bottom)
      if floor is not None:
        return max(chopped(float(self.alphaSums[floor]) / 256 * floorThird), numpy.float32(leastShare))
    return numpy.float32(leastShare)


def placedColors(part, placement, rotation, lights, floors):
  """The RGBA per vertex the client gives a static actor at load (0x100530f0). Alpha is the share of scene light a drop from the
  model's bounding sphere, set at the placement and neither turned nor scaled, finds (ShareFloors.share), times 255, truncated. RGB
  sums, over the zone lights whose sphere reaches the actor's (the light's radius plus the bounding radius times the placement's
  scale, from its position), each light's color times 0.05 radius^2 / distance^2 times the facing of the vertex's normal toward it,
  for a light within its radius in front of the vertex; times 255, truncated and capped at 255. The vertices and normals are turned
  and moved by the actor's matrix, which holds no scale (CSimpleActor + 0xe4 in the dumps), the normals read through the client's
  table (normalSteps) and normalized."""
  sphere = part["boundingSphere"]
  position = numpy.asarray(placement["position"], dtype=numpy.float64)
  share = floors.share(position + sphere["center"], sphere["radius"])
  alpha = int(float(share) * 255)
  world = part["vertices"] @ rotation.T + position
  normals = normalSteps[(numpy.rint(part["lighting"]["normals"] * 127).astype(numpy.int64) >> 3) & 31] @ rotation.T
  lengths = numpy.linalg.norm(normals, axis=1, keepdims=True)
  normals = numpy.where(lengths >= shortestNormal, normals / numpy.maximum(lengths, shortestNormal), 0)
  reach = sphere["radius"] * placement["scale"]
  light = numpy.zeros((len(world), 3), dtype=numpy.float32)
  for source in lights:
    lightPosition = numpy.asarray(source["position"], dtype=numpy.float64)
    if ((lightPosition - position) ** 2).sum() >= (source["radius"] + reach) ** 2:
      continue
    toLight = lightPosition - world
    distanceSquared = (toLight ** 2).sum(1)
    with numpy.errstate(divide="ignore", invalid="ignore"):
      facing = (toLight * normals).sum(1) / numpy.sqrt(distanceSquared)
    lit = (distanceSquared < source["radius"] ** 2) & (facing > 0)
    strength = numpy.where(lit, source["radius"] ** 2 * lightScale / numpy.where(lit, distanceSquared, 1) * facing, 0)
    light = (light + (strength[:, None] * numpy.asarray(source["color"])[None, :]).astype(numpy.float32)).astype(numpy.float32)
  channels = numpy.minimum(numpy.trunc(light.astype(numpy.float64) * 255), 255).astype(numpy.uint8)
  return numpy.concatenate([channels, numpy.full((len(world), 1), alpha, dtype=numpy.uint8)], axis=1)
