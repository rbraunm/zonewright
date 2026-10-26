"""Build the measurement zone (measurementLayout) with zonewright's own tools, export it as a game export under the tooling root, verify
it, render it, and write its layout beside the export for the claudelink walk scripts. Runs a copy of this repository's server against
the real tooling root and client (as the MCP server does), so the work file, the export, and the renders land where zonewright keeps
them:

  python measurementZone/buildMeasurementZone.py

The work file is %LOCALAPPDATA%\\zonewright\\measurementZone\\arttest.blend, the export %LOCALAPPDATA%\\zonewright\\staging\\arttest\\."""
import asyncio
import base64
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

import measurementLayout as layout

repositoryRoot = Path(__file__).resolve().parent.parent
toolingRoot = Path(os.environ["LOCALAPPDATA"]) / "zonewright"
workFile = toolingRoot / "measurementZone" / f"{layout.shortName}.blend"
stagingFolder = toolingRoot / "staging" / layout.shortName
archivePath = stagingFolder / f"{layout.shortName}.eqg"
everquestClient = os.environ.get("zonewrightEverQuestClient")
if not everquestClient:
  raise SystemExit("buildMeasurementZone needs zonewrightEverQuestClient, the EverQuest client folder, set in its shell")
textures = {
  "floor": "texture/di_hp_ground.dds@e7b128c3", "block": "texture/stonewall.dds@34d934e0", "ceiling": "texture/hp_floorboards_inta01_c.dds@f3aa5776",
  "label": "texture/di_sky_stone_plain2.dds@2906fc83", "bed": "texture/di_hp_cliff_underwater.dds@a096e39f",
  "water": "texture/rc_cavewater_c.dds@9505abaf", "waterNormal": "texture/rc_cavewater_n.dds@3c9eae01", "waterEnvironment": "texture/water_e.dds@e41de8c4",
}
labelSize = 2.4
labelCode = """
import math
import bpy
created = []
for name, text, location, size, rotation in labels:
  curve = bpy.data.curves.new(name, 'FONT')
  curve.body = text
  curve.size = size
  curve.align_x = 'CENTER'
  curve.align_y = 'CENTER'
  holder = bpy.data.objects.new(name + 'Text', curve)
  bpy.context.scene.collection.objects.link(holder)
  mesh = bpy.data.meshes.new_from_object(holder.evaluated_get(bpy.context.evaluated_depsgraph_get()))
  bpy.data.objects.remove(holder)
  bpy.data.curves.remove(curve)
  mesh.name = name
  label = bpy.data.objects.new(name, mesh)
  label.location = location
  label.rotation_euler = [math.radians(angle) for angle in rotation]
  bpy.context.scene.collection.objects.link(label)
  created.append(name)
result = created
"""


class Session:
  def __init__(self, client):
    self.client = client

  async def call(self, tool, arguments=None):
    result = await self.client.call_tool(tool, arguments or {})
    texts = [content.text for content in result.content if content.type == "text"]
    if result.is_error:
      raise RuntimeError(f"{tool} failed: {texts}")
    images = [base64.b64decode(content.data) for content in result.content if content.type == "image"]
    return json.loads(texts[-1]) if texts else None, images


def stagedServer(folder):
  """A copy of the server and the files it reads beside it, so edits to the repository while the build runs change nothing it runs."""
  for name in ("server", "recastHelper", "catalog"):
    shutil.copytree(repositoryRoot / name, folder / name, ignore=shutil.ignore_patterns("__pycache__"))
  shutil.copytree(repositoryRoot / ".claude" / "skills" / "zone-survey", folder / ".claude" / "skills" / "zone-survey")
  shutil.copy2(repositoryRoot / "toolingManifest.json", folder / "toolingManifest.json")
  return StdioServerParameters(
    command=sys.executable, args=[str(folder / "server" / "zonewrightServer.py")],
    env={"LOCALAPPDATA": os.environ["LOCALAPPDATA"], "zonewrightEverQuestClient": everquestClient, "SYSTEMROOT": os.environ["SYSTEMROOT"]},
  )


def cube(name, minimum, maximum):
  """createPrimitive arguments for a box between two corners (its origin at its base's center)."""
  size = [high - low for low, high in zip(minimum, maximum)]
  return {"kind": "cube", "name": name, "size": size, "location": [(minimum[0] + maximum[0]) / 2, (minimum[1] + maximum[1]) / 2, minimum[2]]}


