"""The client's sky drawn for view directions from eqSky's state, in EQGraphicsDX9.dll's order (0x1002cb70): the dome, the sun and
moon, then the horizon band, none of it fogged. Plain numpy, shared by Blender's Python and the server."""
import math

import numpy

# The zone properties a sky supplies: eqgame.exe takes the light and the fog color from the sky when the zone draws one.
suppliedZoneKeys = ("ambientColor", "fogColor", "sunColor", "bounceColor", "sunAzimuthDegrees", "sunElevationDegrees")
noSky = "none"
# The dome's rings by angle from its pole (0x1002f110): the pole and a ring at 0.01 radians take row 0, ring r (0-28) at
# 0.1 + r * (pi - 0.2) / 29 takes row r + 1, and the bottom row 29; colors run smoothly between rings.
domeRingAngles = numpy.array([0.0, 0.01] + [0.1 + ring * (math.pi - 0.2) / 29 for ring in range(29)] + [math.pi])
domeRingRows = numpy.array([0, 0] + [ring + 1 for ring in range(29)] + [29])


def domeColors(directions, dome):
  angles = numpy.arccos(numpy.clip(directions @ numpy.array(dome["axis"], dtype=numpy.float32), -1, 1))
  ringColors = numpy.array(dome["colors"], dtype=numpy.float32)[domeRingRows]
  return numpy.stack([numpy.interp(angles, domeRingAngles, ringColors[:, channel]) for channel in range(3)], axis=-1).astype(numpy.float32)


def drawSatellite(colors, directions, satellite, textures):
  """A satellite is a textured quad square to its direction, alpha-blended over what is drawn; the state textures it fades between
  are drawn old first."""
  toward = numpy.array(satellite["direction"], dtype=numpy.float32)
  facing = directions @ toward
  ahead = numpy.flatnonzero(facing > 1e-6)
  projected = directions[ahead] / facing[ahead, None] - toward
  across = projected @ numpy.array(satellite["right"], dtype=numpy.float32) / (2 * satellite["halfExtent"]) + 0.5
  down = 0.5 - projected @ numpy.array(satellite["up"], dtype=numpy.float32) / (2 * satellite["halfExtent"])
  inside = (across >= 0) & (across < 1) & (down >= 0) & (down < 1)
  pixels = ahead[inside]
  for texture in reversed(satellite["textures"]):
    texels = textures[texture["path"]]
    rows = numpy.minimum((down[inside] * texels.shape[0]).astype(numpy.int64), texels.shape[0] - 1)
    columns = numpy.minimum((across[inside] * texels.shape[1]).astype(numpy.int64), texels.shape[1] - 1)
    sampled = texels[rows, columns].astype(numpy.float32) / 255
    alpha = sampled[:, 3:4] * texture["weight"]
    colors[pixels] = colors[pixels] * (1 - alpha) + sampled[:, :3] * alpha


def drawHorizon(colors, directions, horizon, fogColor, cameraZ):
  """The horizon band (0x10029ac0): opaque fog color below elevation a, fading across the band of height h to the color map's row
  30 at its own alpha, nothing above; a and h move from their minimum to their maximum as the camera rises from MinCameraZ to
  MaxCameraZ."""
  share = min(max((cameraZ - horizon["minCameraZ"]) / (horizon["maxCameraZ"] - horizon["minCameraZ"]), 0.0), 1.0)
  low = horizon["minAngle"] + (horizon["maxAngle"] - horizon["minAngle"]) * share
  high = low + horizon["minWidth"] + (horizon["maxWidth"] - horizon["minWidth"]) * share
  elevation = numpy.arcsin(numpy.clip(directions[:, 2], -1, 1))
  alpha = (elevation < low).astype(numpy.float32)
  bandColor = numpy.empty_like(colors)
  bandColor[:] = fogColor
  inBand = numpy.flatnonzero((elevation >= low) & (elevation <= high))
  tangent = numpy.tan(elevation[inBand])
  # The band is a strip between two rings; s is where along it the view ray crosses, perspective-correct.
  across = (math.sin(low) - tangent * math.cos(low)) / ((math.sin(low) - math.sin(high)) - tangent * (math.cos(low) - math.cos(high)))
  across = numpy.clip(across, 0, 1)[:, None]
  bandColor[inBand] = numpy.array(fogColor, dtype=numpy.float32) * (1 - across) + numpy.array(horizon["color"], dtype=numpy.float32) * across
  alpha[inBand] = ((1 - across) + horizon["alpha"] * across)[:, 0]
  return colors * (1 - alpha[:, None]) + bandColor * alpha[:, None]


def skyColors(directions, sky, cameraZ, textures):
  """Colors 0-1 of the sky in unit view directions (n x 3); textures maps each satellite texture path to its RGBA array."""
  colors = domeColors(directions, sky["dome"])
  for satellite in sky["satellites"]:
    drawSatellite(colors, directions, satellite, textures)
  if sky["horizon"] is not None:
    colors = drawHorizon(colors, directions, sky["horizon"], sky["environment"]["fogColor"], cameraZ)
  return colors


def equirectangularDirections(width, height):
  """Unit view directions of an equirectangular image's pixels as Blender maps one onto the world, rows from the bottom: column u
  (0-1) faces atan2(y, x) = (0.5 - u) * 2 pi, row v looks up (v - 0.5) * pi."""
  azimuth = (0.5 - (numpy.arange(width, dtype=numpy.float32) + 0.5) / width) * 2 * math.pi
  elevation = ((numpy.arange(height, dtype=numpy.float32) + 0.5) / height - 0.5) * math.pi
  elevation, azimuth = numpy.meshgrid(elevation, azimuth, indexing="ij")
  return numpy.stack([numpy.cos(elevation) * numpy.cos(azimuth), numpy.cos(elevation) * numpy.sin(azimuth), numpy.sin(elevation)], axis=-1).reshape(-1, 3)
