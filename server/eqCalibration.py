"""Renderer calibration against claudelink shots of Claude's spare RoF2 client (docs/clientRendering.md, Calibration). A shot is the
client window's PNG, a sidecar (claudelinkScreenshot 1) holding its conditions, and a zone dump (claudelinkZoneDump 1) read in the same
frame. The dump fixes the camera, the clock, the zone header, the light the viewer carries, the special ambient it stands in, and what
stands in the zone, so the renderer predicts the shot without fitting anything, and the comparison shows what it still gets wrong."""
import hashlib
import json
import math
import os
from pathlib import Path

import numpy
from PIL import Image

import clientPointLights
import eqAxes
import previewFrame
import skyDrawing
import viewerLight

sidecarFormat = ("claudelinkScreenshot", 1)
dumpFormat = ("claudelinkZoneDump", 1)
# Under this, or with MacroQuest's frame limiter throttling the settle, the shot's particles and liquids may not have run as the
# client runs them unthrottled.
minimumFramesPerSecond = 100
# Calibration renders the viewport at most this wide (bridgeViews' frames reach 1920), its aspect kept.
renderWidthLimit = 1440
# The client computes its projection in single precision; its view angles must give the dump's scales to this share.
projectionTolerance = 1e-5
# Shots are taken after `#set weather 0`; the dump does not decode the client's weather (claudelink README, world.weather).
weatherCondition = "#set weather 0 before the shot; the client's current weather is not read"
# eqgame.exe 0x48ac00: the far clip runs from the zone's minimum clip to its maximum with the player's clip slider, at its maximum 20.
clipSliderMaximum = 20
# After NewZone the client raises a minimum clip under 50 to 50 (eqgame.exe 0x48ae80).
clientMinimumClip = 50.0
# The zone types whose scene ambient is the sky's (eqgame.exe 0x4942b0); the others' is a fixed gray, which previews do not draw yet.
skyLitZoneTypes = (1, 2, 5)
# Human, barbarian, and erudite see without infravision or ultravision: their vision floor is viewerLight's.
racesWithoutNightVision = (1, 2, 3)
# What the dump places is kept when it stands within this of the camera's view, so a model whose origin lies outside it still shows.
placementMargin = 100.0
blockSide = 32
# The difference image is amplified by this, and the block map's color ramp tops out at this difference (of 255).
differenceGain = 4
blockScaleTop = 64.0
sheetWidth = 1440
# Pixels the light fit trusts: drawn, with enough texture to light, and not clipped in the shot; a sample of them is fitted.
minimumBase = 0.06
maximumScreen = 0.98
minimumPixels = 1000
fitSamples = 20000
azimuthStepDegrees = 5.0
elevationStepDegrees = 5.0
fitRounds = 3
driftKeys = ("fogStart", "fogEnd", "fogDensity", "fogOn", "minClip", "maxClip", "newEngineZone", "zoneId")
# A spawn's look values the dump writes as -1 (a non-Drakkin's tattoo) the client reads past their count, which draws 0.
lookKeys = ("variation", "headType", "faceStyle", "hairStyle", "hairColor", "facialHair", "facialHairColor", "eyeColor1", "heritage", "tattoo", "details")
settingsMemoryKeys = (
  "gamma", "sky", "lod", "clipPlane", "lodBias", "fogScale", "mipMapping", "filterOff", "showDynamicLights", "showGrass", "showSpellEffects",
  "environmentParticleDensity", "environmentParticleOpacity", "environmentParticleNearClipPlane",
)
settingsINIKeys = (
  "TextureQuality", "TerrainTextureQuality", "VertexShaders", "20PixelShaders", "14PixelShaders", "1xPixelShaders", "PostEffects", "Bloom",
  "Shadows", "UseLitBatches", "WaterSwap", "SkyCubemapSize", "MultiPassLighting",
)


def readFormatted(path, expected):
  try:
    document = json.loads(Path(path).read_text(encoding="ascii"))
  except (OSError, ValueError) as error:
    raise ValueError(f"{path} cannot be read: {error}") from error
  if (document.get("format"), document.get("version")) != expected:
    raise ValueError(f"{path} is not {expected[0]} version {expected[1]}: format {document.get('format')!r}, version {document.get('version')!r}")
  return document


