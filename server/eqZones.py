"""EverQuest zones built from the client's files into a cache the bridge places as one mesh, with the normals and vertex colors the
client lights them by: a classic (WLD) zone's region meshes and the objects its objects.wld places, or an EQ terrain zone's tiles,
textured as the client textures them, and the objects and object groups they place."""
import json
import math
import re
import struct

import numpy
from PIL import Image

import eqArchive
import eqgFiles
import eqgTerrain
import eqModels
import eqTerrainTextures
import eqTextures
import eqWorldFile
import zoneSources

zoneCacheFormat = 4
readFormats = ("wld", "eqtzp")
# A model's vertex light where its file gives none: no baked light and the full share of scene light, an assumption until the client's
# lighting of EQG objects is traced.
unlitColor = (0, 0, 0, 255)
anglesPerTurn = 512
# objects.wld placement flag for a per-instance vertex color fragment.
instanceColorsFlag = 0x100


def objectPlacements(objectsFile):
  """objects.wld's object placements (0x15): the actor, position, heading and tilt in 512ths of a turn, and a uniform scale."""
  placements = []
  for fragment in objectsFile.fragmentsOfType(0x15):
    body = fragment.body
    nameReference, flags = struct.unpack_from("<iI", body, 4)
    x, y, z, heading, tilt, roll, _, scaleY, scaleZ = struct.unpack_from("<9f", body, 16)
    actor = objectsFile.lookupName(nameReference)
    if flags & instanceColorsFlag:
      raise ValueError(f"{objectsFile.sourceName}: {actor} carries per-instance vertex colors, which are not read yet")
    if roll != 0 or scaleY != scaleZ:
      raise ValueError(f"{objectsFile.sourceName}: {actor} has roll {roll} and scales {scaleY}, {scaleZ}; only a heading, a tilt, and one scale are read")
    placements.append({"actor": actor, "position": numpy.array((x, y, z)), "heading": heading, "tilt": tilt, "scale": scaleY})
  return placements


def placementRotation(placement):
  """The rotation a placement gives its object: the tilt about the object's Y axis, then the heading about Z, counter-clockwise from
  above as a spawn's heading turns it."""
  heading = math.radians(placement["heading"] * 360 / anglesPerTurn)
  tilt = math.radians(placement["tilt"] * 360 / anglesPerTurn)
  aboutZ = numpy.array([[math.cos(heading), -math.sin(heading), 0], [math.sin(heading), math.cos(heading), 0], [0, 0, 1]])
  aboutY = numpy.array([[math.cos(tilt), 0, math.sin(tilt)], [0, 1, 0], [-math.sin(tilt), 0, math.cos(tilt)]])
  return aboutZ @ aboutY


def placedPart(part, placement):
  rotation = placementRotation(placement)
  lighting = part["lighting"] and {"normals": part["lighting"]["normals"] @ rotation.T, "colors": part["lighting"]["colors"]}
  return part | {"vertices": (part["vertices"] * placement["scale"]) @ rotation.T + placement["position"], "lighting": lighting}


def zoneSource(clientRoot, zoneName):
  """The zone variant to build: the classic one where the client has one, else the EQ terrain one."""
  variants = zoneSources.zoneVariants(clientRoot, zoneName)
  for zoneFormat in readFormats:
    if f"{zoneName}:{zoneFormat}" in variants:
      return variants[f"{zoneName}:{zoneFormat}"]
  raise ValueError(f"Zone '{zoneName}' has no classic (WLD) or EQ terrain variant in the client; it has {sorted(variants)}, which are not read yet")


def buildZone(clientRoot, cacheRoot, zoneName):
  """Write a zone into a cache folder as one mesh, reusing one built from the same client files. An object no archive the zone loads
  defines is left out, as the client draws nothing for it, and listed."""
  source = zoneSource(clientRoot, zoneName)
  zoneFolder = cacheRoot / "zones" / f"{zoneName}@{source['format']}"
  stampPath = zoneFolder / "source.json"
  archiveNames = [source["archive"].name.lower()] + [link["archive"] for link in eqModels.loadOrder(clientRoot, zoneName)[0] if link["tier"] == "zone"]
  stamp = {
    "zoneCacheFormat": zoneCacheFormat, "modelCacheFormat": eqModels.modelCacheFormat, "indexFormat": eqModels.indexFormat,
    "archives": eqModels.archiveStamp(clientRoot, sorted(set(archiveNames))), "listingFingerprint": zoneSources.clientListingFingerprint(clientRoot),
  }
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored.get(key) for key in stamp} == json.loads(json.dumps(stamp)):
      return zoneFolder, stored
  builder = buildClassicZone if source["format"] == "wld" else buildTerrainZone
  details = stamp | {"zone": zoneName, "format": source["format"], "archive": source["archive"].name.lower()} | builder(clientRoot, cacheRoot, zoneName, source, zoneFolder)
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return zoneFolder, details