def number(value):
  return f"{value:.2f}" if value != round(value) else f"{value:.0f}"


async def blocks(session, group, boxes, material):
  """Boxes made one by one and joined into one mesh named for the group, mapped and given a material."""
  names = []
  for index, (minimum, maximum) in enumerate(boxes):
    name = f"{group}{index:02d}"
    await session.call("createPrimitive", cube(name, minimum, maximum))
    names.append(name)
  if len(names) > 1:
    await session.call("joinObjects", {"names": names, "into": names[0]})
  await session.call("organize", {"renames": {names[0]: group}})
  await session.call("assignMaterial", {"objectName": group, "materialName": material})
  await session.call("projectUVs", {"objectName": group, "method": "box", "worldUnitsPerRepeat": 10.0})


async def build(session):
  await session.call("newFile", {"discardUnsavedChanges": True})
  extent = layout.extent
  center = [(extent["minimum"][axis] + extent["maximum"][axis]) / 2 for axis in (0, 1)]
  size = [extent["maximum"][axis] - extent["minimum"][axis] for axis in (0, 1)]
  for name, texture in textures.items():
    if name == "water":
      await session.call("createLiquidMaterial", {
        "name": "water", "liquid": "water", "diffuseTexture": texture, "normalTexture": textures["waterNormal"], "environmentTexture": textures["waterEnvironment"],
      })
    elif name not in ("waterNormal", "waterEnvironment"):
      await session.call("createMaterial", {"name": name, "diffuseTexture": texture})
  await session.call("createTerrainGrid", {"name": "floor", "size": size, "spacing": 2.0, "location": [*center, layout.floorZ], "collection": "terrain"})
  await session.call("assignMaterial", {"objectName": "floor", "materialName": "floor"})
  await session.call("projectUVs", {"objectName": "floor", "method": "planar", "worldUnitsPerRepeat": 12.0, "direction": [0, 0, 1]})

  lanes = layout.layout()["lanes"]
  risers = [lane for lane in lanes if lane["feature"] == "riser"]
  await blocks(session, "risers", [(lane["block"]["minimum"], lane["block"]["maximum"]) for lane in risers], "block")

  ramps = [lane for lane in lanes if lane["feature"] == "ramp"]
  for index, lane in enumerate(ramps):
    x, run, height, half = lane["foot"][0], lane["run"], lane["height"], lane["halfWidth"]
    wedge = f"ramp{index:02d}"
    await session.call("createPrimitive", cube(wedge, [x - half, layout.featureStartY, layout.floorZ], [x + half, layout.featureStartY + run, layout.floorZ + height]))
    await session.call("moveVertices", {
      "objectName": wedge, "selector": {"box": {"minimum": [x - half - 1, layout.featureStartY - 0.1, height - 0.1], "maximum": [x + half + 1, layout.featureStartY + 0.1, height + 0.1]}},
      "offset": [0, 0, -height],
    })
    await session.call("cleanupMesh", {"objectName": wedge})
    top = f"rampTop{index:02d}"
    await session.call("createPrimitive", cube(top, [x - half, layout.featureStartY + run, layout.floorZ], [x + half, lane["topEnd"], layout.floorZ + height]))
    await session.call("joinObjects", {"names": [wedge, top], "into": wedge})
  await session.call("joinObjects", {"names": [f"ramp{index:02d}" for index in range(len(ramps))], "into": "ramp00"})
  await session.call("organize", {"renames": {"ramp00": "ramps"}})
  await session.call("assignMaterial", {"objectName": "ramps", "materialName": "block"})
  await session.call("projectUVs", {"objectName": "ramps", "method": "box", "worldUnitsPerRepeat": 10.0})

  ceiling = next(lane for lane in lanes if lane["feature"] == "ceiling")
  cx, inner = ceiling["start"][0], ceiling["innerWidth"] / 2
  first, last = ceiling["sections"][0]["from"], ceiling["sections"][-1]["to"]
  walls = [([cx - inner - 2, first, layout.floorZ], [cx - inner, last, layout.wallHeight]), ([cx + inner, first, layout.floorZ], [cx + inner + 2, last, layout.wallHeight])]
  opening = next(lane for lane in lanes if lane["feature"] == "opening")
  ox, outer = opening["start"][0], opening["outerWidth"] / 2
  for section in opening["sections"]:
    gap = section["value"] / 2
    walls += [([ox - outer, section["from"], layout.floorZ], [ox - gap, section["to"], layout.wallHeight]), ([ox + gap, section["from"], layout.floorZ], [ox + outer, section["to"], layout.wallHeight])]
  await blocks(session, "walls", walls, "block")
  await blocks(session, "ceilings", [([cx - inner, section["from"], section["value"]], [cx + inner, section["to"], layout.wallHeight]) for section in ceiling["sections"]], "ceiling")

  pool = next(lane for lane in lanes if lane["feature"] == "pool")
  px, half = pool["start"][0], pool["width"] / 2
  await session.call("gradeRoute", {
    "objectName": "floor", "name": "poolBed", "points": [pool["bedFrom"], pool["bedTo"]], "width": pool["width"], "cutBatterDegrees": 80.0,
    "fillBatterDegrees": 45.0,
  })
  await session.call("floodWater", {
    "name": "pool", "seed": [px, (pool["bedFrom"][1] + pool["bedTo"][1]) / 2], "level": pool["surfaceZ"], "material": "water", "spacing": 2.0,
    "within": [[px - half - 2, pool["bedFrom"][1] - 2], [px + half + 2, pool["bedFrom"][1] - 2], [px + half + 2, pool["bedTo"][1] + 4], [px - half - 2, pool["bedTo"][1] + 4]],
  })
  # The box spans the whole pool, so where swimming starts is the client's choice, not the box's edge.
  await session.call("placeSwimVolume", {
    "name": "measurePool", "liquid": "water", "minimum": [px - half - 1, pool["bedFrom"][1] - 1, pool["bedTo"][2] - 4], "maximum": [px + half + 1, pool["bedTo"][1] + 1, pool["surfaceZ"]],
    "body": "pool",
  })
  await session.call("acceptSwimVolumes", {"body": "pool"})

  # Floor labels lie flat, read from the start line; a corridor's sections are labelled on its left wall's inner face, reading along
  # the corridor, as a walker passes them.
  flat, onLeftWall = [0, 0, 0], [90, 0, 90]
  labels = []
  for lane in risers + ramps:
    text = number(lane["value"]) if lane["feature"] == "riser" else f"{lane['value']}"
    labels.append((f"label{lane['feature']}{len(labels):03d}", text, [lane["start"][0], 6.0, layout.floorZ + 0.05], labelSize, flat))
  for section in ceiling["sections"]:
    labels.append((f"labelCeiling{len(labels):03d}", number(section["value"]), [cx - inner + 0.05, (section["from"] + section["to"]) / 2, layout.floorZ + 1.5], 1.2, onLeftWall))
  for section in opening["sections"]:
    labels.append((f"labelOpening{len(labels):03d}", number(section["value"]), [ox - section["value"] / 2 + 0.05, (section["from"] + section["to"]) / 2, layout.floorZ + 3.0], 1.4, onLeftWall))
  for mark in pool["labels"]:
    labels.append((f"labelPool{len(labels):03d}", number(mark["depth"]), [px - half - 4, mark["y"], layout.floorZ + 0.05], labelSize, flat))
  for lane, text in ((ceiling, "CEILING"), (opening, "OPENING"), (pool, "POOL")):
    labels.append((f"labelName{len(labels):03d}", text, [lane["start"][0], 6.0, layout.floorZ + 0.05], labelSize, flat))
  labels.append(("labelStart", "START", [layout.safePoint[0], -10.0, layout.floorZ + 0.05], 4.0, flat))
  created, _ = await session.call("runPython", {"code": f"labels = {json.dumps(labels)}\n" + labelCode})
  names = created["result"]
  await session.call("joinObjects", {"names": names, "into": names[0]})
  await session.call("organize", {"renames": {names[0]: "labels"}})
  await session.call("assignMaterial", {"objectName": "labels", "materialName": "label"})
  await session.call("projectUVs", {"objectName": "labels", "method": "box", "worldUnitsPerRepeat": 4.0})
  await session.call("markPassable", {"objects": ["labels"]})

  low, high = extent["minimum"], extent["maximum"]
  await session.call("placeBoundaryWall", {"name": "rim", "path": [[low[0] + 1, low[1] + 1], [high[0] - 1, low[1] + 1], [high[0] - 1, high[1] - 1], [low[0] + 1, high[1] - 1], [low[0] + 1, low[1] + 1]], "height": 60.0})
  await session.call("createRegion", {
    "name": "course", "outline": [[low[0], low[1]], [high[0], low[1]], [high[0], high[1]], [low[0], high[1]]], "bottom": layout.underworld, "top": 100.0,
    "intent": "measurement course: risers, ramps, a pool, stepped ceilings, and narrowing openings, each labelled with its value, walked from one start line",
    "access": "play",
  })
  await session.call("setZoneProperties", {
    "shortName": layout.shortName, "zoneId": layout.zoneID, "longName": layout.longName, "timeType": "outdoor", "entryGate": {"minStatus": 80},
    "serverTemplate": "highpasshold", "newEngineZone": False, "specialAmbientColor": [0.0, 0.0, 0.0], "sky": {"type": layout.shortName, "hour": 12, "minute": 0},
    "fogStart": 400.0, "fogEnd": 1500.0, "fogDensity": 0.33, "fogOn": False, "minClip": 450.0, "maxClip": 1500.0,
    "safePoint": layout.safePoint, "underworld": layout.underworld,
  })
  workFile.parent.mkdir(parents=True, exist_ok=True)
  await session.call("saveFile", {"path": str(workFile), "replaceExisting": True})


