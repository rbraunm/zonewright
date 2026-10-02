"""The asset catalog's measured lane: what a source's graphical assets are, read from the files. A source is a client zone (the archives
it loads for its own geometry and objects, its lights, and its emitter list), a client folder of loose images (sky, water, precipitation,
emitter images), or an EQG zone archive outside the client, such as one exportZone wrote. Each texture is also written out readable,
with a thumbnail, so it can be viewed and used in a material."""
import colorsys
import collections
import hashlib
import io
import os
import re
import struct

import numpy
from PIL import Image

import eqArchive
import eqEmitters
import eqgFiles
import eqgTerrain
import eqLinks
import eqModels
import eqTextures
import eqWorldFile
import eqZones
import surveyFields
import zoneGeometry
import zoneSources

surveyVersion = 4
thumbnailSide = 128
imageExtensions = (".dds", ".bmp", ".tga", ".png", ".jpg")
looseFolders = ("Resources/Sky", "Resources/WaterSwap", "Resources/Precipitation", "EnvEmitterEffects")
# Slope bands of the faces a texture covers, in degrees from flat: ground, slopes, cliffs, walls, overhangs.
slopeBands = (("flat", 0, 15), ("slope", 15, 45), ("steep", 45, 75), ("vertical", 75, 105), ("overhang", 105, 181))
# A texture whose opposite edges differ no more than this times the 90th percentile of its neighboring columns (or rows) repeats
# without a visible seam. Measured: seamless client textures score 0.9 to 1.2, unique maps and atlases 2 to 5.
tilingSeamRatio = 1.5
seamPercentile = 90
listLength = 5
genericLightName = re.compile(r"^l\d+_ldef$")


def shortHash(data):
  return hashlib.sha256(data).hexdigest()


def ddsFormat(data):
  flags, fourCC, bitCount = struct.unpack_from("<I4sI", data, 80)
  if flags & 0x4:
    return fourCC.decode("latin1")
  alphaMask = struct.unpack_from("<I", data, 104)[0]
  return f"{'A' if alphaMask else 'X'}RGB{bitCount}"


colorNames = ("black", "white", "grey", "red", "brown", "tan", "orange", "yellow", "green", "teal", "blue", "purple", "pink")


def colorName(red, green, blue):
  """A plain name for a mean color (components 0-1): a hint for the interpreted lane's color tags."""
  hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
  degrees = hue * 360
  if value < 0.15:
    return "black"
  if saturation < 0.15:
    return "white" if value > 0.85 else "grey"
  if degrees < 15 or degrees >= 345:
    return "red"
  if degrees < 45:
    return "brown" if value < 0.5 else "tan" if saturation < 0.45 else "orange"
  if degrees < 70:
    return "tan" if saturation < 0.4 else "yellow"
  if degrees < 160:
    return "green"
  if degrees < 200:
    return "teal"
  if degrees < 255:
    return "blue"
  return "purple" if degrees < 290 else "pink"


def seamRatios(rgb):
  """How much a texture's opposite edges differ, across x then across y, against how much its neighboring columns (rows) differ inside
  it, so a pattern whose edges fall on its own boundaries, such as a checkerboard, still reads as seamless."""
  ratios = []
  for axis in (1, 0):
    if rgb.shape[axis] < 4:
      ratios.append(None)
      continue
    neighbors = numpy.abs(numpy.diff(rgb, axis=axis)).mean(axis=(0 if axis == 1 else 1, 2))
    typical = float(numpy.percentile(neighbors, seamPercentile))
    edge = float(numpy.abs(numpy.take(rgb, 0, axis=axis) - numpy.take(rgb, -1, axis=axis)).mean())
    ratios.append(round(edge / typical, 2) if typical > 1e-6 else None)
  return ratios