def buildClassicZone(clientRoot, cacheRoot, zoneName, source, zoneFolder):
  """A classic zone's region meshes and the objects its objects.wld places."""
  archive = eqArchive.EQArchive(source["archive"])
  worldFile = eqWorldFile.WorldFile(archive.read(f"{zoneName}.wld"), f"{source['archive'].name}:{zoneName}.wld")
  parts = [eqModels.wldMeshPart(mesh, {}, True) for mesh in worldFile.meshes()]
  regionMeshCount = len(parts)
  placements = objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), f"{source['archive'].name}:objects.wld")) if "objects.wld" in archive.entries else []
  objectParts, missingModels, objectArchives, placedCounts, particleClouds = {}, set(), [], {}, 0
  for placement in placements:
    actor = placement["actor"]
    if actor not in objectParts:
      found = eqModels.findModel(clientRoot, cacheRoot, actor, zoneName)
      if not found["linked"] and not found["onDemand"]:
        objectParts[actor] = None
        missingModels.add(actor)
        continue
      definition = eqModels.resolveModel(clientRoot, cacheRoot, actor, zoneName)
      holder = eqArchive.EQArchive(clientRoot / definition["archive"])
      if definition["kind"] == "wldStatic":
        objectParts[actor] = eqModels.wldStaticParts(holder, definition, {}, None)["parts"]
      elif definition["kind"] == "wldSkeletal":
        objectParts[actor], clouds = eqModels.wldSkeletalBindParts(holder, definition)
        particleClouds += clouds
      else:
        raise ValueError(f"Zone '{zoneName}' places {actor}, a {definition['kind']} model from {definition['archive']}; only WLD objects are placed yet")
      if definition["archive"] not in objectArchives:
        objectArchives.append(definition["archive"])
    if objectParts[actor] is None:
      continue
    parts += [placedPart(part, placement) for part in objectParts[actor]]
    placedCounts[actor] = placedCounts.get(actor, 0) + 1
  textureHolders = [archive] + [eqArchive.EQArchive(clientRoot / name) for name in objectArchives]
  written = eqModels.writePartsCache(zoneFolder, parts, textureHolders, f"Zone '{zoneName}'")
  return {
    "regionMeshes": regionMeshCount, "placements": len(placements), "placedObjects": sum(placedCounts.values()), "objectArchives": objectArchives,
    "missingModels": sorted(missingModels), "particleCloudsNotDrawn": particleClouds,
  } | written


def bytesRGBA(colors):
  """D3DCOLOR values (0xAARRGGBB) as RGBA bytes."""
  colors = numpy.asarray(colors, dtype=numpy.uint32)
  return numpy.stack([(colors >> 16) & 0xFF, (colors >> 8) & 0xFF, colors & 0xFF, colors >> 24], axis=-1).astype(numpy.uint8)


def readEntry(archive, name):
  entry = name.lower()
  if entry not in archive.entries:
    raise ValueError(f"{archive.archivePath.name} has no {entry}")
  return archive.read(entry)


def parseObjectGroup(togText, sourceName):
  """An object group (.tog): its objects' models, positions, turns in degrees, scales, and the baked light file each names."""
  objects = []
  for match in re.finditer(r"\*BEGIN_OBJECT\b(.*?)\*END_OBJECT", togText, re.S):
    fields = {key: values.split() for key, values in re.findall(r"\*(\w+)\s+([^\r\n]*)", match.group(1))}
    if fields.get("FILE", [None])[0] != "LIT":
      raise ValueError(f"{sourceName}: object {fields.get('NAME')} names {fields.get('FILE')}; only LIT files are read")
    scale = float(fields["SCALE"][0])
    objects.append({
      "model": fields["NAME"][0].lower() + ".mod", "position": tuple(float(value) for value in fields["POSITION"]),
      "rotationDegrees": tuple(float(value) for value in fields["ROTATION"]), "scale": (scale, scale, scale), "lit": fields["FILE"][1].lower(),
    })
  return objects