def samePath(first, second):
  return first is not None and os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def readShot(screenshotPath):
  """A claudelink shot's sidecar and dump, refused unless they pair with the PNG and each other (paths and capture time), the client ran
  at minimumFramesPerSecond or more with MacroQuest's frame limiter idle, and the PNG is the client area the sidecar records."""
  png = Path(screenshotPath)
  if not png.is_file():
    raise ValueError(f"No screenshot at {png}")
  sidecarPath = png.with_suffix(".json")
  if not sidecarPath.is_file():
    raise ValueError(f"{png.name} has no claudelink sidecar at {sidecarPath}: calibration takes only claudelink shots")
  sidecar = readFormatted(sidecarPath, sidecarFormat)
  if not Path(sidecar["dump"]).is_file():
    raise ValueError(f"The dump {sidecar['dump']} that {sidecarPath.name} names is missing")
  dump = readFormatted(sidecar["dump"], dumpFormat)
  refusals = []
  if not samePath(sidecar["png"], png):
    refusals.append(f"the sidecar names the PNG {sidecar['png']}")
  origin = dump["origin"]
  if origin["command"] != "screenshot" or not samePath(origin.get("screenshot"), png) or not samePath(origin.get("sidecar"), sidecarPath):
    refusals.append(f"the dump was taken for {origin}, not this shot")
  if sidecar["capturedAt"] != dump["capturedAt"]:
    refusals.append(f"the sidecar was captured at {sidecar['capturedAt']} and the dump at {dump['capturedAt']}")
  if sidecar["fps"] < minimumFramesPerSecond:
    refusals.append(f"the client ran at {sidecar['fps']:.1f} fps, under {minimumFramesPerSecond}")
  if sidecar["frameLimiter"]["activeDuringSettle"]:
    refusals.append("MacroQuest's frame limiter was throttling the client while the shot settled")
  with Image.open(png) as image:
    size = image.size
  window = sidecar["window"]
  if size != (window["clientWidth"], window["clientHeight"]):
    refusals.append(f"the PNG is {size[0]}x{size[1]}, not the {window['clientWidth']}x{window['clientHeight']} client area the sidecar records")
  for key in ("window", "viewport"):
    if sidecar[key] != dump[key]:
      refusals.append(f"the sidecar's {key} {sidecar[key]} differs from the dump's {dump[key]}")
  if any(sidecar["camera"][key] != dump["camera"][key] for key in sidecar["camera"]):
    refusals.append(f"the sidecar's camera {sidecar['camera']} differs from the dump's")
  if refusals:
    raise ValueError(f"{png.name} cannot calibrate: " + "; ".join(refusals))
  return sidecar, dump


def blenderPoint(record):
  """A dump's y, x, z (the client's /loc order, the server's axes) in Blender's (the zone files') axes."""
  return eqAxes.zoneFromServer([record["x"], record["y"], record["z"]]).tolist()


def cameraForward(camera):
  if camera["roll"] != 0:
    raise ValueError(f"The camera rolls {camera['roll']}/512 of a turn; which way the client rolls its view is not traced")
  turn = math.radians(eqAxes.turnFromEQHeading(camera["heading"]))
  pitch = math.radians(camera["pitch"] * 360 / eqAxes.eqHeadingUnits)
  return [math.cos(pitch) * math.cos(turn), math.cos(pitch) * math.sin(turn), math.sin(pitch)]


def cameraView(camera):
  """The dump's camera as an {eye, target} view: the eye where the camera stands, looking along its heading (512ths of a turn, the turn
  a model facing +X takes, eqAxes) and pitch (512ths, up positive), the target a unit ahead."""
  eye = blenderPoint(camera)
  forward = cameraForward(camera)
  return {"eye": eye, "target": [eye[axis] + forward[axis] for axis in range(3)]}