def imageFacts(textureName, data):
  """A texture's format, size, alpha, color, and tiling, and its RGBA pixels."""
  with Image.open(io.BytesIO(data)) as image:
    image.load()
    container, mode = image.format, image.mode
    rgba = numpy.asarray(image.convert("RGBA"))
  pixels = rgba.astype(numpy.float64) / 255
  alpha = pixels[..., 3]
  covered = alpha >= 0.5
  rgb = pixels[..., :3]
  mean = rgb[covered].mean(0) if covered.any() else rgb.reshape(-1, 3).mean(0)
  luminance = rgb @ (0.299, 0.587, 0.114)
  seams = seamRatios(rgb)
  return {
    "width": int(rgba.shape[1]), "height": int(rgba.shape[0]),
    "format": ddsFormat(data) if data[:4] == b"DDS " else f"{container} {mode}",
    "transparentShare": round(float((alpha < 0.5).mean()), 3), "partialAlphaShare": round(float(((alpha >= 0.05) & (alpha < 0.95)).mean()), 3),
    "meanColor": [int(round(component * 255)) for component in mean], "colorName": colorName(*mean),
    "brightness": round(float(luminance.mean()), 3), "contrast": round(float(luminance.std()), 3),
    "seamRatios": seams, "tiles": all(ratio is not None and ratio <= tilingSeamRatio for ratio in seams),
    "normalMapLike": bool(abs(mean[0] - 0.5) < 0.12 and abs(mean[1] - 0.5) < 0.12 and mean[2] > 0.7),
    "nameSaysNormalMap": bool(re.search(r"_n(\.|$)|normal", textureName)),
  }, rgba


def textureFolder(cacheRoot, digest):
  return cacheRoot / "textures" / digest[:12]


def thumbnailPath(cacheRoot, digest):
  return cacheRoot / "thumbnails" / f"{digest[:12]}.png"


def writeOnce(path, data):
  """Write a content-addressed file unless it exists. Parallel surveys write the same image at once, so each writes its own temporary
  file and renames it into place; when another got there first, its identical file stands."""
  if path.is_file():
    return
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
  temporary.write_bytes(data)
  try:
    os.replace(temporary, path)
  except PermissionError:
    if not path.is_file():
      raise
    temporary.unlink()


class TextureStore:
  """Readable texture files and thumbnails under the tooling root's catalog folder, one per distinct content; the measured facts name
  only the file, so they read the same on every machine."""

  def __init__(self, cacheRoot):
    self.cacheRoot = cacheRoot

  def store(self, textureName, rawBytes):
    digest = shortHash(rawBytes)
    fileName, readable = eqTextures.readableTexture(textureName, rawBytes)
    writeOnce(textureFolder(self.cacheRoot, digest) / fileName, readable)
    facts, rgba = imageFacts(textureName, readable)
    thumbnail = thumbnailPath(self.cacheRoot, digest)
    if not thumbnail.is_file():
      image = Image.fromarray(rgba, "RGBA")
      image.thumbnail((thumbnailSide, thumbnailSide))
      encoded = io.BytesIO()
      image.save(encoded, "PNG")
      writeOnce(thumbnail, encoded.getvalue())
    return f"texture/{textureName}@{digest[:8]}", {"kind": "texture", "name": textureName, "sha256": digest, "fileName": fileName} | facts


def textureUses(geometry):
  """Per texture name: its share of the zone's textured area, world units per texture repeat, slope bands, placed-object share, and surface kinds."""
  if not geometry["textureNames"]:
    return {}
  _, areas, normalZ = surveyFields.triangleFrames(geometry)
  slopes = numpy.degrees(numpy.arccos(numpy.clip(normalZ, -1, 1)))
  textures = geometry["triangleTextures"]
  uvAreas = geometry["triangleUVAreas"]
  texturedArea = float(areas[textures >= 0].sum())
  uses = {}
  for index, name in enumerate(geometry["textureNames"]):
    mask = textures == index
    area = float(areas[mask].sum())
    if area <= 0:
      continue
    measurable = mask & numpy.isfinite(uvAreas) & (uvAreas > 1e-9)
    uses[name] = {
      "areaShare": round(area / texturedArea, 4),
      "unitsPerRepeat": round(float(numpy.sqrt(areas[measurable].sum() / uvAreas[measurable].sum())), 1) if measurable.any() else None,
      "slopeShares": {band: round(float(areas[mask & (slopes >= low) & (slopes < high)].sum()) / area, 3) for band, low, high in slopeBands},
      "objectShare": round(float(areas[mask & geometry["triangleIsObject"]].sum()) / area, 3),
      "surfaces": {kind: round(float(areas[mask & (geometry["triangleSurfaces"] == code)].sum()) / area, 3) for code, kind in enumerate(zoneGeometry.surfaceKinds) if (mask & (geometry["triangleSurfaces"] == code)).any()},
    }
  return uses


