"""Renderer calibration against live client screenshots (docs/clientRendering.md). A screenshot named
<zone>,<loc y>,<loc x>,<loc z>,<compass heading>,<pitch>.jpg fixes the camera. Rendering the same view's passes (texture, normal,
baked light, share of scene light) and solving the client's lighting for the colors and sun direction that best explain the
screenshot measures the scene light at the moment of the shot; the comparison shows what the renderer still gets wrong."""
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
# Pixels the fit trusts: drawn, with enough texture to divide by, and not clipped in the screenshot.
minimumBase = 0.06
maximumScreen = 0.98
normalBinDegrees = 20.0
minimumGroupPixels = 150
azimuthStepDegrees = 5.0
elevationStepDegrees = 5.0


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


def surfaceGroups(screen, passes):
  """Pixels grouped by surface direction: each group's median screenshot-to-texture ratio per channel, mean unit normal, baked light,
  share, and pixel count. Medians keep glows, misregistered edges, and JPEG noise from steering the fit."""
  base, normal, baked, share = (passes[name] for name in ("base", "normal", "baked", "share"))
  valid = (passes["lit"][..., 3] > 0.5) & (base[..., :3].min(axis=2) > minimumBase) & (screen.max(axis=2) < maximumScreen)
  normals = normal[..., :3][valid]
  lengths = numpy.linalg.norm(normals, axis=1)
  keep = lengths > 0.5
  normals = normals[keep] / lengths[keep, None]
  ratios = (screen[valid] / base[..., :3][valid])[keep]
  bakedValues, shareValues = baked[..., :3][valid][keep], share[..., 0][valid][keep]
  azimuthBin = numpy.floor((numpy.degrees(numpy.arctan2(normals[:, 1], normals[:, 0])) % 360) / normalBinDegrees)
  elevationBin = numpy.floor((numpy.degrees(numpy.arcsin(numpy.clip(normals[:, 2], -1, 1))) + 90) / normalBinDegrees)
  keys = elevationBin * 1000 + azimuthBin
  groups = []
  for key in numpy.unique(keys):
    members = keys == key
    if members.sum() < minimumGroupPixels:
      continue
    meanNormal = normals[members].mean(axis=0)
    groups.append({
      "ratio": numpy.median(ratios[members], axis=0), "normal": meanNormal / numpy.linalg.norm(meanNormal), "baked": bakedValues[members].mean(axis=0),
      "share": float(shareValues[members].mean()), "pixels": int(members.sum()),
    })
  return groups


def towardSun(azimuthDegrees, elevationDegrees):
  azimuth, elevation = math.radians(azimuthDegrees), math.radians(elevationDegrees)
  return numpy.array((math.sin(azimuth) * math.cos(elevation), math.cos(azimuth) * math.cos(elevation), math.sin(elevation)))


def nonNegativeSolve(design, target, weights):
  """Weighted least squares with every coefficient at least 0, dropping negative ones until none remain."""
  active = list(range(design.shape[1]))
  while True:
    coefficients = numpy.zeros(design.shape[1])
    if active:
      solved, *_ = numpy.linalg.lstsq(design[:, active] * weights[:, None], target * weights, rcond=None)
      coefficients[active] = solved
    negative = [column for column in active if coefficients[column] < 0]
    if not negative:
      return coefficients
    active.remove(min(negative, key=lambda column: coefficients[column]))


def fitLighting(groups):
  """The scene light that best explains the groups' ratios under the client's formula (clamping aside): ambient, sun, and bounce
  colors and the direction toward the sun, searched over a grid of directions with the colors solved per channel. Special ambient
  adds like ambient wherever every surface takes the full share of scene light, so it is folded into ambient."""
  if len(groups) < 2:
    raise ValueError(f"Only {len(groups)} surface directions have enough usable pixels to fit lighting")
  normals = numpy.array([group["normal"] for group in groups])
  shares = numpy.array([group["share"] for group in groups])
  targets = numpy.array([group["ratio"] - group["baked"] for group in groups])
  weights = numpy.sqrt(numpy.array([group["pixels"] for group in groups], dtype=float))
  best = None
  for elevation in numpy.arange(-80, 80 + elevationStepDegrees / 2, elevationStepDegrees):
    for azimuth in numpy.arange(0, 360, azimuthStepDegrees):
      facing = normals @ towardSun(azimuth, elevation)
      design = numpy.stack([shares, shares * numpy.maximum(facing, 0), shares * numpy.maximum(-facing, 0)], axis=1)
      colors = numpy.stack([nonNegativeSolve(design, targets[:, channel], weights) for channel in range(3)], axis=1)
      residual = float(numpy.sqrt((((design @ colors - targets) * weights[:, None]) ** 2).sum() / (weights ** 2).sum() / 3))
      if best is None or residual < best["residual"]:
        best = {"residual": residual, "azimuth": float(azimuth), "elevation": float(elevation), "colors": colors}
  ambient, sun, bounce = (numpy.clip(best["colors"][row], 0, 1) for row in range(3))
  return {
    "ambientColor": [round(float(value), 4) for value in ambient], "sunColor": [round(float(value), 4) for value in sun],
    "bounceColor": [round(float(value), 4) for value in bounce], "specialAmbientColor": [0.0, 0.0, 0.0],
    "sunAzimuthDegrees": best["azimuth"], "sunElevationDegrees": best["elevation"], "ratioResidual": round(best["residual"], 4),
    "surfaceDirections": len(groups),
  }


def latestResults(calibrationRoot):
  """Each screenshot's latest calibration run: when, the fit's ratio residual, and the mean pixel difference."""
  latest = []
  for shotFolder in sorted(path for path in calibrationRoot.glob("*") if path.is_dir()) if calibrationRoot.is_dir() else []:
    runs = sorted(path for path in shotFolder.glob("*/result.json"))
    if runs:
      result = json.loads(runs[-1].read_text(encoding="utf-8"))
      latest.append({
        "screenshot": result["screenshot"], "zone": result["zone"], "time": result["time"], "runs": len(runs),
        "ratioResidual": result["fit"]["ratioResidual"], "meanPixelDifference": result["meanPixelDifference"],
      })
  return latest


def comparison(screen, render):
  """The screenshot beside the render, and the mean absolute difference over drawn pixels (0-255 scale)."""
  drawn = render[..., 3] > 0.5
  difference = float(numpy.abs(screen - render[..., :3])[drawn].mean() * 255) if drawn.any() else None
  sideBySide = numpy.concatenate([screen, render[..., :3]], axis=1)
  return Image.fromarray(numpy.clip(sideBySide * 255 + 0.5, 0, 255).astype(numpy.uint8)), difference