def clientFrame(camera, viewport):
  """The frame the client renders the viewport at, at no more than renderWidthLimit across: its aspect, and the vertical field of
  view its half view angle and viewAngleRatio give (previewFrame), checked against the projection scales the client computed."""
  width, height = viewport["maxX"] - viewport["minX"], viewport["maxY"] - viewport["minY"]
  if (camera["renderWidth"], camera["renderHeight"]) != (width, height):
    raise ValueError(f"The camera renders {camera['renderWidth']}x{camera['renderHeight']} into a {width}x{height} viewport")
  aspect = camera["viewAngleRatio"]
  if abs(aspect - width / height) > projectionTolerance * aspect:
    raise ValueError(f"The camera's viewAngleRatio {aspect} is not the viewport's aspect {width / height}")
  scaleX = 1 / math.tan(camera["halfViewAngle"] * 2 * math.pi / previewFrame.anglesPerTurn)
  for name, expected in (("projectionScaleX", scaleX), ("projectionScaleY", scaleX * aspect)):
    if abs(camera[name] - expected) > projectionTolerance * expected:
      raise ValueError(f"The client's {name} {camera[name]} is not the {expected:.9g} its half view angle and viewAngleRatio give")
  scale = min(1.0, renderWidthLimit / width)
  return {"size": [round(width * scale), round(height * scale)], "verticalFieldOfViewDegrees": previewFrame.verticalFieldOfView(camera["halfViewAngle"], aspect)}


def viewportPixels(path, viewport, size):
  """The shot cropped to the client's 3D viewport and resized to the render's size, raw values 0-1, rows top to bottom."""
  with Image.open(path) as image:
    cropped = image.convert("RGB").crop((viewport["minX"], viewport["minY"], viewport["maxX"], viewport["maxY"]))
  return numpy.asarray(cropped.resize(tuple(size), Image.LANCZOS), dtype=numpy.float64) / 255


def renderPixels(path):
  """A rendered view's pixels, raw 0-1, rows top to bottom."""
  with Image.open(path) as image:
    return numpy.asarray(image.convert("RGB"), dtype=numpy.float64) / 255


def playerSpawn(dump):
  matches = [spawn for spawn in dump["spawns"] if spawn["spawnId"] == dump["player"]["spawnId"]]
  if len(matches) != 1:
    raise ValueError(f"The dump holds {len(matches)} spawns with the player's id {dump['player']['spawnId']}")
  return matches[0]


def viewer(dump):
  """The special ambient the client gives the viewer (viewerLight) from the share of scene light its actor holds in the dump."""
  race = playerSpawn(dump)["look"]["race"]
  if race not in racesWithoutNightVision:
    raise ValueError(f"The viewer is race {race}; the vision floor of a race with infravision or ultravision is not drawn yet")
  share = dump["player"]["actor"]["sceneLightShare"]
  return {"race": race, "share": share, "specialAmbientColor": viewerLight.specialAmbient(share)}


def carriedLight(dump):
  """The light the viewer carries (clientPointLights.carriedLightAt) where the client put it, or None for light type 0."""
  light = dump["player"]["light"]
  if light["type"] == 0:
    return None
  if light["type"] not in clientPointLights.carriedLightTypes:
    raise ValueError(f"The viewer carries light type {light['type']}, which the client defines no light for")
  definition = clientPointLights.carriedLightTypes[light["type"]][0]
  if light["definition"]["tag"].lower() != definition.lower():
    raise ValueError(f"The viewer's light of type {light['type']} has the client's definition {light['definition']['tag']}, not {definition}")
  return clientPointLights.carriedLightAt(light["type"], blenderPoint(light), light["radius"], dump["world"]["zoneHeader"]["zoneId"])


