"""An EQG zone archive from what bridgeExport collects from a scene: the terrain (.ter) placed at the origin as TER_<zone>, its
boundaries merged in as triangles without a material, each model (.mod) at its placements, triangles players pass through flagged 0x1,
a version 1 .zon with the swim volumes and zone lines as regions and the scene's point lights, and every material's textures as DDS.
The terrain placement carries baked light (ter_<zone>.lit) only where caves set daylight: no baked color, the share of scene light as
alpha; models carry none. The scene's emitters go beside the archive in the client's emitter list, which the client reads loose."""
import os
import re
from pathlib import Path

import numpy
from PIL import Image

import eqgFiles
import eqgWriter

zoneNamePattern = re.compile(r"^[a-z0-9]+$")


def textureEntry(path):
  """A texture as the archive stores it: a DDS file's bytes unchanged under its name, any other image as an uncompressed DDS."""
  name = os.path.basename(path).lower()
  data = Path(path).read_bytes()
  if data[:4] == b"DDS ":
    if not name.endswith(".dds"):
      raise ValueError(f"{path} holds DDS data under another extension")
    return name, data
  with Image.open(path) as image:
    rgba = numpy.asarray(image.convert("RGBA"))
  return os.path.splitext(name)[0] + ".dds", eqgWriter.ddsBytes(rgba)


def zoneArchive(collected):
  """The archive's bytes and what went into it."""
  zone = collected["zone"]
  if not zoneNamePattern.match(zone):
    raise ValueError(f"Zone name '{zone}' must be lowercase letters and digits, as the client's zone short names are")
  arrays = numpy.load(collected["arrays"])
  materials = {material["name"]: material for material in collected["materials"]}
  files, textureSources, textureNames = {}, {}, {}

  def texture(path):
    if path not in textureNames:
      name, data = textureEntry(path)
      if name in textureSources:
        raise ValueError(f"Textures {textureSources[name]} and {path} would both be stored as {name}")
      textureSources[name], textureNames[path], files[name] = path, name, data
    return textureNames[path]

  def model(kind, entry):
    names = [name for name in dict.fromkeys(entry["materials"]) if name is not None]
    writerMaterials = []
    for name in names:
      material = materials[name]
      liquid = material["liquid"]
      writerMaterials.append({
        "name": name, "diffuseTexture": texture(material["diffusePath"]),
        "normalTexture": texture(material["normalPath"]) if material["normalPath"] is not None else None, "cutout": material["cutout"],
        "liquid": None if liquid is None else {
          "liquid": liquid["liquid"], "values": liquid["values"],
          "environmentTexture": texture(liquid["environmentPath"]) if liquid["environmentPath"] else None,
          "secondDiffuseTexture": texture(liquid["secondDiffusePath"]) if liquid["secondDiffusePath"] else None,
        },
      })
    index = {name: position for position, name in enumerate(names)} | {None: eqgFiles.noMaterial}
    prefix = entry["arrays"]
    return eqgWriter.modelBytes(kind, writerMaterials, arrays[f"{prefix}_positions"], arrays[f"{prefix}_normals"], arrays[f"{prefix}_uvs"],
      arrays[f"{prefix}_triangles"], [index[name] for name in entry["materials"]], numpy.where(arrays[f"{prefix}_passable"], eqgFiles.passableFlag, 0))

  terrainFile = collected["terrain"]["file"]
  files[terrainFile] = model("ter", collected["terrain"])
  litFile = None
  if collected["terrain"]["daylight"]:
    # The terrain placement's baked light: no baked color, and as alpha the share of scene light each vertex takes (a cave's daylight).
    shares = arrays[f"{collected['terrain']['arrays']}_shares"]
    colors = numpy.zeros((len(shares), 4), dtype=numpy.uint32)
    colors[:, 3] = numpy.round(numpy.clip(shares, 0.0, 1.0) * 255)
    litFile = f"ter_{zone}.lit"
    files[litFile] = eqgWriter.litBytes(colors)
  for entry in collected["models"]:
    files[entry["file"]] = model("mod", entry)
  placements = [{"model": terrainFile, "name": f"TER_{zone}", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0}]
  placements += [{key: placement[key] for key in ("model", "name", "position", "rotation", "scale")} for placement in collected["placements"]]
  files[f"{zone}.zon"] = eqgWriter.zoneBytes([terrainFile] + [entry["file"] for entry in collected["models"]], placements, collected["regions"], collected["lights"])
  data = eqgWriter.archiveBytes(files)
  return data, {
    "zone": zone, "bytes": len(data), "terrainTriangles": len(collected["terrain"]["materials"]),
    "boundaryTriangles": sum(name is None for name in collected["terrain"]["materials"]),
    "modelTriangles": {entry["file"]: len(entry["materials"]) for entry in collected["models"]},
    "passableTriangles": {entry["file"]: int(arrays[f"{entry['arrays']}_passable"].sum()) for entry in [collected["terrain"]] + collected["models"] if arrays[f"{entry['arrays']}_passable"].any()},
    "placements": len(collected["placements"]), "lights": len(collected["lights"]), "emitters": len(collected["emitters"]), "terrainBakedLight": litFile,
    "regions": [region["name"] for region in collected["regions"]],
    "textures": sorted(textureSources), "materials": sorted(materials),
  }