def textureNamesOf(value):
  return value.lower() if isinstance(value, str) and value.lower().endswith(imageExtensions) else None


def eqgModelFacts(library, placements):
  """Facts for each placed EQG model and, per texture, the material properties it fills, the shaders it is drawn with, the textures
  it shares a material with, and the models that use it."""
  counts = collections.Counter(placement["model"] for placement in placements)
  scales = collections.defaultdict(list)
  for placement in placements:
    # EQ terrain zones scale each axis; the range follows the largest.
    scales[placement["model"]].append(float(numpy.max(placement["scale"])))
  models, textureMaterials = {}, collections.defaultdict(lambda: {"roles": collections.Counter(), "shaders": collections.Counter(), "partners": collections.Counter(), "models": collections.Counter()})
  for modelName, count in counts.items():
    holder = next((archive for archive in library.archives if modelName in archive.entries), None)
    model = library.model(modelName)
    if holder is None or model is None:
      continue
    digest = shortHash(holder.read(modelName))
    size = model["vertices"].max(0) - model["vertices"].min(0) if len(model["vertices"]) else numpy.zeros(3)
    materials = []
    for material in model["materials"]:
      named = {key: textureNamesOf(value) for key, value in material["properties"].items() if textureNamesOf(value)}
      materials.append({"shader": material["shader"], "textures": named})
      for role, textureName in named.items():
        entry = textureMaterials[textureName]
        entry["roles"][role] += 1
        entry["shaders"][material["shader"]] += 1
        entry["models"][modelName] += count
        for partner in named.values():
          if partner != textureName:
            entry["partners"][partner] += 1
    models[f"model/{modelName}@{digest[:8]}"] = {
      "kind": "model", "name": modelName, "archive": holder.archivePath.name.lower(), "sha256": digest,
      "triangles": int(len(model["triangles"])), "size": [round(float(value), 1) for value in size], "skinned": model["bones"] is not None,
      "placements": count, "scaleRange": [round(min(scales[modelName]), 3), round(max(scales[modelName]), 3)], "materials": materials,
    }
  return models, {name: {key: dict(counter.most_common(listLength)) for key, counter in entry.items()} for name, entry in textureMaterials.items()}


def wldMaterialFacts(archive):
  """Per texture in an archive's WLD files: its render methods and, for an animated material, its frames in order."""
  facts = collections.defaultdict(lambda: {"renderMethods": collections.Counter(), "animationFrames": None})
  for wldName in [name for name in archive.entries if name.endswith(".wld")]:
    worldFile = eqWorldFile.WorldFile(archive.read(wldName), f"{archive.archivePath.name}:{wldName}")
    for fragment in worldFile.fragmentsOfType(0x30):
      material = worldFile.material(fragment.index)
      for textureName in material["textureNames"]:
        facts[textureName]["renderMethods"][f"{material['renderMethod']:#x}"] += 1
        if len(material["textureNames"]) > 1:
          facts[textureName]["animationFrames"] = material["textureNames"]
  return {name: {"renderMethods": dict(entry["renderMethods"]), "animationFrames": entry["animationFrames"]} for name, entry in facts.items()}