def litColors(litBytes, vertexCount, sourceName):
  """A .lit file's baked light per vertex, or None for one whose count differs from the model's vertices, which the client ignores
  (EQGraphicsDX9.dll 0x100548d0)."""
  count = struct.unpack_from("<I", litBytes, 0)[0]
  if len(litBytes) != 4 + 4 * count:
    raise ValueError(f"{sourceName}: {count} colors in {len(litBytes)} bytes")
  if count != vertexCount:
    return None
  return bytesRGBA(numpy.frombuffer(litBytes, dtype="<u4", count=count, offset=4))


class TerrainObjects:
  """The static EQG models a terrain zone places, each parsed once from the archive the zone's links resolve it to."""
  def __init__(self, clientRoot, cacheRoot, zoneName):
    self.clientRoot, self.cacheRoot, self.zoneName = clientRoot, cacheRoot, zoneName
    self.models, self.missing, self.archives = {}, set(), []

  def model(self, modelName):
    if modelName not in self.models:
      self.models[modelName] = None
      modelKey = modelName.removesuffix(".mod")
      found = eqModels.findModel(self.clientRoot, self.cacheRoot, modelKey, self.zoneName)
      if not found["linked"] and not found["onDemand"]:
        self.missing.add(modelName)
        return None
      definition = eqModels.resolveModel(self.clientRoot, self.cacheRoot, modelKey, self.zoneName)
      if definition["kind"] != "eqgModel":
        raise ValueError(f"Zone '{self.zoneName}' places {modelName}, a {definition['kind']} model from {definition['archive']}; only EQG models are placed in terrain zones yet")
      holder = eqArchive.EQArchive(self.clientRoot / definition["archive"])
      model = eqgFiles.parseModel(holder.read(definition["entry"]), f"{definition['archive']}:{definition['entry']}")
      if model["bones"] is not None:
        raise ValueError(f"Zone '{self.zoneName}' places {modelName}, a skinned model; only static models are placed yet")
      self.models[modelName] = model
      if definition["archive"] not in self.archives:
        self.archives.append(definition["archive"])
    return self.models[modelName]

  def part(self, model, transform, position, colors):
    textures, alphaModes = eqModels.eqgMaterialTextures(model["materials"], model["triangleMaterials"], {})
    normals = model["normals"] @ numpy.linalg.inv(transform)
    normals /= numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    return eqModels.meshPart(model["vertices"] @ transform.T + position, model["triangles"], eqModels.staticEQGUVs(model["uvs"]), textures, alphaModes, {"normals": normals, "colors": colors})


def terrainAtlasLayout(terrain):
  """Where tile textures sit in the zone's atlases: the tile grid's first longitude and latitude, and the atlas width and height (tile
  columns along x, rows along y)."""
  longitudes = [tile["longitude"] for tile in terrain["tiles"]]
  latitudes = [tile["latitude"] for tile in terrain["tiles"]]
  side = eqTerrainTextures.textureSide
  origin = (min(longitudes), min(latitudes))
  return origin, ((max(longitudes) - origin[0] + 1) * side, (max(latitudes) - origin[1] + 1) * side)


