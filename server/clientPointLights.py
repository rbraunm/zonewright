"""The point lights the client adds to each vertex it draws, as EQGraphicsDX9.dll chooses and its effects compute them
(docs/clientRendering.md, Point lights): each draw takes up to three of the lights reaching it, the three scoring highest, and each
vertex adds color * clamp(N . L, 0, 1) * (1 - min((distance / radius)^2, 1)) for each, unscaled by its share of scene light. Besides a
zone's lights, the character the view belongs to carries the light of its light source (carriedLight)."""
import math

import numpy

lightsPerDraw = 3
# The EQG zone loaders mark a light whose name's third character is B (LIB_ rather than LIT_) to light geometry that carries baked
# light (0x100653e0, 0x10065b13); lights.wld's never are (0x1001cf69).
bakedGeometryMark = "b"
# The light definitions eqgame.exe makes for carried lights (0x488640); the effects take a definition's first frame (EQGraphicsDX9.dll
# 0x10089fbc-0x10089fe5).
carriedLightColors = {
  "torch": (0.9959999918937683, 0.9412000179290771, 0.6273999810218811), "candle": (0.9959999918937683, 0.9412000179290771, 0.6273999810218811),
  "smlantern": (0.9959999918937683, 0.9412000179290771, 0.6273999810218811), "lglantern": (0.9959999918937683, 0.9412000179290771, 0.6273999810218811),
  "magtorch": (0.4000000059604645, 0.9959999918937683, 0.9959999918937683), "smmagic": (0.4000000059604645, 0.9959999918937683, 0.9959999918937683),
  "lgmagic": (0.4000000059604645, 0.9959999918937683, 0.9959999918937683), "redlight": (0.5, 0.25999999046325684, 0.0),
  "blueLight": (0.1599999964237213, 0.20000000298023224, 0.5),
}
# Each light type a character carries (the spawn's light): its definition and reach (eqgame.exe 0x48b270, jump table 0x48b444).
carriedLightTypes = {
  1: ("candle", 25.0), 2: ("torch", 100.0), 3: ("magtorch", 100.0), 4: ("smlantern", 150.0), 5: ("smmagic", 150.0), 6: ("lglantern", 200.0),
  7: ("lgmagic", 200.0), 8: ("lglantern", 400.0), 9: ("magtorch", 100.0), 10: ("smmagic", 150.0), 11: ("lgmagic", 200.0), 12: ("redlight", 150.0),
  13: ("blueLight", 150.0), 14: ("redlight", 150.0), 15: ("blueLight", 150.0),
}
# The reach is capped by zone id (eqgame.exe 0x514220): 25 in zone 121 and 100 in zone 158.
carriedLightReachCaps = {121: 25.0, 158: 100.0}
carriedLightReachCap = 10000.0
# The viewing character's own light ranks a hundred times its score (EQGraphicsDX9.dll 0x1000ff61 through the light's +0x1d, which
# eqgame.exe sets for it at 0x590434).
viewersLightPriority = 100.0
carriedLightHeight = 4.0
carriedLightOffset = 2.0
# eqgame.exe 0x59012b passes the heading turned to radians (times 6.28 and 1/512) to the client's 512-step sine and cosine tables
# (EQGraphicsDX9.dll 0x100b9390, 0x100b93b0), which read it as 512ths of a turn: the offset turns by at most 6 of 512 steps.
headingToRadians = float(numpy.float32(6.28)) / 512


def lightsBakedGeometry(light):
  """Whether a light lights geometry that carries baked light: a zone light by its name's mark, and every light eqgame.exe makes through
  the scene graph (0x1006ace0 sets the mark), such as a carried one."""
  return light.get("carriedByViewer", False) or (len(light["name"]) > 2 and light["name"][2].lower() == bakedGeometryMark)


def priority(light):
  return viewersLightPriority if light.get("carriedByViewer", False) else 1.0


def lightness(color):
  """The middle of a light's brightest and dimmest channel, as its selection weight takes it (0x1000e4f0)."""
  return (max(color) + min(color)) * 0.5


def selectionScore(light, center):
  """How strongly the client ranks a light for a draw centered at center: 2 * lightness * radius^2 / distance^2 (0x1000ff61), times the
  priority of the viewer's own light."""
  distanceSquared = float(numpy.sum((numpy.asarray(light["position"], dtype=float) - numpy.asarray(center, dtype=float)) ** 2))
  weight = lightness(light["color"]) * light["radius"] ** 2 * priority(light)
  return float("inf") if distanceSquared == 0 else 2.0 * weight / distanceSquared


def carriedLight(lightType, at, headingDegrees, zoneId):
  """The light the viewing character carries (eqgame.exe 0x5900e0, 0x48b270): its light type's definition color and reach (capped by
  the zone's id), standing 4 above the character's feet at `at` and 2 off them in the direction the client's tables give its heading
  (headingDegrees: 0 = +Y, clockwise), which stays within 5 degrees of +X. A character whose model holds the attachment point 0x17
  holds it there instead (0x5901cb), which is not modeled."""
  if lightType not in carriedLightTypes:
    raise ValueError(f"A carried light type is one of {sorted(carriedLightTypes)} (0 carries none), got {lightType!r}")
  definition, reach = carriedLightTypes[lightType]
  heading512 = numpy.float32((128.0 - headingDegrees * 512.0 / 360.0) % 512.0)
  step = int(numpy.float32(float(heading512) * headingToRadians)) & 0x1FF
  turn = 2 * math.pi * step / 512
  position = [at[0] + carriedLightOffset * math.cos(turn), at[1] + carriedLightOffset * math.sin(turn), at[2] + carriedLightHeight]
  return {
    "name": f"carried {definition}", "position": position, "color": list(carriedLightColors[definition]),
    "radius": min(reach, carriedLightReachCaps.get(zoneId, carriedLightReachCap)), "carriedByViewer": True,
  }


def reachesBox(light, low, high):
  """Whether a light's sphere of influence reaches a box: the test this preview takes for the dPVS region of influence the client
  makes of each light, whose own test is not traced."""
  position = numpy.asarray(light["position"], dtype=float)
  nearest = numpy.clip(position, low, high)
  return float(numpy.sum((position - nearest) ** 2)) <= light["radius"] ** 2


def eligible(lights, takesAllLights):
  """The lights a draw may take: geometry with baked light (zone regions, and placed models whose baked light the client kept) takes
  only the lights marked to light it; the rest take every light (0x1000ff3c)."""
  return lights if takesAllLights else [light for light in lights if lightsBakedGeometry(light)]


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
    score = 2.0 * lightness(light["color"]) * radius * radius * priority(light) / numpy.maximum(distanceSquared, 1e-12)
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