def wldModelFacts(clientRoot, cacheRoot, zoneName, archive):
  """The objects a classic zone's objects.wld places, by actor, with the archive that defines each (the first the client loads)."""
  models = {}
  for actor, count in zoneGeometry.wldPlacementCounts(archive).items():
    try:
      definition = eqModels.resolveModel(clientRoot, cacheRoot, actor, zoneName)
    except ValueError as error:
      models[f"model/{actor}@unresolved"] = {"kind": "model", "name": actor, "placements": count, "problem": str(error)}
      continue
    models[f"model/{actor}@{definition['archive'].rsplit('.', 1)[0]}"] = {
      "kind": "model", "name": actor, "archive": definition["archive"], "modelKind": definition["kind"], "placements": count,
    }
  return models


def lightStyle(light):
  name = light["name"].lower()
  if genericLightName.match(name):
    color = "".join(f"{int(round(component * 255)):02x}" for component in light["color"])
    return f"r{light['radius']:g}_{color}"
  return re.sub(r"[\d_]+$", "", name) or name


def lightFacts(zoneName, lights):
  styles = collections.defaultdict(list)
  for light in lights:
    styles[lightStyle(light)].append(light)
  facts = {}
  for style, members in styles.items():
    colors = collections.Counter(tuple(round(component, 3) for component in light["color"]) for light in members)
    facts[f"light/{zoneName}/{style}"] = {
      "kind": "light", "name": style, "zone": zoneName, "count": len(members), "shareOfZoneLights": round(len(members) / len(lights), 3),
      "colors": [{"rgb": list(color), "count": count} for color, count in colors.most_common(3)],
      "radii": sorted({round(light["radius"], 1) for light in members})[:listLength],
      "names": sorted({light["name"] for light in members})[:3],
      "flickerFrames": max(light.get("frames", 1) for light in members),
      "examplePositions": [[round(value, 1) for value in light["position"]] for light in members[:3]],
    }
  return facts


def emitterFacts(zoneName, emitters):
  byDefinition = collections.defaultdict(list)
  for emitter in emitters:
    byDefinition[emitter["definition"]].append(emitter)
  return {
    f"emitter/{definition}": {
      "kind": "emitter", "name": str(definition), "zones": {zoneName: {
        "count": len(members), "names": dict(collections.Counter(emitter["name"] for emitter in members).most_common(listLength)),
        "lifespans": sorted({emitter["lifespan"] for emitter in members})[:listLength],
        "examplePositions": [[round(value, 1) for value in emitter["position"]] for emitter in members[:3]],
      }},
    }
    for definition, members in byDefinition.items()
  }


def ecosystemFacts(zoneName, archive, tileSide):
  """An EQ terrain zone's ecosystems: each one's texture layers and the slope and height ranges that choose them. A layer repeats its
  detail and normal maps a given number of times across each terrain tile, so in world units each repeats every tile side over that."""
  facts, layerUses = {}, collections.defaultdict(list)
  for ecoName in sorted(name for name in archive.entries if name.endswith(".eco")):
    ecosystem = ecoName[:-4]
    try:
      layers = eqgTerrain.parseEcosystem(archive.read(ecoName).decode("latin1"), f"{archive.archivePath.name}:{ecoName}")
    except ValueError as error:
      facts[f"ecosystem/{zoneName}/{ecosystem}"] = {"kind": "ecosystem", "name": ecosystem, "zone": zoneName, "problem": str(error)}
      continue
    for layer in layers:
      for repeatKey, unitsKey in (("detailRepeat", "detailUnitsPerRepeat"), ("normalRepeat", "normalUnitsPerRepeat")):
        if layer.get(repeatKey):
          layer[unitsKey] = round(tileSide / layer[repeatKey], 1)
    facts[f"ecosystem/{zoneName}/{ecosystem}"] = {"kind": "ecosystem", "name": ecosystem, "zone": zoneName, "layers": layers}
    for layer in layers:
      for key in ("coverMap", "detailMap", "normalMap", "blendMap", "layeringMap"):
        if key in layer:
          layerUses[layer[key]].append({"ecosystem": ecosystem, "layer": layer["name"], "as": key} | {field: layer[field] for field in ("minSlope", "maxSlope", "minHeight", "maxHeight", "detailUnitsPerRepeat", "normalUnitsPerRepeat") if field in layer})
  return facts, layerUses


