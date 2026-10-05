"""The client's sky from Resources/Sky (sky.ini, weather.ini): which sky a zone gets and, at a time of day, its colors, sun and moon,
and the scene light it supplies, as EQGraphicsDX9.dll and eqgame.exe evaluate them (docs/clientRendering.md, Sky). Clouds and stars
are not evaluated."""
import hashlib
import math

import numpy
from PIL import Image

import skyDrawing

indexSections = {
  "SkySetting": "skysettings", "ColorMap": "colormaps", "ColorSet": "colorsets", "SatelliteState": "satellitestates",
  "Satellite": "satellites", "WeatherPattern": "weatherpatterns",
}
# EQGraphicsDX9.dll's values for keys a sky setting leaves out (0x100c2420).
horizonDefaults = {"minangle": 0.0, "maxangle": -0.1, "minwidth": 0.1, "maxwidth": 0.1, "mincameraz": 0.0, "maxcameraz": 100.0}
satelliteDistanceDefault = 10.0
# Times and transitions are compared in whole 65536ths of a day (0x10026e60).
dayUnits = 65536
colorMapSide = 32
# Column 31 of a color map holds the scene's light colors (CSky's getters); rows 0-29 of column 0 the dome, row 30 the horizon band.
lightColumn = 31
lightRows = {"sun": 0, "moon": 1, "fog": 2, "ambient": 3, "sunBounce": 28, "moonBounce": 29}
domeRows = 30
horizonRow = 30
# CSky lights the scene by the moon before the first and after the second of these fractions of a day (EQGraphicsDX9.dll 0x100c20a0,
# single-precision constants 0x1013f984 and 0x1013f988: 5:55 and 18:20), and eqgame.exe takes the time as minutes times 1/1440
# (0x496b8d), both in single precision.
moonBefore = numpy.float32(0.2465277761220932)
moonAfter = numpy.float32(0.7638888955116272)
minuteOfDay = numpy.float32(0.0006944444612599909)
# eqgame.exe raises each channel of the ambient it hands the scene to the viewer's floor (0x494461-0x49471d): 0.08 for a character
# without infravision or ultravision.
ambientFloor = 0.08


def readProfile(path):
  """An ini file as the client reads it: section and key names ignore case, and the first of a repeated section or key wins."""
  sections = {}
  current = None
  for line in path.read_text(encoding="latin-1").splitlines():
    line = line.strip()
    if line.startswith("[") and line.endswith("]"):
      name = line[1:-1].strip().lower()
      current = None if name in sections else sections.setdefault(name, {})
    elif current is not None and "=" in line and not line.startswith(("#", ";")):
      key, value = line.split("=", 1)
      current.setdefault(key.strip().lower(), value.strip())
  return sections


class SkyFiles:
  """sky.ini and weather.ini as the client loads them: an object exists only when its kind's index section lists it and its own
  [Kind-Name] section exists; anything else the client skips."""

  def __init__(self, clientRoot):
    weatherOverride = readProfile(clientRoot / "eqclient.ini").get("defaults", {}).get("weatherini")
    if weatherOverride:
      raise ValueError(f"eqclient.ini sets WeatherINI={weatherOverride}; how the client resolves that path is not known")
    self.folder = clientRoot / "Resources" / "Sky"
    self.sections = readProfile(self.folder / "sky.ini") | readProfile(self.folder / "weather.ini")

  def find(self, kind, name):
    if name.lower() not in self.sections.get(indexSections[kind], {}):
      return None
    return self.sections.get(f"{kind}-{name}".lower())

  def section(self, skyType):
    """The sky setting the client draws for a sky type, lowercased: the type's own when sky.ini has a usable one, else 'default'."""
    for name in (skyType, "default"):
      setting = self.find("SkySetting", name)
      if setting is not None and setting.get("defaultweather"):
        return name.lower()
    raise ValueError("sky.ini has no usable [SkySetting-default], so the client draws no sky for this type")

  def require(self, kind, name, referrer):
    section = self.find(kind, name)
    if section is None:
      raise ValueError(f"{referrer} names {kind} '{name}', which the client does not load: it needs a [{kind}-{name}] section listed in its index section")
    return section


def requireValue(section, key, sectionName):
  if not section.get(key):
    raise ValueError(f"[{sectionName}] has no {key}")
  return section[key]