async def exportAndLook(session):
  stagingFolder.mkdir(parents=True, exist_ok=True)
  checked, _ = await session.call("checkExport", {"path": str(archivePath), "purpose": "game"})
  print(json.dumps({"failures": checked["failures"], "findingCount": len(checked["findings"])}, indent=1))
  exported, _ = await session.call("exportZone", {"path": str(archivePath), "purpose": "game"})
  verified, _ = await session.call("verifyExport", {"path": str(archivePath)})
  (stagingFolder / f"{layout.shortName}_layout.json").write_text(layout.layoutJSON(), encoding="utf-8")
  views = {
    "above": {"map": {"center": [(layout.extent["minimum"][0] + layout.extent["maximum"][0]) / 2, 111.0], "width": 860.0}},
    "start": {"standAt": [200.0, -20.0], "headingDegrees": 0.0, "pitchDegrees": -6.0},
    "risers": {"standAt": [80.0, -4.0], "headingDegrees": 0.0, "pitchDegrees": -12.0},
    "ramps": {"standAt": [275.0, -12.0], "headingDegrees": 0.0, "pitchDegrees": -4.0},
    "rampsSide": {"eye": [160.0, 30.0, 14.0], "target": [275.0, 22.0, 6.0]},
    "pool": {"standAt": [layout.poolCenterX, 4.0], "headingDegrees": 0.0, "pitchDegrees": -10.0},
    "ceilings": {"standAt": [layout.ceilingCenterX, 4.0], "headingDegrees": 0.0, "pitchDegrees": 0.0},
    "ceilingsInside": {"standAt": [layout.ceilingCenterX, 60.0], "headingDegrees": 0.0, "pitchDegrees": 0.0},
    "openings": {"standAt": [layout.openingCenterX, 4.0], "headingDegrees": 0.0, "pitchDegrees": 0.0},
    "openingsInside": {"standAt": [layout.openingCenterX, 50.0], "headingDegrees": 0.0, "pitchDegrees": -5.0},
  }
  renders = {}
  for name, view in views.items():
    shading = "layout" if name == "above" else "client"
    rendered, _ = await session.call("renderView", {"view": view, "guides": False, "swimVolumes": name == "pool", "shading": shading, "bandHeight": 2.0})
    renders[name] = rendered["outputPath"]
  reach, _ = await session.call("mapReach", {"problemViews": False})
  return {
    "checkExport": {"failures": checked["failures"], "findings": [finding.get("finding", finding.get("failure")) for finding in checked["findings"]]},
    "export": {key: exported[key] for key in ("path", "manifest")}, "verifyExport": {
      "failedChecks": verified.get("failedChecks"), "checks": [{"check": check["check"], "passed": check["passed"]} for check in verified["checks"]],
    },
    "renders": renders, "lanePlan": reach["plan"]["outputPath"], "reachProblems": [(problem["kind"], problem.get("verdict"), problem.get("groups")) for problem in reach["problems"]],
  }


async def main():
  with tempfile.TemporaryDirectory(prefix="zwMeasure") as folder:
    async with Client(stagedServer(Path(folder))) as client:
      session = Session(client)
      await build(session)
      summary = await exportAndLook(session)
  print(json.dumps(summary, indent=1))


if __name__ == "__main__":
  asyncio.run(main())