def zoneProperties(dump, specialAmbientColor):
  """The zone properties the dump's zone header, clock, and sky give the preview (setZoneProperties): the sky at the game time, the
  header's first fog set, density, and fog switch, its clips, newEngineZone, and zone id, and the viewer's special ambient."""
  header, camera, world = dump["world"]["zoneHeader"], dump["camera"], dump["world"]
  if header["zoneType"] not in skyLitZoneTypes:
    raise ValueError(f"Zone type {header['zoneType']} lights the scene with a fixed gray, not the sky's ambient; previews do not draw that yet")
  if header["sky"] == 0 or not world["sky"]["enabled"]["value"] or not world["sky"]["render"]["value"]:
    raise ValueError(f"The client draws no sky here (header sky {header['sky']}, Sky/Enabled {world['sky']['enabled']['value']}, Sky/Render {world['sky']['render']['value']}); a shot without one is not drawn yet")
  slider = dump["graphicsSettings"]["memory"]["clipPlane"]
  if slider != clipSliderMaximum or camera["farClip"] != header["maxClip"]:
    raise ValueError(f"The clip slider is {slider} and the far clip {camera['farClip']}; previews draw the far clip at the zone's maximum, {header['maxClip']} (slider {clipSliderMaximum})")
  fog = header["fogSets"][0]
  return {
    "sky": {"type": world["sky"]["type"]["value"], "hour": world["time"]["hour"], "minute": world["time"]["minute"]},
    "fogStart": float(fog["start"]), "fogEnd": float(fog["end"]), "fogDensity": header["fogDensity"], "fogOn": header["fogOnOff"] != 0,
    "minClip": max(float(header["minClip"]), clientMinimumClip), "maxClip": float(header["maxClip"]), "newEngineZone": bool(header["newEngineZone"]),
    "shortName": dump["zone"]["shortName"], "zoneId": header["zoneId"], "specialAmbientColor": specialAmbientColor,
  }


def zoneDrift(stored, updates):
  """Where a work file's stored zone values (getZoneProperties) differ from the dump's (zoneProperties): the sky type and each of
  driftKeys, with the value the file holds (None where it holds none) and the dump's; empty when none differ."""
  skyType = None if "sky" not in stored else stored["sky"] if stored["sky"] == skyDrawing.noSky else stored["sky"]["type"]
  pairs = [("skyType", skyType, updates["sky"]["type"])] + [(key, stored[key] if key in stored else None, updates[key]) for key in driftKeys]
  return {key: {"stored": storedValue, "dump": dumpValue} for key, storedValue, dumpValue in pairs if storedValue != dumpValue}


def spawnAppearance(look):
  return {key: max(int(look[key]), 0) for key in lookKeys} | {"textureSet": max(int(look["textureType"]), 0)}


def dumpPlacements(dump):
  """What the dump shows standing in its zone as the client draws it: each spawn but the viewer, visible door, ground item, and placed
  object, at its actor's position, heading, and scale in the client's scene graph, with the model the actor draws; and those that
  cannot be placed, with why (no actor drawn, hidden, or tilted, which placement does not turn)."""
  actors = {actor["address"]: actor for actor in dump["engine"]["actors"]["list"]}
  viewerID = dump["player"]["spawnId"]
  records = [("spawn", spawn, f"spawn{spawn['spawnId']}_{spawn['name']}") for spawn in dump["spawns"] if spawn["spawnId"] != viewerID]
  records += [("door", door, f"door{door['id']}_{door['name']}") for door in dump["doors"]]
  records += [("groundItem", item, f"groundItem{item['dropId']}_{item['name']}") for item in dump["groundItems"]]
  records += [("placedObject", placed, f"placedObject{placed['recordNum']}_{placed['name']}") for placed in dump["placedObjects"]]
  placements, notPlaced = [], []
  for kind, record, name in records:
    actor = actors.get(record["actor"])
    if actor is None:
      notPlaced.append({"kind": kind, "name": name, "reason": f"no actor in the client's scene graph ({record['actor']}): the client draws none"})
      continue
    if kind == "door" and not record["visible"]:
      notPlaced.append({"kind": kind, "name": name, "reason": "hidden: the door is not visible"})
      continue
    if actor["pitch"] != 0 or actor["roll"] != 0:
      notPlaced.append({"kind": kind, "name": name, "reason": f"tilted (pitch {actor['pitch']}, roll {actor['roll']}): placement turns only about Z"})
      continue
    placement = {
      "kind": kind, "name": name, "model": actor["definition"], "position": blenderPoint(actor), "turnDegrees": eqAxes.turnFromEQHeading(actor["heading"]),
      "scale": actor["scale"],
    }
    if kind == "spawn":
      placement |= {"height": record["height"], "appearance": spawnAppearance(record["look"])}
    placements.append(placement)
  return placements, notPlaced


