"""The point lights the client adds to each vertex it draws, as EQGraphicsDX9.dll chooses and its effects compute them
(docs/clientRendering.md, Point lights): each draw takes up to three of the lights reaching it, the three scoring highest, and each
vertex adds color * clamp(N . L, 0, 1) * (1 - min((distance / radius)^2, 1)) for each, unscaled by its share of scene light."""
import numpy

lightsPerDraw = 3
# The EQG zone loaders mark a light whose name's third character is B (LIB_ rather than LIT_) to light geometry that carries baked
# light (0x100653e0, 0x10065b13); lights.wld's never are (0x1001cf69).
bakedGeometryMark = "b"


def lightsBakedGeometry(name):
  return len(name) > 2 and name[2].lower() == bakedGeometryMark


def lightness(color):
  """The middle of a light's brightest and dimmest channel, as its selection weight takes it (0x1000e4f0)."""
  return (max(color) + min(color)) * 0.5


def selectionScore(light, center):
  """How strongly the client ranks a light for a draw centered at center: 2 * lightness * radius^2 / distance^2 (0x1000ff61)."""
  distanceSquared = float(numpy.sum((numpy.asarray(light["position"], dtype=float) - numpy.asarray(center, dtype=float)) ** 2))
  weight = lightness(light["color"]) * light["radius"] ** 2
  return float("inf") if distanceSquared == 0 else 2.0 * weight / distanceSquared


def reachesBox(light, low, high):
  """Whether a light's sphere of influence reaches a box: the test this preview takes for the dPVS region of influence the client
  makes of each light, whose own test is not traced."""
  position = numpy.asarray(light["position"], dtype=float)
  nearest = numpy.clip(position, low, high)
  return float(numpy.sum((position - nearest) ** 2)) <= light["radius"] ** 2


def eligible(lights, takesAllLights):
  """The lights a draw may take: geometry with baked light (zone regions, and placed models whose baked light the client kept) takes
  only the lights marked to light it; the rest take every light (0x1000ff3c)."""
  return lights if takesAllLights else [light for light in lights if lightsBakedGeometry(light["name"])]


def selectForDraw(lights, center, low, high, takesAllLights):
  """The up to three lights the client gives one draw: those reaching it, highest score first (0x1000ffe2)."""
  candidates = [light for light in eligible(lights, takesAllLights) if reachesBox(light, low, high)]
  candidates.sort(key=lambda light: -selectionScore(light, center))
  return candidates[:lightsPerDraw]


def addedLight(positions, normals, lights):
  """Each vertex's light from the given lights (rows of positions and normals, world space): the effects' point light term, summed."""
  positions = numpy.asarray(positions, dtype=numpy.float64)
  normals = numpy.asarray(normals, dtype=numpy.float64)
  total = numpy.zeros((len(positions), 3))
  for light in lights:
    toLight = numpy.asarray(light["position"], dtype=numpy.float64) - positions
    distance = numpy.linalg.norm(toLight, axis=1)
    facing = numpy.einsum("ij,ij->i", normals, toLight) / numpy.where(distance > 0, distance, 1.0)
    falloff = 1.0 - numpy.minimum((distance / light["radius"]) ** 2, 1.0)
    total += numpy.outer(numpy.clip(facing, 0.0, 1.0) * falloff, light["color"])
  return total


def addedLightPerVertex(positions, normals, lights):
  """Each vertex's light when every vertex takes its own three lights, highest score at the vertex first: how this preview draws a
  zone's regions, whose division into the client's draws (dPVS objects) is not traced."""
  positions = numpy.asarray(positions, dtype=numpy.float64)
  normals = numpy.asarray(normals, dtype=numpy.float64)
  bestScores = numpy.full((len(positions), lightsPerDraw), -numpy.inf)
  bestTerms = numpy.zeros((len(positions), lightsPerDraw, 3))
  for light in lights:
    center, radius = numpy.asarray(light["position"], dtype=numpy.float64), light["radius"]
    near = numpy.flatnonzero(numpy.all(numpy.abs(positions - center) <= radius, axis=1))
    toLight = center - positions[near]
    distanceSquared = numpy.einsum("ij,ij->i", toLight, toLight)
    reaches = distanceSquared <= radius * radius
    near, toLight, distanceSquared = near[reaches], toLight[reaches], distanceSquared[reaches]
    if not len(near):
      continue
    distance = numpy.sqrt(distanceSquared)
    score = 2.0 * lightness(light["color"]) * radius * radius / numpy.maximum(distanceSquared, 1e-12)
    facing = numpy.einsum("ij,ij->i", normals[near], toLight) / numpy.where(distance > 0, distance, 1.0)
    term = numpy.outer(numpy.clip(facing, 0.0, 1.0) * (1.0 - numpy.minimum(distanceSquared / (radius * radius), 1.0)), light["color"])
    # Each vertex keeps its three highest scores; a later light displaces the lowest only when it scores higher, as the client's
    # selection sort keeps the earlier of two equal lights.
    slot = numpy.argmin(bestScores[near], axis=1)
    displaces = score > bestScores[near, slot]
    rows, columns = near[displaces], slot[displaces]
    bestScores[rows, columns] = score[displaces]
    bestTerms[rows, columns] = term[displaces]
  return bestTerms.sum(axis=1)
