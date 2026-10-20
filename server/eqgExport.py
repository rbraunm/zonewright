"""An EQG zone archive from what bridgeExport collects from a scene: the terrain (.ter) placed at the origin as TER_<zone>, its
boundaries merged in as triangles without a material, each model (.mod) at its placements, triangles players pass through flagged 0x1,
a version 1 .zon with the swim volumes and zone lines as regions and the scene's point lights, and every material's textures as DDS.
The terrain placement carries baked light (ter_<zone>.lit) only where caves set daylight: no baked color, the share of scene light as
alpha; models carry none. The client models the scene uses come in as clientModelBundle copies them: their entries and textures byte
for byte, the scenery among them placed by the .zon under the names they are stored under (the client's own entry names, or with their
archive's name before them where taken). The scene's emitters go beside the archive in the client's emitter list, which the client
reads loose."""
import hashlib
import os
from pathlib import Path

import numpy
from PIL import Image

import clientModelBundle
import eqTextures
import eqgFiles
import eqgWriter


def textureEntry(path):
  """A texture as the archive stores it: a DDS file under its name in the client's form (eqTextures.clientDDS), any other image as an
  uncompressed DDS."""
  name = os.path.basename(path).lower()
  data = Path(path).read_bytes()
  if data[:4] == b"DDS ":
    if not name.endswith(".dds"):
      raise ValueError(f"{path} holds DDS data under another extension")
    return name, eqTextures.clientDDS(data)
  with Image.open(path) as image:
    rgba = numpy.asarray(image.convert("RGBA"))
  return os.path.splitext(name)[0] + ".dds", eqgWriter.ddsBytes(rgba)


def zoneArchive(collected, clientRoot):
  """The archive's bytes, what went into it (the scene's own terrain, models, and textures, and the client files its client models
  bring), and those client models' bundle (clientModelBundle.bundle, beside our files)."""
  zone = collected["zone"]
  if not eqgFiles.zoneNamePattern.match(zone):
    raise ValueError(f"Zone name '{zone}' is not a zone short name: {eqgFiles.zoneNameRule}")
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
      # A normal map ships only with a liquid; on any other material it stays in the work file (eqgWriter.materialShader).
      shipsNormal = liquid is not None and material["normalPath"] is not None
      writerMaterials.append({
        "name": name, "diffuseTexture": texture(material["diffusePath"]),
        "normalTexture": texture(material["normalPath"]) if shipsNormal else None, "cutout": material["cutout"],
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
  ownTextures = {name: hashlib.sha256(files[name]).hexdigest() for name in textureSources}
  bundle = clientModelBundle.bundle(clientRoot, collected["clientModels"], files)
  files |= bundle["files"]
  placements += [{key: placement[key] for key in ("model", "name", "position", "rotation", "scale")} for placement in bundle["placements"]]
  files[f"{zone}.zon"] = eqgWriter.zoneBytes([terrainFile] + [entry["file"] for entry in collected["models"]] + bundle["zonModels"], placements, collected["regions"], collected["lights"])
  data = eqgWriter.archiveBytes(files)
  return data, bundle, {
    "zone": zone, "bytes": len(data), "terrainTriangles": len(collected["terrain"]["materials"]),
    "boundaryTriangles": sum(name is None for name in collected["terrain"]["materials"]),
    "modelTriangles": {entry["file"]: len(entry["materials"]) for entry in collected["models"]},
    "passableTriangles": {entry["file"]: int(arrays[f"{entry['arrays']}_passable"].sum()) for entry in [collected["terrain"]] + collected["models"] if arrays[f"{entry['arrays']}_passable"].any()},
    "placements": len(collected["placements"]), "lights": len(collected["lights"]), "emitters": len(collected["emitters"]), "terrainBakedLight": litFile,
    "regions": [region["name"] for region in collected["regions"]],
    "textures": sorted(textureSources), "materials": sorted(materials), "ownTextures": ownTextures,
    "clientFiles": sorted(bundle["files"]), "clientPlacements": len(bundle["placements"]), "clientRowPieces": len(bundle["rowPieces"]),
    "clientBytes": sum(len(data) for data in bundle["files"].values()),
  }