def terrainTileParts(terrain, comboIndex):
  """Each tile as a mesh part: the client's vertices (x and y whole units, heights in eighths, truncated), atlas and detail
  coordinates, tint and baked light, and packed normals."""
  quads = terrain["header"]["quadsPerTile"]
  vertexSide = quads + 1
  spacing = terrain["header"]["unitsPerVertex"]
  side = eqTerrainTextures.textureSide
  (originLongitude, originLatitude), (atlasWidth, atlasHeight) = terrainAtlasLayout(terrain)
  tilesByGrid = {(tile["longitude"], tile["latitude"]): tile for tile in terrain["tiles"]}
  rows, columns = numpy.meshgrid(numpy.arange(vertexSide), numpy.arange(vertexSide), indexing="ij")
  # A tile's texture coordinate runs from its first texel's center to its last's, stored in 256ths of the texture (0x100ac170).
  halfTexel = 0.5 / side
  across = numpy.trunc((halfTexel + columns.ravel() / quads * (1 - 2 * halfTexel)) * 256) / 256 * side
  along = numpy.trunc((halfTexel + rows.ravel() / quads * (1 - 2 * halfTexel)) * 256) / 256 * side
  parts = []
  for tile in terrain["tiles"]:
    longitude, latitude = tile["longitude"], tile["latitude"]
    neighbors = [tilesByGrid.get(grid) for grid in ((longitude - 1, latitude), (longitude + 1, latitude), (longitude, latitude - 1), (longitude, latitude + 1))]
    normals = eqTerrainTextures.vertexNormals(tile["heights"].astype(numpy.float64), [None if neighbor is None else neighbor["heights"].astype(numpy.float64) for neighbor in neighbors], spacing)
    vertices = numpy.stack([tile["x"] + columns.ravel() * spacing, tile["y"] + rows.ravel() * spacing, numpy.trunc(tile["heights"].ravel().astype(numpy.float64) * 8) / 8], axis=1)
    atlasUVs = numpy.stack([((longitude - originLongitude) * side + across) / atlasWidth, ((latitude - originLatitude) * side + along) / atlasHeight], axis=1)
    detailUVs = numpy.stack([columns.ravel() / quads, rows.ravel() / quads], axis=1)
    triangles = eqgTerrain.tileTriangles(tile["quadFlags"])
    combo = comboIndex[tuple(layer["ecosystem"] for layer in tile["layers"])]
    part = eqModels.meshPart(
      vertices, triangles, atlasUVs, [f"terrain:{combo}"] * len(triangles), ["opaque"] * len(triangles),
      {"normals": eqTerrainTextures.packedNormals(normals).reshape(-1, 3), "colors": bytesRGBA(tile["baked"].ravel())},
    )
    parts.append(part | {"detailUVs": detailUVs, "vertexTints": bytesRGBA(tile["tints"].ravel())})
  return parts


def writeTerrainTextures(zoneFolder, terrain, textures, combos, ecosystems, archive):
  """The atlases (a color map and a detail mask image per ecosystem slot, saved bottom row first as Blender reads images) and the detail
  textures, described in terrain.json for the bridge's terrain materials."""
  side = eqTerrainTextures.textureSide
  (originLongitude, originLatitude), (width, height) = terrainAtlasLayout(terrain)
  slots = max(len(combo) for combo in combos)
  atlases = {kind: [numpy.zeros((height, width, 4), dtype=numpy.uint8) for _ in range(slots)] for kind in ("colorMap", "detailMask")}
  for tile in terrain["tiles"]:
    row, column = (tile["latitude"] - originLatitude) * side, (tile["longitude"] - originLongitude) * side
    for slot, ecosystemTextures in enumerate(textures[(tile["x"], tile["y"])]):
      for kind, images in atlases.items():
        images[slot][row:row + side, column:column + side] = ecosystemTextures[kind]
  fileNames = {kind: [] for kind in atlases}
  for kind, images in atlases.items():
    for slot, image in enumerate(images):
      fileName = f"terrain{kind[0].upper()}{kind[1:]}{slot}.png"
      Image.fromarray(image[::-1]).save(zoneFolder / fileName)
      fileNames[kind].append(fileName)
  detailFiles = {}
  for ecosystem in sorted({name for combo in combos for name in combo}):
    for layer in ecosystems[ecosystem]:
      if layer["detailMap"] not in detailFiles:
        detailFiles[layer["detailMap"]], readable = eqTextures.readableTexture(layer["detailMap"], readEntry(archive, layer["detailMap"]))
        (zoneFolder / detailFiles[layer["detailMap"]]).write_bytes(readable)
  description = {"combos": [
    [
      {"colorMap": fileNames["colorMap"][slot], "detailMask": fileNames["detailMask"][slot], "details": [{"texture": detailFiles[layer["detailMap"]], "repeat": layer["detailRepeat"]} for layer in ecosystems[ecosystem]]}
      for slot, ecosystem in enumerate(combo)
    ]
    for combo in combos
  ]}
  (zoneFolder / "terrain.json").write_text(json.dumps(description, indent=1), encoding="utf-8")