def timeNodes(section, valueKey, sectionName):
  """A time-keyed list's nodes in file order: (start, transition) in day units and the value named."""
  nodes = []
  while f"{valueKey}{len(nodes)}" in section:
    index = len(nodes)
    start = int(float(requireValue(section, f"time{index}", sectionName)) * dayUnits)
    transition = int(float(requireValue(section, f"transition{index}", sectionName)) * dayUnits)
    nodes.append((start, transition, section[f"{valueKey}{index}"]))
  if not nodes:
    raise ValueError(f"[{sectionName}] has no {valueKey}0")
  return nodes


def nodesAt(nodes, time):
  """The node in effect at a time (the last whose start is at or before it, or before the first the last) and, inside its
  transition, the node it fades from and the fade's weight 0-255 on the new one (0x10026e60)."""
  started = [index for index, node in enumerate(nodes) if node[0] <= time]
  current = started[-1] if started else len(nodes) - 1
  start, transition, value = nodes[current]
  if transition > 0 and start <= time < start + transition:
    return value, nodes[current - 1][2], (time - start) * 255 // transition
  return value, None, 255


def readColorMap(files, name, referrer):
  section = files.require("ColorMap", name, referrer)
  path = files.folder / f"ColorMap-{requireValue(section, 'file', 'ColorMap-' + name)}.dds"
  with Image.open(path) as image:
    pixels = numpy.asarray(image.convert("RGBA"), dtype=numpy.int32)
  if pixels.shape != (colorMapSide, colorMapSide, 4):
    raise ValueError(f"{path.name} is {pixels.shape[1]}x{pixels.shape[0]}; a color map is {colorMapSide}x{colorMapSide}")
  return pixels


def evaluateColorSet(files, name, time, referrer):
  """The color set's map at a time; inside a transition the two maps cross-fade in 8 bits, (new * w + old * (255 - w)) >> 8 (0x1002e8e0)."""
  section = files.require("ColorSet", name, referrer)
  newMap, oldMap, weight = nodesAt(timeNodes(section, "colormap", "ColorSet-" + name), time)
  newPixels = readColorMap(files, newMap, "ColorSet-" + name)
  if oldMap is None:
    return newPixels, [newMap]
  return (newPixels * weight + readColorMap(files, oldMap, "ColorSet-" + name) * (255 - weight)) >> 8, [oldMap, newMap]


def satelliteTexture(files, textureName, cachePath):
  """A satellite texture as an RGBA uint8 array (rows from the top) under the cache, named by its bytes."""
  source = files.folder / f"Satellite-{textureName}.dds"
  data = source.read_bytes()
  arrayPath = cachePath / f"{source.stem}-{hashlib.sha256(data).hexdigest()[:16]}.npy"
  if not arrayPath.is_file():
    cachePath.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
      numpy.save(arrayPath, numpy.asarray(image.convert("RGBA"), dtype=numpy.uint8))
  return str(arrayPath)


def satelliteDirection(angle, heading):
  """Toward a satellite at a turn angle: -(sin H sin a, cos H sin a, cos a); with angle 2 pi t it rises at -Y, crosses the zenith at
  noon, and sets at +Y."""
  return numpy.array([-math.sin(heading) * math.sin(angle), -math.cos(heading) * math.sin(angle), -math.cos(angle)])


def evaluateSatellite(files, name, time, dayFraction, cachePath, referrer):
  section = files.require("Satellite", name, referrer)
  sectionName = "Satellite-" + name
  if section.get("rotates", "1") != "1":
    raise ValueError(f"[{sectionName}] does not rotate, which the preview does not draw")
  heading = float(section.get("heading", "0"))
  distance = float(section.get("distance", satelliteDistanceDefault))
  newState, oldState, weight = nodesAt(timeNodes(section, "satellite", sectionName), time)
  states = [(newState, weight / 255)] + ([(oldState, 1 - weight / 255)] if oldState is not None else [])
  size = 0.0
  textures = []
  for stateName, share in states:
    state = files.require("SatelliteState", stateName, sectionName)
    size += float(requireValue(state, "size", "SatelliteState-" + stateName)) * share
    textures.append({"state": stateName, "path": satelliteTexture(files, requireValue(state, "texture", "SatelliteState-" + stateName), cachePath), "weight": round(share, 4)})
  toward = satelliteDirection(2 * math.pi * dayFraction + float(requireValue(section, "angle", sectionName)), heading)
  # The quad's turn about its own axis is not traced; it is laid square to the satellite's path.
  pathNormal = numpy.array([math.cos(heading), -math.sin(heading), 0.0])
  up = numpy.cross(toward, pathNormal)
  return {
    "name": name, "direction": toward.round(6).tolist(), "up": up.round(6).tolist(), "right": numpy.cross(toward, up).round(6).tolist(),
    "halfExtent": size / distance, "textures": textures,
  }


