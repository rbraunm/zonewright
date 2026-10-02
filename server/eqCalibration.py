"""Renderer calibration against live client screenshots (docs/clientRendering.md). A screenshot named
<zone>,<loc y>,<loc x>,<loc z>,<compass heading>,<pitch>.jpg fixes the camera. Rendering the same view's passes (texture, normal,
baked light, share of scene light) and solving the client's lighting for the colors and sun direction that best explain the
screenshot measures the scene light (and, without the zone header, the fog) at the moment of the shot; the comparison shows what
the renderer still gets wrong."""
import json
import math
from pathlib import Path

import numpy
from PIL import Image

# Measured against the Palatial Guild Hall screenshot (a high elf female, first person): the eye sits this far above /loc's z.
eyeAboveLoc = 0.66
# Measured by aligning renders to Plane of Knowledge and Eastern Wastes screenshots: the client's first-person camera looks this far
# below the pitch a screenshot's name records.
pitchOffsetDegrees = -4.5
anglesPerTurn = 512
lookDistance = 100.0
# Pixels the fit trusts: drawn, with enough texture to light, and not clipped in the screenshot; a sample of them is fitted.
minimumBase = 0.06
maximumScreen = 0.98
minimumPixels = 1000
fitSamples = 20000
azimuthStepDegrees = 5.0
elevationStepDegrees = 5.0
# The client's fog density (RegionOldA.fxo's default, which the DLL keeps); a fitted fog varies start, end, and color.
clientFogDensity = 0.33
# RegionOldA.fxo's fFogRange.
fogRange = 10.0
fogStartSteps = 25
fogEndSteps = 30
# With the fog unknown, sun direction and fog are searched in turn this many times.
fogRounds = 3


def parseShotName(path):
  """The zone name, /loc (y, x, z), compass heading (degrees clockwise from north), and pitch (512 to a turn) a screenshot's name holds."""
  parts = Path(path).stem.split(",")
  if len(parts) != 6:
    raise ValueError(f"Screenshot '{Path(path).name}' is not named <zone>,<loc y>,<loc x>,<loc z>,<heading>,<pitch>")
  zone, *numbers = parts
  try:
    locY, locX, locZ, compass, pitch = (float(number) for number in numbers)
  except ValueError as error:
    raise ValueError(f"Screenshot '{Path(path).name}' has a non-numeric field: {error}") from error
  return {"zone": zone, "loc": (locY, locX, locZ), "compassDegrees": compass, "pitch": pitch}


def shotView(shot):
  """The view the client shot from: eye above /loc (the scene's x, y are /loc's y, x), looking along the compass heading, which turns
  clockwise from north, the scene's +X, and the pitch."""
  locY, locX, locZ = shot["loc"]
  eye = [locY, locX, locZ + eyeAboveLoc]
  heading = math.radians(shot["compassDegrees"])
  pitch = math.radians(shot["pitch"] * 360 / anglesPerTurn + pitchOffsetDegrees)
  direction = (math.cos(heading) * math.cos(pitch), -math.sin(heading) * math.cos(pitch), math.sin(pitch))
  return {"eye": eye, "target": [eye[index] + lookDistance * direction[index] for index in range(3)]}