def buildTerrainZone(clientRoot, cacheRoot, zoneName, source, zoneFolder):
  """An EQ terrain zone's tiles, the objects they place on the ground, and the object groups they place."""
  archive = eqArchive.EQArchive(source["archive"])
  terrain = eqgTerrain.parseTerrain(archive.read(source["zon"]).decode("latin1"), archive.read(source["zon"][:-4] + ".dat"), zoneName)
  kinds = eqgTerrain.quadKinds(terrain)
  if kinds:
    raise ValueError(f"Zone '{zoneName}' marks terrain quads with kind bits {kinds}, whose drawing is not traced yet")
  if any(not tile["layers"] for tile in terrain["tiles"]):
    raise ValueError(f"Zone '{zoneName}' has tiles without ecosystems, which the client draws with its default texture; not read yet")
  ecosystems = {}
  for tile in terrain["tiles"]:
    for layer in tile["layers"]:
      name = layer["ecosystem"]
      if name not in ecosystems:
        ecosystems[name] = eqgTerrain.parseEcosystem(readEntry(archive, name + ".eco").decode("latin1"), f"{source['archive'].name}:{name}.eco")
        if len(ecosystems[name]) > eqTerrainTextures.maximumDetailLayers:
          raise ValueError(f"Ecosystem {name} has {len(ecosystems[name])} texture layers; the client's terrain effects draw at most {eqTerrainTextures.maximumDetailLayers}")
  covers = {}
  for layers in ecosystems.values():
    for layer in layers:
      if layer["coverMap"] not in covers:
        covers[layer["coverMap"]] = eqTerrainTextures.coverImage(readEntry(archive, layer["coverMap"]), eqTerrainTextures.textureSide, layer["coverMap"])
  textures = eqTerrainTextures.tileTextures(terrain, ecosystems, covers)
  combos = sorted({tuple(layer["ecosystem"] for layer in tile["layers"]) for tile in terrain["tiles"]})
  parts = terrainTileParts(terrain, {combo: index for index, combo in enumerate(combos)})
  tileCount = len(parts)
  objects = TerrainObjects(clientRoot, cacheRoot, zoneName)
  tilesByOrigin = {(tile["x"], tile["y"]): tile for tile in terrain["tiles"]}
  placedCounts = {}
  for placement in terrain["placements"]:
    model = objects.model(placement["model"])
    if model is None:
      continue
    transform = eqgTerrain.placementMatrix(placement["rotationDegrees"], placement["scale"])
    colors = numpy.tile(numpy.array(unlitColor, dtype=numpy.uint8), (len(model["vertices"]), 1))
    parts.append(objects.part(model, transform, eqgTerrain.placedPosition(terrain, tilesByOrigin, placement), colors))
    placedCounts[placement["model"]] = placedCounts.get(placement["model"], 0) + 1
  missingGroups, litMismatches = set(), set()
  for group in terrain["groups"]:
    groupEntry = group["group"] + ".tog"
    if groupEntry not in archive.entries:
      missingGroups.add(group["group"])
      continue
    groupTransform = eqgTerrain.placementMatrix(group["rotationDegrees"], group["scale"])
    groupPosition = eqgTerrain.placedPosition(terrain, tilesByOrigin, group)
    for member in parseObjectGroup(archive.read(groupEntry).decode("latin1"), f"{source['archive'].name}:{groupEntry}"):
      model = objects.model(member["model"])
      if model is None:
        continue
      colors = litColors(readEntry(archive, member["lit"]), len(model["vertices"]), member["lit"])
      if colors is None:
        litMismatches.add(member["lit"])
        colors = numpy.tile(numpy.array(unlitColor, dtype=numpy.uint8), (len(model["vertices"]), 1))
      transform = groupTransform @ eqgTerrain.placementMatrix(member["rotationDegrees"], member["scale"])
      parts.append(objects.part(model, transform, groupPosition + groupTransform @ numpy.array(member["position"]), colors))
      placedCounts[member["model"]] = placedCounts.get(member["model"], 0) + 1
  textureHolders = [archive] + [eqArchive.EQArchive(clientRoot / name) for name in objects.archives if name != source["archive"].name.lower()]
  written = eqModels.writePartsCache(zoneFolder, parts, textureHolders, f"Zone '{zoneName}'")
  writeTerrainTextures(zoneFolder, terrain, textures, combos, ecosystems, archive)
  return {
    "tiles": tileCount, "ecosystems": sorted(ecosystems), "terrainCombos": [list(combo) for combo in combos], "placements": len(terrain["placements"]),
    "objectGroups": len(terrain["groups"]), "missingObjectGroups": sorted(missingGroups), "litFilesNotMatchingModels": sorted(litMismatches),
    "placedObjects": sum(placedCounts.values()),
    "objectArchives": objects.archives, "missingModels": sorted(objects.missing), "particleCloudsNotDrawn": 0,
  } | written