def colorOf(pixel):
  return [round(int(component) / 255, 4) for component in pixel[:3]]


def lightsByMoon(hour, minute):
  """Whether CSky lights the scene by the moon at a time (0x100c20a0), from the day fraction as eqgame.exe computes it."""
  fraction = numpy.float32(float(hour * 60 + minute) * float(minuteOfDay))
  return bool(fraction < moonBefore or fraction > moonAfter)


def directionAngles(direction):
  return round(math.degrees(math.atan2(direction[0], direction[1])) % 360, 3), round(math.degrees(math.asin(max(-1.0, min(1.0, direction[2])))), 3)


def skyState(clientRoot, cachePath, sky):
  """What the client draws for a sky {type, weather (the type's DefaultWeather when left out), hour, minute} and the scene light it
  sets: ambient (raised to the floor of a character without infravision or ultravision), fog color, the directional light's color and
  direction (the sun's, or before 5:55 and after 18:20 the moon's), and its bounce. hour and minute place the sun: t = (hour * 60 +
  minute) / 1440 of a day, the sun at its highest at 12:00."""
  hour, minute = sky["hour"], sky["minute"]
  files = SkyFiles(clientRoot)
  chain = []
  settingName = files.section(sky["type"])
  setting = files.find("SkySetting", settingName)
  if settingName != sky["type"].lower():
    chain.append(f"sky.ini has no sky type '{sky['type']}', so the client uses 'default'")
  weatherName = sky.get("weather") or setting["defaultweather"]
  chain.append(f"SkySetting-{settingName} -> WeatherPattern-{weatherName}" + ("" if sky.get("weather") else " (its DefaultWeather)"))
  pattern = files.require("WeatherPattern", weatherName, "SkySetting-" + settingName)
  dayFraction = (hour * 60 + minute) / 1440
  time = int(dayFraction * dayUnits)
  colorSet = requireValue(pattern, "colorset", "WeatherPattern-" + weatherName)
  colors, maps = evaluateColorSet(files, colorSet, time, "WeatherPattern-" + weatherName)
  chain.append(f"ColorSet-{colorSet} -> " + " fading to ".join(f"ColorMap-{name}" for name in maps))
  satellites = []
  for role in ("sun", "moon"):
    if pattern.get(role):
      satellites.append(evaluateSatellite(files, pattern[role], time, dayFraction, cachePath, "WeatherPattern-" + weatherName))
  horizonValues = {key: float(setting.get(key, default)) for key, default in horizonDefaults.items()}
  if horizonValues["maxcameraz"] <= horizonValues["mincameraz"]:
    raise ValueError(f"[SkySetting-{settingName}] has MaxCameraZ {horizonValues['maxcameraz']} not above MinCameraZ {horizonValues['mincameraz']}")
  horizon = None
  if setting.get("renderhorizon", "1") != "0":
    band = colors[horizonRow, 0]
    horizon = {
      "color": colorOf(band), "alpha": round(int(band[3]) / 255, 4), "minAngle": horizonValues["minangle"], "maxAngle": horizonValues["maxangle"],
      "minWidth": horizonValues["minwidth"], "maxWidth": horizonValues["maxwidth"], "minCameraZ": horizonValues["mincameraz"], "maxCameraZ": horizonValues["maxcameraz"],
    }
  sun = satelliteDirection(2 * math.pi * dayFraction, 0.0)
  # By the moon, eqgame.exe lights from the moon's angle, the sun's plus half a turn (0x496f9a, 0x496bc7): the sun's direction reversed.
  byDay = not lightsByMoon(hour, minute)
  light = colors[:, lightColumn]
  azimuth, elevation = directionAngles(sun if byDay else -sun)
  environment = {
    "ambientColor": [max(value, ambientFloor) for value in colorOf(light[lightRows["ambient"]])], "fogColor": colorOf(light[lightRows["fog"]]),
    "sunColor": colorOf(light[lightRows["sun" if byDay else "moon"]]), "bounceColor": colorOf(light[lightRows["sunBounce" if byDay else "moonBounce"]]),
    "sunAzimuthDegrees": azimuth, "sunElevationDegrees": elevation,
  }
  assert set(environment) == set(skyDrawing.suppliedZoneKeys)
  return {
    "chain": chain, "dayFraction": round(dayFraction, 6), "lightFrom": "sun" if byDay else "moon", "environment": environment,
    "dome": {"axis": sun.round(6).tolist(), "colors": [colorOf(pixel) for pixel in colors[:domeRows, 0]]},
    "horizon": horizon, "satellites": satellites,
  }