def screenshotPixels(path, width, height):
  """The screenshot cropped about its center to the render's aspect and resized to it, as raw values 0-1 (rows top to bottom)."""
  image = Image.open(path).convert("RGB")
  if image.width * height >= image.height * width:
    cropWidth = image.height * width // height
    image = image.crop(((image.width - cropWidth) // 2, 0, (image.width + cropWidth) // 2, image.height))
  else:
    cropHeight = image.width * height // width
    image = image.crop((0, (image.height - cropHeight) // 2, image.width, (image.height + cropHeight) // 2))
  return numpy.asarray(image.resize((width, height), Image.LANCZOS), dtype=numpy.float64) / 255


def scenePixels(screen, passes):
  """The pixels the fit trusts (drawn, with enough texture to light, and not clipped in the screenshot), sampled: the screenshot's
  color and the passes' texture, unit normal, baked light, share of scene light, and distance at each."""
  base, normal, baked, share, distance = (passes[name] for name in ("base", "normal", "baked", "share", "distance"))
  valid = (passes["lit"][..., 3] > 0.5) & (base[..., :3].min(axis=2) > minimumBase) & (screen.max(axis=2) < maximumScreen)
  normals = normal[..., :3][valid] * 2 - 1
  lengths = numpy.linalg.norm(normals, axis=1)
  keep = numpy.flatnonzero(lengths > 0.5)
  if len(keep) < minimumPixels:
    raise ValueError(f"Only {len(keep)} usable pixels to fit lighting by")
  keep = numpy.random.default_rng(0).choice(keep, min(fitSamples, len(keep)), replace=False)
  return {
    "observed": screen[valid][keep], "base": base[..., :3][valid][keep], "normal": normals[keep] / lengths[keep, None],
    "baked": baked[..., :3][valid][keep], "share": share[..., 0][valid][keep], "distance": distance[..., 0][valid][keep],
  }


def towardSun(azimuthDegrees, elevationDegrees):
  azimuth, elevation = math.radians(azimuthDegrees), math.radians(elevationDegrees)
  return numpy.array((math.sin(azimuth) * math.cos(elevation), math.cos(azimuth) * math.cos(elevation), math.sin(elevation)))


def nonNegativeSolve(gram, moment):
  """Least squares from its normal equations with every coefficient at least 0, dropping negative ones until none remain."""
  active = list(range(len(moment)))
  while True:
    coefficients = numpy.zeros(len(moment))
    if active:
      coefficients[active] = numpy.linalg.lstsq(gram[numpy.ix_(active, active)], moment[active], rcond=None)[0]
    negative = [column for column in active if coefficients[column] < 0]
    if not negative:
      return coefficients
    active.remove(min(negative, key=lambda column: coefficients[column]))


def solveColors(pixels, direction, visibility, withFog):
  """For one sun direction and fog visibility per pixel, the ambient, sun, bounce (and fog) colors that best explain the screenshot,
  each channel solved by least squares, and the root mean square error."""
  facing = pixels["normal"] @ direction
  light = numpy.stack([pixels["share"], pixels["share"] * numpy.maximum(facing, 0), pixels["share"] * numpy.maximum(-facing, 0)], axis=1)
  colors, squared = [], 0.0
  for channel in range(3):
    design = light * (visibility * pixels["base"][:, channel])[:, None]
    if withFog:
      design = numpy.concatenate([design, (1 - visibility)[:, None]], axis=1)
    target = pixels["observed"][:, channel] - visibility * pixels["base"][:, channel] * pixels["baked"][:, channel]
    coefficients = nonNegativeSolve(design.T @ design, design.T @ target)
    colors.append(coefficients)
    squared += float(((design @ coefficients - target) ** 2).sum())
  return numpy.array(colors).T, math.sqrt(squared / (3 * len(visibility)))


def fogVisibility(distances, start, end, density):
  ramp = numpy.clip(fogRange * (distances - start) / (end - start), 0, fogRange)
  return numpy.exp(-(density * ramp) ** 2)


def bestDirection(pixels, visibility, withFog):
  best = None
  for elevation in numpy.arange(0, 85 + elevationStepDegrees / 2, elevationStepDegrees):
    for azimuth in numpy.arange(0, 360, azimuthStepDegrees):
      colors, residual = solveColors(pixels, towardSun(azimuth, elevation), visibility, withFog)
      if best is None or residual < best["residual"]:
        best = {"azimuth": float(azimuth), "elevation": float(elevation), "colors": colors, "residual": residual}
  return best


def bestFog(pixels, direction):
  farthest = float(numpy.percentile(pixels["distance"], 99))
  best = None
  for start in numpy.linspace(0, farthest, fogStartSteps):
    for end in start + numpy.geomspace(20, max(40, 4 * farthest), fogEndSteps):
      visibility = fogVisibility(pixels["distance"], start, end, clientFogDensity)
      colors, residual = solveColors(pixels, direction, visibility, True)
      if best is None or residual < best["residual"]:
        best = {"start": float(start), "end": float(end), "colors": colors, "residual": residual}
  return best


def fitScene(screen, passes, fog=None):
  """The scene light (and, with fog None, the fog) that best explains the screenshot under the client's formula, clamping aside: for
  each sun direction (and fog start and end), the ambient, sun, bounce (and fog) colors solved by least squares over every trusted
  pixel. With the fog unknown, direction and fog are searched in turn. Special ambient adds like ambient wherever every surface takes
  the full share of scene light, so it is folded into ambient. A sun from a direction lights exactly as a bounce from the opposite
  one does, so the sun is searched above the horizon and light from below is bounce."""
  pixels = scenePixels(screen, passes)
  if fog is not None:
    best = bestDirection(pixels, fogVisibility(pixels["distance"], fog["fogStart"], fog["fogEnd"], fog["fogDensity"]), False)
    fitted = None
  else:
    best = bestDirection(pixels, numpy.ones(len(pixels["distance"])), False)
    fitted = None
    for _ in range(fogRounds):
      fitted = bestFog(pixels, towardSun(best["azimuth"], best["elevation"]))
      best = bestDirection(pixels, fogVisibility(pixels["distance"], fitted["start"], fitted["end"], clientFogDensity), True)
    fitted |= {"colors": best["colors"]}
  ambient, sun, bounce = (numpy.clip(best["colors"][row], 0, 1) for row in range(3))
  result = {
    "ambientColor": [round(float(value), 4) for value in ambient], "sunColor": [round(float(value), 4) for value in sun],
    "bounceColor": [round(float(value), 4) for value in bounce], "specialAmbientColor": [0.0, 0.0, 0.0],
    "sunAzimuthDegrees": best["azimuth"], "sunElevationDegrees": best["elevation"], "residual": round(best["residual"], 4), "pixels": len(pixels["distance"]),
  }
  if fitted is not None:
    result |= {
      "fogStart": round(fitted["start"], 1), "fogEnd": round(fitted["end"], 1), "fogDensity": clientFogDensity,
      "fogColor": [round(float(value), 4) for value in numpy.clip(fitted["colors"][3], 0, 1)],
    }
  return result


def latestResults(calibrationRoot):
  """Each screenshot's latest calibration run: when, the fit's residual, and the mean pixel difference."""
  latest = []
  for shotFolder in sorted(path for path in calibrationRoot.glob("*") if path.is_dir()) if calibrationRoot.is_dir() else []:
    runs = sorted(path for path in shotFolder.glob("*/result.json"))
    if runs:
      result = json.loads(runs[-1].read_text(encoding="utf-8"))
      latest.append({
        "screenshot": result["screenshot"], "zone": result["zone"], "time": result["time"], "runs": len(runs),
        "residual": result["fit"]["residual"], "meanPixelDifference": result["meanPixelDifference"],
      })
  return latest


def comparison(screen, render):
  """The screenshot beside the render, and the mean absolute difference over drawn pixels (0-255 scale)."""
  drawn = render[..., 3] > 0.5
  difference = float(numpy.abs(screen - render[..., :3])[drawn].mean() * 255) if drawn.any() else None
  sideBySide = numpy.concatenate([screen, render[..., :3]], axis=1)
  return Image.fromarray(numpy.clip(sideBySide * 255 + 0.5, 0, 255).astype(numpy.uint8)), difference