def storeTextures(store, archives, uses, materialUses, wldMaterials, layerUses):
  """Every image in the archives, in load order; a later archive's different image under a name already seen is marked shadowed, as
  the client keeps the first."""
  assets, seen = {}, {}
  for archive in archives:
    for name in sorted(archive.entries):
      if not name.endswith(imageExtensions):
        continue
      raw = archive.read(name)
      try:
        assetID, facts = store.store(name, raw)
      except (OSError, ValueError, struct.error) as error:
        assets[f"texture/{name}@unreadable"] = {"kind": "texture", "name": name, "archive": archive.archivePath.name.lower(), "problem": f"{type(error).__name__}: {error}"}
        continue
      archiveName = archive.archivePath.name.lower()
      if assetID in assets:
        assets[assetID]["archives"].append(archiveName)
        continue
      shadowed = name in seen and seen[name] != assetID
      seen.setdefault(name, assetID)
      assets[assetID] = facts | {"archives": [archiveName], "shadowed": shadowed} | ({} if shadowed else {
        key: value for key, value in (("uses", uses.get(name)), ("materials", materialUses.get(name)), ("wldMaterials", wldMaterials.get(name)), ("ecosystemLayers", layerUses.get(name))) if value
      })
  return assets


def zoneArchivePaths(clientRoot, zoneName):
  """The archives a zone loads for its own geometry and objects, in the client's load order: its zone links less character archives."""
  links = eqLinks.zoneLinks(clientRoot, zoneName)["archives"]
  return [clientRoot / link["archive"] for link in links if "_chr" not in link["archive"] and not link["via"].endswith("_chr.txt")]


def clientZoneSourcePaths(clientRoot, zoneName):
  """Every file a client zone's survey reads."""
  source = eqZones.zoneSource(clientRoot, zoneName)
  emitterPath = eqEmitters.emitterListPath(clientRoot, zoneName)
  return zoneArchivePaths(clientRoot, zoneName) + ([source["zonPath"]] if "zonPath" in source else []) + ([emitterPath] if emitterPath else [])


def looseFolderPaths(clientRoot, folder):
  if folder not in looseFolders:
    raise ValueError(f"'{folder}' is not one of the client's loose image folders {list(looseFolders)}")
  return sorted(path for path in (clientRoot / folder).iterdir() if path.suffix.lower() in imageExtensions)


def zoneFileSourcePaths(archivePath):
  emitterPath = archivePath.parent / f"{archivePath.stem}_EnvironmentEmitters.txt"
  return [archivePath] + ([emitterPath] if emitterPath.is_file() else [])