def withinView(positions, view, frame, farClip):
  """Which positions stand within placementMargin of what the view shows: in front of the eye, inside the frame's field, and inside
  the far clip."""
  eye = numpy.array(view["eye"], dtype=numpy.float64)
  forward = numpy.array(view["target"], dtype=numpy.float64) - eye
  forward /= numpy.linalg.norm(forward)
  right = numpy.cross(forward, (0.0, 0.0, 1.0))
  right /= numpy.linalg.norm(right)
  up = numpy.cross(right, forward)
  tangentUp = math.tan(math.radians(frame["verticalFieldOfViewDegrees"]) / 2)
  tangentAcross = tangentUp * frame["size"][0] / frame["size"][1]
  relative = numpy.asarray(positions, dtype=numpy.float64).reshape(-1, 3) - eye
  depth = relative @ forward
  return (
    (depth >= -placementMargin) & (numpy.linalg.norm(relative, axis=1) <= farClip + placementMargin)
    & (numpy.abs(relative @ right) - depth * tangentAcross <= placementMargin * math.hypot(1, tangentAcross))
    & (numpy.abs(relative @ up) - depth * tangentUp <= placementMargin * math.hypot(1, tangentUp))
  )


def conditions(dump, sidecar, sky, carried, viewed):
  """What the shot was taken under, as a run records it."""
  memory, ini = dump["graphicsSettings"]["memory"], dump["graphicsSettings"]["eqclientIni"]["defaults"]
  time = dump["world"]["time"]
  return {
    "gameTime": f"{time['hour']:02d}:{time['minute']:02d}", "weather": weatherCondition, "sky": sky,
    "carriedLight": None if carried is None else {key: carried[key] for key in ("name", "position", "color", "radius")},
    "viewer": viewed, "fps": round(sidecar["fps"], 1), "frameLimiter": sidecar["frameLimiter"],
    "settings": {"memory": {key: memory[key] for key in settingsMemoryKeys}, "eqclientIni": {key: ini[key] for key in settingsINIKeys if key in ini}},
    "client": dump["client"]["build"], "plugin": dump["client"]["plugin"], "character": dump["character"], "server": dump["server"],
  }


def rendererSource(serverDirectory):
  """A hash of the server's source, which holds the renderer: runs with the same hash drew with the same code."""
  digest = hashlib.sha256()
  for path in sorted(Path(serverDirectory).glob("*.py")):
    digest.update(path.name.encode("ascii") + b"\0" + path.read_bytes())
  return digest.hexdigest()


def blockMeans(perPixel):
  """The mean of each blockSide square (the last row and column of blocks cover what is left)."""
  rows = numpy.arange(0, perPixel.shape[0], blockSide)
  columns = numpy.arange(0, perPixel.shape[1], blockSide)
  sums = numpy.add.reduceat(numpy.add.reduceat(perPixel, rows, axis=0), columns, axis=1)
  counts = numpy.outer(numpy.diff(numpy.append(rows, perPixel.shape[0])), numpy.diff(numpy.append(columns, perPixel.shape[1])))
  return sums / counts


def toImage(pixels):
  return Image.fromarray(numpy.clip(pixels * 255 + 0.5, 0, 255).astype(numpy.uint8))


def blockMapImage(blocks, size):
  """The block means over the frame, black at 0 through red and yellow to white at blockScaleTop and over."""
  level = numpy.clip(blocks / blockScaleTop, 0, 1)
  colors = numpy.stack([numpy.clip(level * 3, 0, 1), numpy.clip(level * 3 - 1, 0, 1), numpy.clip(level * 3 - 2, 0, 1)], axis=-1)
  return toImage(colors).resize(tuple(size), Image.NEAREST)


