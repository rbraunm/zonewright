"""An EQG zone archive from what bridgeExport collects from a scene: the terrain (.ter) placed at the origin as TER_<zone>, each model
(.mod) at its placements, a version 1 .zon, and every material's textures as DDS. No baked light yet: placements carry no .lit."""
import os
import re
from pathlib import Path

import numpy
from PIL import Image

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
    names = list(dict.fromkeys(entry["materials"]))
    writerMaterials = []
    for name in names:
      material = materials[name]
      writerMaterials.append({
        "name": name, "diffuseTexture": texture(material["diffusePath"]),
        "normalTexture": texture(material["normalPath"]) if material["normalPath"] is not None else None, "cutout": material["cutout"],
      })
    index = {name: position for position, name in enumerate(names)}
    prefix = entry["arrays"]
    return eqgWriter.modelBytes(kind, writerMaterials, arrays[f"{prefix}_positions"], arrays[f"{prefix}_normals"], arrays[f"{prefix}_uvs"],
      arrays[f"{prefix}_triangles"], [index[name] for name in entry["materials"]])

  terrainFile = collected["terrain"]["file"]
  files[terrainFile] = model("ter", collected["terrain"])
  for entry in collected["models"]:
    files[entry["file"]] = model("mod", entry)
  placements = [{"model": terrainFile, "name": f"TER_{zone}", "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0}]
  placements += [{key: placement[key] for key in ("model", "name", "position", "rotation", "scale")} for placement in collected["placements"]]
  files[f"{zone}.zon"] = eqgWriter.zoneBytes([terrainFile] + [entry["file"] for entry in collected["models"]], placements)
  data = eqgWriter.archiveBytes(files)
  return data, {
    "zone": zone, "bytes": len(data), "terrainTriangles": len(collected["terrain"]["materials"]),
    "modelTriangles": {entry["file"]: len(entry["materials"]) for entry in collected["models"]},
    "placements": len(collected["placements"]), "textures": sorted(textureSources), "materials": sorted(materials),
  }
