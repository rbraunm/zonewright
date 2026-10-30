"""Synthetic claudelink shots for calibration tests: a window PNG, its sidecar (claudelinkScreenshot 1), and the zone dump taken in the
same frame (claudelinkZoneDump 1), laid out as claudelink's screenshot writes them (its README, The screenshot and The dump)."""
import json
import math

from PIL import Image

capturedAt = "2026-10-12T00:55:09.082Z"
windowPadding = 70
graphicsMemory = {
  "gamma": 7, "gammaScale": 0.1, "sky": 2, "lod": True, "pcNames": 1, "npcNames": 1, "realism": 1, "mipMapping": 1, "filterOff": 0, "clipPlane": 20,
  "maxFPS": 100, "maxBGFPS": 100, "lodBias": 10, "fogScale": 4, "showDynamicLights": 1, "showGrass": True, "showSpellEffects": 1, "showNamesLevel": 4,
  "environmentParticleDensity": 1, "environmentParticleOpacity": 1, "environmentParticleNearClipPlane": 8,
}


def actor(address, definition, position, heading, scale, pitch=0, roll=0):
  """A scene graph actor at a server-axes position (y, x, z)."""
  y, x, z = position
  return {"address": address, "class": "CSimpleActor", "y": y, "x": x, "z": z, "heading": heading, "pitch": pitch, "roll": roll, "scale": scale, "definition": definition}


def spawnRecord(spawnID, name, actorAddress, position, heading, race=1, actorDef="HUM_ACTORDEF", height=6.0):
  y, x, z = position
  look = {
    "race": race, "class": 1, "gender": 0, "actorDef": actorDef, "textureType": -1, "material": 0, "variation": 0, "headType": 0, "faceStyle": 0,
    "hairStyle": 0, "hairColor": 0, "facialHair": 255, "facialHairColor": 0, "eyeColor1": 0, "eyeColor2": 0, "showHelm": True, "heritage": 0,
    "tattoo": -1, "details": -1,
  }
  return {
    "spawnId": spawnID, "name": name, "type": 1, "y": y, "x": x, "z": z, "heading": heading, "avatarHeight": 3.75, "height": height, "light": 0,
    "actor": actorAddress, "look": look,
  }


def zoneDump(shortName, zoneID, viewportSize, camera, time, light, share, fogSet, maxClip, spawns=(), doors=(), groundItems=(), actors=(), fogOnOff=0, minClip=50):
  """A dump of a zone with a sky (zone type 1), its header's fog switch fogOnOff (0, off) and minClip, the viewer a human (spawn 1)
  carrying light {type, tag, position (y, x, z), radius}, its camera (y, x, z, heading, pitch) looking through a viewport of
  viewportSize at RoF2's half view angle."""
  width, height = viewportSize
  scaleX = 1 / math.tan(45 * 2 * math.pi / 512)
  window = {"clientLeft": 0, "clientTop": 23, "clientWidth": width + 2 * windowPadding, "clientHeight": height, "outerLeft": -8, "outerTop": -8, "outerRight": 0, "outerBottom": 0}
  viewport = {"minX": windowPadding, "minY": 0, "maxX": windowPadding + width, "maxY": height, "xScale": 0, "yScale": 0, "widthScale": 100, "heightScale": 100}
  lightY, lightX, lightZ = light["position"]
  viewerActor = actor("0x2", "HUM_ACTORDEF", (camera["y"], camera["x"], camera["z"] - 3.69), camera["heading"], 1.2)
  return {
    "format": "claudelinkZoneDump", "version": 1, "capturedAt": capturedAt, "origin": {"command": "screenshot"},
    "client": {"build": "May 10 2013 23:30:08", "layout": "May 10 2013 23:30:08", "processId": 1, "folder": "", "plugin": "MQClaudeLink test"},
    "server": "test", "character": "Viewer", "zone": {"shortName": shortName, "longName": shortName, "id": zoneID, "characterZoneId": zoneID, "instance": 0},
    "window": window, "viewport": viewport,
    "camera": camera | {
      "roll": 0, "halfViewAngle": 45, "viewAngleRatio": width / height, "nearClip": 1, "farClip": maxClip, "actorFarClip": 1040, "renderWidth": width,
      "renderHeight": height, "projectionScaleX": scaleX, "projectionScaleY": scaleX * width / height,
    },
    "player": {
      "spawnId": 1, "y": camera["y"], "x": camera["x"], "z": camera["z"] - 3.69, "heading": camera["heading"],
      "light": {"type": light["type"], "class": "CPointLight", "y": lightY, "x": lightX, "z": lightZ, "radius": light["radius"], "definition": {"class": "CLightDefinition", "tag": light["tag"]}},
      "actor": {"address": "0x2", "class": "CHierarchicalActor", "sceneLightShare": share},
    },
    "world": {
      "time": {"hour": time[0], "minute": time[1]},
      "sky": {"type": {"value": shortName, "kind": "string"}, "enabled": {"value": True, "kind": "bool"}, "render": {"value": True, "kind": "bool"}},
      "zoneHeader": {
        "shortName": shortName, "fogSets": [fogSet] * 4, "fogOnOff": fogOnOff, "zoneType": 1, "sky": 1, "minClip": minClip, "maxClip": maxClip, "newEngineZone": 0,
        "zoneId": zoneID, "fogDensity": 0.33,
      },
    },
    "graphicsSettings": {"memory": graphicsMemory, "eqclientIni": {"path": "", "defaults": {"PostEffects": "1", "Bloom": "1", "Shadows": "0"}}},
    "spawns": [spawnRecord(1, "Viewer000", "0x2", (camera["y"], camera["x"], camera["z"] - 3.69), camera["heading"])] + list(spawns),
    "doors": list(doors), "groundItems": list(groundItems), "placedObjects": [],
    "engine": {"actors": {"list": [viewerActor] + list(actors), "sceneGraphCount": 1 + len(actors)}},
    "problems": [],
  }


def writeShot(folder, name, viewportImage, dump, fps=150.0, limiterActive=False, windowSize=None):
  """The shot's PNG (the client area, black around the viewport), its sidecar, and its dump; returns the PNG's path."""
  folder.mkdir(parents=True, exist_ok=True)
  window, viewport = dump["window"], dump["viewport"]
  pngPath, sidecarPath, dumpPath = folder / f"{name}.png", folder / f"{name}.json", folder / f"{dump['zone']['shortName']}_{name}.dump.json"
  canvas = Image.new("RGB", windowSize or (window["clientWidth"], window["clientHeight"]))
  canvas.paste(viewportImage, (viewport["minX"], viewport["minY"]))
  canvas.save(pngPath)
  written = dump | {"origin": {"command": "screenshot", "screenshot": str(pngPath), "sidecar": str(sidecarPath)}}
  dumpPath.write_text(json.dumps(written), encoding="ascii")
  camera = {key: dump["camera"][key] for key in ("y", "x", "z", "heading", "pitch", "roll")}
  sidecar = {
    "format": "claudelinkScreenshot", "version": 1, "capturedAt": dump["capturedAt"], "png": str(pngPath), "dump": str(dumpPath),
    "width": window["clientWidth"], "height": window["clientHeight"], "hideUI": True, "fps": fps,
    "frameLimiter": {"suspendedByLink": True, "activeDuringSettle": limiterActive}, "frameCaps": {"foreground": 100, "background": 100},
    "camera": camera, "window": window, "viewport": viewport, "time": dump["world"]["time"],
  }
  sidecarPath.write_text(json.dumps(sidecar), encoding="ascii")
  return pngPath