def comparison(screen, render, drawn):
  """The shot against the render (both raw 0-1, rows top to bottom): the mean absolute difference (of 255) over the zone's drawn pixels,
  over the rest (the sky), and over all; the difference per block; and the pictures: the difference amplified differenceGain times,
  the block map, and a sheet of the shot beside the render over the two."""
  perPixel = numpy.abs(screen - render).mean(axis=2) * 255
  blocks = blockMeans(perPixel)
  height, width = perPixel.shape
  differenceImage = toImage(numpy.abs(screen - render) * differenceGain)
  blockImage = blockMapImage(blocks, (width, height))
  sheet = Image.new("RGB", (2 * width, 2 * height))
  for index, picture in enumerate((toImage(screen), toImage(render), differenceImage, blockImage)):
    sheet.paste(picture, ((index % 2) * width, (index // 2) * height))
  sheet = sheet.resize((sheetWidth, round(sheetWidth * height / width)), Image.LANCZOS)
  numbers = {
    "meanPixelDifference": round(float(perPixel[drawn].mean()), 2) if drawn.any() else None,
    "meanSkyDifference": round(float(perPixel[~drawn].mean()), 2) if (~drawn).any() else None,
    "meanDifference": round(float(perPixel.mean()), 2), "drawnShare": round(float(drawn.mean()), 4),
    "blocks": {"side": blockSide, "rows": blocks.shape[0], "columns": blocks.shape[1], "median": round(float(numpy.median(blocks)), 2), "largest": round(float(blocks.max()), 2)},
  }
  return numbers, blocks, {"difference": differenceImage, "blocks": blockImage, "sheet": sheet}


def scenePixels(screen, passes):
  """The pixels the light fit trusts (drawn, with enough texture to light, and not clipped in the shot), sampled: the shot's color and
  the passes' texture, unit normal, baked light, share of scene light, point light, and fog visibility at each."""
  base, normal = passes["base"], passes["normal"]
  valid = (passes["lit"][..., 3] > 0.5) & (base[..., :3].min(axis=2) > minimumBase) & (screen.max(axis=2) < maximumScreen)
  normals = normal[..., :3][valid] * 2 - 1
  lengths = numpy.linalg.norm(normals, axis=1)
  keep = numpy.flatnonzero(lengths > 0.5)
  if len(keep) < minimumPixels:
    raise ValueError(f"Only {len(keep)} usable pixels to fit lighting by")
  keep = numpy.random.default_rng(0).choice(keep, min(fitSamples, len(keep)), replace=False)
  return {
    "observed": screen[valid][keep], "base": base[..., :3][valid][keep], "normal": normals[keep] / lengths[keep, None],
    "baked": passes["baked"][..., :3][valid][keep], "share": passes["share"][..., 0][valid][keep], "pointLight": passes["pointLight"][..., :3][valid][keep],
    "visibility": passes["visibility"][..., 0][valid][keep],
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


def sceneTerms(pixels, direction):
  """The share of scene light each pixel takes of ambient, sun, and bounce from a sun direction: [n, 3]."""
  facing = pixels["normal"] @ direction
  share = pixels["share"]
  return numpy.stack([share, share * numpy.maximum(facing, 0), share * numpy.maximum(-facing, 0)], axis=1)


def knownLight(pixels, environment):
  return pixels["baked"] + pixels["pointLight"] + numpy.array(environment["specialAmbientColor"])


def drawnColors(pixels, colors, direction, environment):
  """The client's color for each pixel (bridgeClientLight): the light clamped to 0-1, times the texture held to 1, fogged."""
  light = numpy.clip(knownLight(pixels, environment) + sceneTerms(pixels, direction) @ colors, 0, 1)
  lit = numpy.minimum(pixels["base"] * light, 1)
  visibility = pixels["visibility"][:, None]
  return visibility * lit + (1 - visibility) * numpy.array(environment["fogColor"])


def residual(pixels, colors, direction, environment):
  """The root mean square error colors (rows ambient, sun, bounce) and a sun direction leave under the client's formula."""
  return math.sqrt(float(((drawnColors(pixels, colors, direction, environment) - pixels["observed"]) ** 2).mean()))


def solveColors(pixels, unclamped, direction, environment):
  """The ambient, sun, and bounce colors (rows) that best explain the unclamped pixels for one sun direction, each channel solved by
  least squares, where the light is linear in them, and the root mean square error they leave there."""
  terms = sceneTerms(pixels, direction)[unclamped]
  known = knownLight(pixels, environment)[unclamped]
  visibility = pixels["visibility"][unclamped]
  colors, squared = [], 0.0
  for channel in range(3):
    base = visibility * pixels["base"][unclamped, channel]
    design = terms * base[:, None]
    target = pixels["observed"][unclamped, channel] - base * known[:, channel] - (1 - visibility) * environment["fogColor"][channel]
    coefficients = nonNegativeSolve(design.T @ design, design.T @ target)
    colors.append(coefficients)
    squared += float(((design @ coefficients - target) ** 2).sum())
  return numpy.array(colors).T, math.sqrt(squared / (3 * max(int(unclamped.sum()), 1)))


def rounded(values):
  return [round(float(value), 4) for value in values]


def fitLight(screen, passes, environment):
  """The ambient, sun, and bounce colors and sun direction that best explain the shot under the client's formula, the rest of the light
  (baked, point, special ambient) and the fog taken from the prediction's environment: for each sun direction above the horizon (a sun
  lights as a bounce from the opposite one does), the colors solved by least squares per channel over the pixels the light does not
  clamp, which the fit then finds again from its own colors, fitRounds times. Light that clamps everywhere leaves the colors
  unmeasured. Reported beside the prediction's own values, each with the residual it leaves over every trusted pixel."""
  pixels = scenePixels(screen, passes)
  unclamped = numpy.ones(len(pixels["share"]), dtype=bool)
  best = None
  for _ in range(fitRounds):
    best = None
    for elevation in numpy.arange(0, 85 + elevationStepDegrees / 2, elevationStepDegrees):
      for azimuth in numpy.arange(0, 360, azimuthStepDegrees):
        direction = towardSun(azimuth, elevation)
        colors, linearResidual = solveColors(pixels, unclamped, direction, environment)
        if best is None or linearResidual < best["linearResidual"]:
          best = {"azimuth": float(azimuth), "elevation": float(elevation), "direction": direction, "colors": colors, "linearResidual": linearResidual}
    light = knownLight(pixels, environment) + sceneTerms(pixels, best["direction"]) @ best["colors"]
    clamped = (light >= 1).any(axis=1)
    if clamped.all():
      break
    unclamped = ~clamped
  ambient, sun, bounce = best["colors"]
  predictedColors = numpy.array([environment["ambientColor"], environment["sunColor"], environment["bounceColor"]])
  predictedDirection = towardSun(environment["sunAzimuthDegrees"], environment["sunElevationDegrees"])
  return {
    "pixels": len(pixels["share"]), "unclampedPixels": int(unclamped.sum()),
    "fitted": {
      "ambientColor": rounded(ambient), "sunColor": rounded(sun), "bounceColor": rounded(bounce), "sunAzimuthDegrees": best["azimuth"],
      "sunElevationDegrees": best["elevation"], "residual": round(residual(pixels, best["colors"], best["direction"], environment), 4),
    },
    "predicted": {key: environment[key] for key in ("ambientColor", "sunColor", "bounceColor", "sunAzimuthDegrees", "sunElevationDegrees")} | {
      "residual": round(residual(pixels, predictedColors, predictedDirection, environment), 4),
    },
  }


def latestResults(calibrationRoot):
  """Each shot's latest run (calibrateShot's or compareShot's): when, what it drew (its source's kind and path), the game time, the mean
  differences, and the renderer source it drew with."""
  latest = []
  for shotFolder in sorted(path for path in calibrationRoot.glob("*") if path.is_dir()) if calibrationRoot.is_dir() else []:
    runs = sorted(shotFolder.glob("*/result.json"))
    if runs:
      result = json.loads(runs[-1].read_text(encoding="utf-8"))
      latest.append({
        "shot": result["shot"], "zone": result["zone"], "run": result["run"], "runs": len(runs),
        "source": {key: result["source"][key] for key in ("kind", "path")}, "gameTime": result["conditions"]["gameTime"],
        "meanPixelDifference": result["comparison"]["meanPixelDifference"], "meanSkyDifference": result["comparison"]["meanSkyDifference"],
        "rendererSource": result["rendererSource"][:12],
      })
  return latest