def surveyClientZone(clientRoot, cacheRoot, catalogRoot, zoneName):
  source = eqZones.zoneSource(clientRoot, zoneName)
  archivePaths = zoneArchivePaths(clientRoot, zoneName)
  emitterPath = eqEmitters.emitterListPath(clientRoot, zoneName)
  archives = [eqArchive.EQArchive(path) for path in archivePaths]
  problems = []
  geometry = zoneGeometry.buildGeometry(clientRoot, source)
  models, materialUses, wldMaterials, layerUses = {}, {}, {}, {}
  if source["format"] == "wld":
    zoneArchive = eqArchive.EQArchive(source["archive"])
    models = wldModelFacts(clientRoot, cacheRoot, zoneName, zoneArchive)
    for archive in archives:
      for name, facts in wldMaterialFacts(archive).items():
        wldMaterials.setdefault(name, facts)
  else:
    library = zoneSources.ModelLibrary(zoneSources.assetArchivePaths(clientRoot, source)[0], zoneName)
    if source["format"] == "eqgz":
      zonBytes = source["zonPath"].read_bytes() if "zonPath" in source else library.archives[0].read(source["zon"])
      placements = eqgFiles.parseZone(zonBytes, zoneName)["placements"]
    else:
      terrainArchive = library.archives[0]
      terrain = eqgTerrain.parseTerrain(terrainArchive.read(source["zon"]).decode("latin1"), terrainArchive.read(source["zon"][:-4] + ".dat"), zoneName)
      placements = terrain["placements"]
      ecosystems, layerUses = ecosystemFacts(zoneName, terrainArchive, terrain["header"]["quadsPerTile"] * terrain["header"]["unitsPerVertex"])
      models |= ecosystems
    placedModels, materialUses = eqgModelFacts(library, placements)
    models |= placedModels
  lights = eqZones.zoneLights(clientRoot, zoneName)
  if lights is None:
    problems.append("This EQ terrain zone's lights are not read")
  emitters = eqEmitters.parseEmitters(emitterPath.read_text(encoding="latin1"), emitterPath.name) if emitterPath else []
  assets = storeTextures(TextureStore(catalogRoot), archives, textureUses(geometry), materialUses, wldMaterials, layerUses)
  assets |= models | lightFacts(zoneName, lights or []) | emitterFacts(zoneName, emitters)
  return {"source": f"zone:{zoneName}", "zone": zoneName, "format": source["format"], "archives": [path.name.lower() for path in archivePaths], "assets": assets, "problems": problems}


def surveyClientZoneInWorker(clientRoot, modelCacheRoot, catalogCacheRoot, zoneName):
  """Runs in a worker process. A zone these parsers cannot read is reported rather than stopping the others."""
  try:
    return {"survey": surveyClientZone(clientRoot, modelCacheRoot, catalogCacheRoot, zoneName)}
  except (ValueError, KeyError, struct.error) as parseError:
    return {"error": f"{type(parseError).__name__}: {parseError}"}


def surveyLooseFolder(clientRoot, catalogRoot, folder):
  paths = looseFolderPaths(clientRoot, folder)
  store, assets = TextureStore(catalogRoot), {}
  for path in paths:
    assetID, facts = store.store(path.name.lower(), path.read_bytes())
    assets.setdefault(assetID, facts | {"archives": [folder], "shadowed": False})
  return {"source": f"folder:{folder}", "folder": folder, "assets": assets, "problems": []}


def surveyZoneFile(clientRoot, catalogRoot, archivePath):
  archive = eqArchive.EQArchive(archivePath)
  zoneFiles = [name for name in archive.entries if name.endswith(".zon")]
  if len(zoneFiles) != 1:
    raise ValueError(f"{archivePath.name} holds {len(zoneFiles)} .zon files; a zone archive holds one")
  zoneName = archivePath.stem.lower()
  source = {"zone": zoneName, "format": "eqgz", "archive": archivePath, "zon": zoneFiles[0]}
  geometry = zoneGeometry.buildEQGGeometry(clientRoot, source)
  zone = eqgFiles.parseZone(archive.read(zoneFiles[0]), zoneFiles[0])
  models, materialUses = eqgModelFacts(zoneSources.ModelLibrary([archivePath], zoneName), zone["placements"])
  emitterPath = archivePath.parent / f"{archivePath.stem}_EnvironmentEmitters.txt"
  emitters = eqEmitters.parseEmitters(emitterPath.read_text(encoding="latin1"), emitterPath.name) if emitterPath.is_file() else []
  assets = storeTextures(TextureStore(catalogRoot), [archive], textureUses(geometry), materialUses, {}, {})
  assets |= models | lightFacts(zoneName, zone["lights"]) | emitterFacts(zoneName, emitters)
  return {"source": f"file:{archivePath}", "zone": zoneName, "format": "eqgz", "archives": [archivePath.name.lower()], "assets": assets, "problems": []}
