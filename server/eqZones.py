"""EverQuest zones built from the client's files into a cache the bridge places as one mesh, with the normals and vertex colors the
client lights them by: a classic (WLD) zone's region meshes and the objects its objects.wld places, an EQ terrain zone's tiles,
textured as the client textures them, and the objects and object groups they place, or an EQG (EQGZ) zone's terrain and objects,
including a zone file zonewright exported."""
import hashlib
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

zoneCacheFormat = 10
# A model's vertex light where its file gives none: no baked light and the full share of scene light, an assumption until the client's
# lighting of EQG objects is traced.
unlitColor = (0, 0, 0, 255)
anglesPerTurn = 512
# objects.wld placement (0x15) flags the client reads its fields by (EQGraphicsDX9.dll 0x1001cad0): 0x1 shifts them past one more
# field; 0x2, 0x4, and 0x8 store the position and turns, then two scales; 0x100 a reference to the placement's own vertex colors.
placementLayoutFlags = 0xF
placementLayout = 0xE
instanceColorsFlag = 0x100
instanceColorsOffset = 52
# The vertex light the client's region builder gives a zone mesh that stores no colors (EQGraphicsDX9.dll 0x1001f7d2: 0xFF1F1F1F).
colorlessRegionColor = (0x1F, 0x1F, 0x1F, 0xFF)


def objectPlacements(objectsFile):
  """objects.wld's object placements (0x15): the actor, position, heading and tilt in 512ths of a turn, a uniform scale, and the
  placement's own vertex colors (flag 0x100, RGBA bytes) or None."""
  placements = []
  for fragment in objectsFile.fragmentsOfType(0x15):
    body = fragment.body
    nameReference, flags = struct.unpack_from("<iI", body, 4)
    actor = objectsFile.lookupName(nameReference)
    if flags & placementLayoutFlags != placementLayout:
      raise ValueError(f"{objectsFile.sourceName}: {actor} has placement flags {flags:#x}; only those storing a position, turns, and two scales are read")
    x, y, z, heading, tilt, roll, _, scaleY, scaleZ = struct.unpack_from("<9f", body, 16)
    if roll != 0 or scaleY != scaleZ:
      raise ValueError(f"{objectsFile.sourceName}: {actor} has roll {roll} and scales {scaleY}, {scaleZ}; only a heading, a tilt, and one scale are read")
    colors = objectsFile.vertexColorTrack(struct.unpack_from("<i", body, instanceColorsOffset)[0]) if flags & instanceColorsFlag else None
    placements.append({"actor": actor, "position": numpy.array((x, y, z)), "heading": heading, "tilt": tilt, "scale": scaleY, "colors": colors})
  return placements


def placementRotation(placement):
  """The rotation a placement gives its object: the tilt about the object's Y axis, then the heading about Z, counter-clockwise from
  above as a spawn's heading turns it."""
  heading = math.radians(placement["heading"] * 360 / anglesPerTurn)
  tilt = math.radians(placement["tilt"] * 360 / anglesPerTurn)
  aboutZ = numpy.array([[math.cos(heading), -math.sin(heading), 0], [math.sin(heading), math.cos(heading), 0], [0, 0, 1]])
  aboutY = numpy.array([[math.cos(tilt), 0, math.sin(tilt)], [0, 1, 0], [-math.sin(tilt), 0, math.cos(tilt)]])
  return aboutZ @ aboutY


def placedPart(part, placement, colors=None):
  """A part moved, turned, and scaled by its placement, its vertex colors replaced by colors when given."""
  rotation = placementRotation(placement)
  lighting = part["lighting"] and {"normals": part["lighting"]["normals"] @ rotation.T, "colors": part["lighting"]["colors"] if colors is None else colors}
  return part | {"vertices": (part["vertices"] * placement["scale"]) @ rotation.T + placement["position"], "lighting": lighting}


def staticPlacementParts(parts, placement, label):
  """A static actor's parts as its placement draws them, and whether its own colors run short of its vertices. A placement with its own
  colors gives them to the actor's one mesh by vertex index (EQGraphicsDX9.dll 0x10054630, 0x1008f6d0); one without them gets colors
  the client computes at load (0x100530f0), which are not drawn yet: it keeps the mesh's own vertex light here. Either way the model
  then holds baked light, so it takes only the lights marked for baked geometry, which lights.wld never marks (0x1000e45e asks the model,
  0x10056470). Where its own colors run short, the client reads on past their copy into whatever its memory pool holds next; the
  vertices past them keep the mesh's own vertex light here."""
  colors = placement["colors"]
  if colors is None:
    return [placedPart(part, placement) | {"takesAllLights": False} for part in parts], False
  if len(parts) != 1:
    raise ValueError(f"{label} gives {placement['actor']} its own vertex colors across {len(parts)} meshes; the client's static actor draws one")
  ownColors = parts[0]["lighting"]["colors"]
  short = len(colors) < len(ownColors)
  drawnColors = numpy.concatenate([colors, ownColors[len(colors):]]) if short else colors[:len(ownColors)]
  return [placedPart(parts[0], placement, drawnColors) | {"takesAllLights": False}], short


def zoneSource(clientRoot, zoneName):
  """The zone variant to build: the one the client loads, which importZone draws."""
  return zoneSources.loadedVariant(clientRoot, zoneName)[1]


def clientEQGZone(source, zoneName):
  """A client EQG zone's .zon, the loose one when the client has it."""
  zonBytes = source["zonPath"].read_bytes() if "zonPath" in source else eqArchive.EQArchive(source["archive"]).read(source["zon"])
  return eqgFiles.parseZone(zonBytes, zoneName)


def zoneLights(clientRoot, zoneName):
  """The lights a client zone places: lights.wld's point lights for a classic zone, the .zon's for an EQG zone (the loose one when the
  client has it); None for an EQ terrain zone, whose lights are not read."""
  source = zoneSource(clientRoot, zoneName)
  if source["format"] == "wld":
    archive = eqArchive.EQArchive(source["archive"])
    return eqWorldFile.WorldFile(archive.read("lights.wld"), f"{source['archive'].name}:lights.wld").pointLights() if "lights.wld" in archive.entries else []
  if source["format"] == "eqgz":
    return clientEQGZone(source, zoneName)["lights"]
  return None


def lightsNothing(light):
  """Whether a light lights nothing: the client keeps a light's radius as stored (EQGraphicsDX9.dll 0x100128f0), reaches with it as its
  region of influence (0x1000e4f0), scores it by radius squared over distance squared (0x1000ff61), and fades it to nothing at it
  (1 - min((distance / radius)^2, 1) in every SPL vertex shader), so a light of radius 0 lights no vertex."""
  return light["radius"] == 0


def unreadZoneFiles(clientRoot, zoneName):
  """Files beside a classic zone, named for it, that the client never opens: a Luclin zone's loose <zone>.dat (dawnshroud, grimling,
  and nine more). No code in eqgame.exe or EQGraphicsDX9.dll names it: the game's one .dat name is <zone>_switches.dat (eqgame.exe
  0x512850), and the DLL's are the EQ terrain system's, read from the zone's .eqg (0x1010c0d0)."""
  path = clientRoot / f"{zoneName}.dat"
  return [path.name.lower()] if zoneSource(clientRoot, zoneName)["format"] == "wld" and path.is_file() else []


def zoneLineBoxes(regions):
  """A .zon's ATP_ regions as zone-line guides, each its name and box (eqgFiles.regionBox), and the names of those with a tilt field set,
  listed apart since how the client reads those is untraced."""
  boxes = [region | eqgFiles.regionBox(region) for region in regions if eqgFiles.isZoneLine(region["name"])]
  return {
    "zoneLines": [{key: box[key] for key in ("name", "center", "halfExtents", "headingDegrees")} for box in boxes if "tiltFields" not in box],
    "zoneLinesTilted": [box["name"] for box in boxes if "tiltFields" in box],
  }


def zoneLines(clientRoot, zoneName):
  """The zone lines of the variant importZone draws: an EQG zone's (zoneLineBoxes); None for a classic or EQ terrain zone, whose zone-line
  regions are not read."""
  source = zoneSource(clientRoot, zoneName)
  return zoneLineBoxes(clientEQGZone(source, zoneName)["regions"]) if source["format"] == "eqgz" else None


def zoneFileLights(archivePath):
  """The lights in an EQG zone archive's one .zon."""
  archive = eqArchive.EQArchive(archivePath)
  zoneFiles = [name for name in archive.entries if name.endswith(".zon")]
  if len(zoneFiles) != 1:
    raise ValueError(f"{archivePath.name} holds {len(zoneFiles)} .zon files; a zone archive holds one")
  return eqgFiles.parseZone(archive.read(zoneFiles[0]), zoneFiles[0])["lights"]


def buildZone(clientRoot, cacheRoot, zoneName):
  """Write a zone into a cache folder as one mesh, reusing one built from the same client files. An object no archive the zone loads
  defines is left out, as the client draws nothing for it, and listed."""
  source = zoneSource(clientRoot, zoneName)
  zoneFolder = cacheRoot / "zones" / f"{zoneName}@{source['format']}"
  stampPath = zoneFolder / "source.json"
  if source["format"] == "eqgz":
    archiveNames = [path.name.lower() for path in zoneSources.assetArchivePaths(clientRoot, source)[0]]
  else:
    archiveNames = [source["archive"].name.lower()] + [link["archive"] for link in eqModels.loadOrder(clientRoot, zoneName)[0] if link["tier"] == "zone"]
  stamp = {
    "zoneCacheFormat": zoneCacheFormat, "modelCacheFormat": eqModels.modelCacheFormat, "indexFormat": eqModels.indexFormat,
    "archives": eqModels.archiveStamp(clientRoot, sorted(set(archiveNames))), "listingFingerprint": zoneSources.clientListingFingerprint(clientRoot),
  }
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored.get(key) for key in stamp} == json.loads(json.dumps(stamp)):
      return zoneFolder, stored
  builder = {"wld": buildClassicZone, "eqtzp": buildTerrainZone, "eqgz": buildClientEQGZone}[source["format"]]
  details = stamp | {"zone": zoneName, "format": source["format"], "archive": source["archive"].name.lower()} | builder(clientRoot, cacheRoot, zoneName, source, zoneFolder)
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return zoneFolder, details


def buildClassicZone(clientRoot, cacheRoot, zoneName, source, zoneFolder):
  """A classic zone's region meshes and the objects its objects.wld places."""
  archive = eqArchive.EQArchive(source["archive"])
  worldFile = eqWorldFile.WorldFile(archive.read(f"{zoneName}.wld"), f"{source['archive'].name}:{zoneName}.wld")
  label = f"Zone '{zoneName}'"
  # A zone's regions take only the lights marked for baked geometry, which lights.wld never marks (EQGraphicsDX9.dll 0x1000db20).
  parts = [eqModels.wldMeshPart(mesh, {}, colorlessRegionColor) | {"takesAllLights": False} for mesh in worldFile.meshes()]
  regionMeshCount = len(parts)
  placements = objectPlacements(eqWorldFile.WorldFile(archive.read("objects.wld"), f"{source['archive'].name}:objects.wld")) if "objects.wld" in archive.entries else []
  objectParts, missingModels, objectArchives, placedCounts, particleClouds, colorsIgnored, colorsShort, litAtLoad = {}, set(), [], {}, 0, 0, {}, 0
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
        objectParts[actor] = {"skeletal": False, "parts": eqModels.wldStaticParts(holder, definition, {}, None)["parts"]}
      elif definition["kind"] == "wldSkeletal":
        skeletalParts, clouds = eqModels.wldSkeletalBindParts(holder, definition)
        objectParts[actor] = {"skeletal": True, "parts": skeletalParts}
        particleClouds += clouds
      else:
        raise ValueError(f"{label} places {actor}, a {definition['kind']} model from {definition['archive']}; only WLD objects are placed yet")
      if definition["archive"] not in objectArchives:
        objectArchives.append(definition["archive"])
    if objectParts[actor] is None:
      continue
    if objectParts[actor]["skeletal"]:
      # A skeletal actor never reads its placement's colors (EQGraphicsDX9.dll 0x10044550) and, holding no model whose baked light
      # 0x1000e45e asks after (0x10102930), takes every light.
      colorsIgnored += placement["colors"] is not None
      parts += [placedPart(part, placement) | {"takesAllLights": True} for part in objectParts[actor]["parts"]]
    else:
      placedParts, short = staticPlacementParts(objectParts[actor]["parts"], placement, label)
      parts += placedParts
      litAtLoad += placement["colors"] is None
      if short:
        entry = colorsShort.setdefault(actor, {"actor": actor, "vertices": len(placedParts[0]["vertices"]), "colorCounts": [], "placements": 0})
        entry["colorCounts"] = sorted(set(entry["colorCounts"]) | {len(placement["colors"])})
        entry["placements"] += 1
    placedCounts[actor] = placedCounts.get(actor, 0) + 1
  textureHolders = [archive] + [eqArchive.EQArchive(clientRoot / name) for name in objectArchives]
  written = eqModels.writePartsCache(zoneFolder, parts, textureHolders, label)
  return {
    "regionMeshes": regionMeshCount, "placements": len(placements), "placedObjects": sum(placedCounts.values()), "objectArchives": objectArchives,
    "missingModels": sorted(missingModels), "particleCloudsNotDrawn": particleClouds, "placementColorsIgnoredBySkeletalActors": colorsIgnored,
    "placementColorsShort": [colorsShort[actor] for actor in sorted(colorsShort)], "placementsLitAtLoadNotDrawn": litAtLoad,
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
    objects.append({
      "model": fields["NAME"][0].lower() + ".mod", "position": tuple(float(value) for value in fields["POSITION"]),
      "rotationDegrees": tuple(float(value) for value in fields["ROTATION"]), "scale": float(fields["SCALE"][0]), "lit": fields["FILE"][1].lower(),
    })
  return objects


def memberBakedLight(archive, litName, vertexCount):
  """A group member's baked light as the client takes it: RGBA per vertex, or None and why the member draws without it. Without the
  file in the archive no light data is set (EQGraphicsDX9.dll 0x100a5810, 0x10053aeb); a count that differs from the model's vertices
  is ignored (0x100548d0); and a file holding fewer colors than its count is copied whole by that count from the loader's pooled
  buffer (0x100cb540), so the rest comes from memory past the file's end."""
  if litName not in archive.entries:
    return None, "missing"
  litBytes = archive.read(litName)
  count = struct.unpack_from("<I", litBytes, 0)[0]
  if count != vertexCount:
    return None, "notFitting"
  if len(litBytes) < 4 + 4 * count:
    return None, "short"
  return bytesRGBA(numpy.frombuffer(litBytes, dtype="<u4", count=count, offset=4)), None


def groupMemberTransform(group, member):
  """How an object group places a member (0x101038c0): from the group's position, a height in the world, by the member's offset turned
  by the group's turns and scaled by the group's x scale, lifted by the group's tenth value times that scale; the member turns by its
  own turns plus the group's and scales by its scale times the group's on each axis."""
  scaleX = group["scale"][0]
  offset = eqgTerrain.placementMatrix(group["rotationDegrees"], (1.0, 1.0, 1.0)) @ numpy.array(member["position"])
  position = numpy.array(group["position"]) + scaleX * offset + numpy.array((0.0, 0.0, scaleX * group["memberLift"]))
  turns = tuple(groupTurn + memberTurn for groupTurn, memberTurn in zip(group["rotationDegrees"], member["rotationDegrees"]))
  return eqgTerrain.placementMatrix(turns, tuple(axis * member["scale"] for axis in group["scale"])), position


def placedEQGPart(model, transform, position, colors):
  """A static EQG model placed by a transform (rotation and scale) and a position, its normals turned with it, lit by colors (RGBA per
  vertex), with the triangles its file lets players through."""
  textures, alphaModes = eqModels.eqgMaterialTextures(model["materials"], model["triangleMaterials"], {})
  normals = model["normals"] @ numpy.linalg.inv(transform)
  normals /= numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  return eqModels.meshPart(model["vertices"] @ transform.T + position, model["triangles"], eqModels.staticEQGUVs(model["uvs"]), textures, alphaModes,
    {"normals": normals, "colors": colors}, eqModels.eqgLiquids(model["materials"], model["triangleMaterials"]), model["triangleFlags"] & eqgFiles.passableFlag)


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


def loadsFromArchive(archive, name):
  return name != eqTerrainTextures.defaultMap and name in archive.entries


def terrainMaps(archive, ecosystems):
  """The cover and blend maps the ecosystems' layers name, at the tile texture size, None for one the archive lacks (the terrain's
  bitmap lookup then finds none, 0x100ea060), and the names of maps the archive lacks, by kind. A layering map that loads is refused:
  no client zone ships one, so how the client applies one is not checked against any."""
  covers, blends = {}, {}
  missing = {kind: set() for kind in ("coverMaps", "blendMaps", "layeringMaps", "detailMaps", "normalMaps")}
  side = eqTerrainTextures.textureSide
  for layers in ecosystems.values():
    for layer in layers:
      cover = layer["coverMap"]
      if cover not in covers:
        covers[cover] = eqTerrainTextures.coverImage(archive.read(cover), side, cover) if loadsFromArchive(archive, cover) else None
      blend = layer.get("blendMap", eqTerrainTextures.defaultMap)
      if blend != eqTerrainTextures.defaultMap and blend not in blends:
        blends[blend] = eqTerrainTextures.blendImage(archive.read(blend), side, blend) if loadsFromArchive(archive, blend) else None
      layering = layer.get("layeringMap", eqTerrainTextures.defaultMap)
      if loadsFromArchive(archive, layering):
        raise ValueError(f"{archive.archivePath.name}: layer {layer['name']} names layering map {layering}, which is not drawn yet")
      named = (("coverMaps", cover), ("blendMaps", blend), ("layeringMaps", layering), ("detailMaps", layer["detailMap"]), ("normalMaps", layer.get("normalMap", eqTerrainTextures.defaultMap)))
      for kind, name in named:
        if name != eqTerrainTextures.defaultMap and name not in archive.entries:
          missing[kind].add(name)
  return covers, blends, {kind: sorted(names) for kind, names in missing.items()}


def writeTerrainTextures(zoneFolder, terrain, textures, combos, ecosystems, archive):
  """The atlases (a color map and a detail mask image per ecosystem slot, saved bottom row first as Blender reads images) and the detail
  textures, described in terrain.json for the bridge's terrain materials. A detail map that does not load leaves its sampler empty,
  which the client's shaders read as black (Direct3D 9: a sampler without a texture returns 0, 0, 0, 1). An ecosystem whose first
  layer has no normal map draws with Terrain_<n>Detail, which does not double the tint, instead of Terrain_Bump<n>Detail (0x1008fe20)."""
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
      detail = layer["detailMap"]
      if detail in detailFiles:
        continue
      if loadsFromArchive(archive, detail):
        detailFiles[detail], readable = eqTextures.readableTexture(detail, archive.read(detail))
        (zoneFolder / detailFiles[detail]).write_bytes(readable)
      else:
        detailFiles[detail] = "detailUnloaded.png"
        Image.new("RGB", (1, 1), (0, 0, 0)).save(zoneFolder / detailFiles[detail])
  description = {"combos": [
    [
      {
        "colorMap": fileNames["colorMap"][slot], "detailMask": fileNames["detailMask"][slot],
        "tintScale": 2.0 if loadsFromArchive(archive, ecosystems[ecosystem][0].get("normalMap", eqTerrainTextures.defaultMap)) else 1.0,
        "details": [{"texture": detailFiles[layer["detailMap"]], "repeat": layer["detailRepeat"]} for layer in ecosystems[ecosystem]],
      }
      for slot, ecosystem in enumerate(combo)
    ]
    for combo in combos
  ]}
  (zoneFolder / "terrain.json").write_text(json.dumps(description, indent=1), encoding="utf-8")


def buildTerrainZone(clientRoot, cacheRoot, zoneName, source, zoneFolder):
  """An EQ terrain zone's tiles, the objects they place on the ground, and the object groups they place. Hole quads, maps the archive
  lacks, and baked light the client does not take are drawn as the client draws them and listed."""
  archive = eqArchive.EQArchive(source["archive"])
  terrain = eqgTerrain.parseTerrain(*zoneSources.terrainFiles(source), zoneName)
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
  covers, blends, missingMaps = terrainMaps(archive, ecosystems)
  textures = eqTerrainTextures.tileTextures(terrain, ecosystems, covers, blends)
  combos = sorted({tuple(layer["ecosystem"] for layer in tile["layers"]) for tile in terrain["tiles"]})
  parts = [part | {"takesAllLights": False} for part in terrainTileParts(terrain, {combo: index for index, combo in enumerate(combos)})]
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
    parts.append(placedEQGPart(model, transform, eqgTerrain.placedPosition(terrain, tilesByOrigin, placement), colors) | {"takesAllLights": True})
    placedCounts[placement["model"]] = placedCounts.get(placement["model"], 0) + 1
  missingGroups = set()
  litProblems = {"missing": set(), "notFitting": set(), "short": set()}
  for group in terrain["groups"]:
    groupEntry = group["group"] + ".tog"
    if groupEntry not in archive.entries:
      missingGroups.add(group["group"])
      continue
    for member in parseObjectGroup(archive.read(groupEntry).decode("latin1"), f"{source['archive'].name}:{groupEntry}"):
      model = objects.model(member["model"])
      if model is None:
        continue
      colors, problem = memberBakedLight(archive, member["lit"], len(model["vertices"]))
      if problem is not None:
        litProblems[problem].add(member["lit"])
        colors = numpy.tile(numpy.array(unlitColor, dtype=numpy.uint8), (len(model["vertices"]), 1))
      parts.append(placedEQGPart(model, *groupMemberTransform(group, member), colors) | {"takesAllLights": problem is not None})
      placedCounts[member["model"]] = placedCounts.get(member["model"], 0) + 1
  textureHolders = [archive] + [eqArchive.EQArchive(clientRoot / name) for name in objects.archives if name != source["archive"].name.lower()]
  written = eqModels.writePartsCache(zoneFolder, parts, textureHolders, f"Zone '{zoneName}'")
  writeTerrainTextures(zoneFolder, terrain, textures, combos, ecosystems, archive)
  return {
    "tiles": tileCount, "holeQuads": eqgTerrain.holeQuadCount(terrain), "ecosystems": sorted(ecosystems), "terrainCombos": [list(combo) for combo in combos],
    "terrainMapsMissing": {kind: names for kind, names in missingMaps.items() if names}, "placements": len(terrain["placements"]),
    "objectGroups": len(terrain["groups"]), "missingObjectGroups": sorted(missingGroups), "litFilesMissing": sorted(litProblems["missing"]),
    "litFilesNotMatchingModels": sorted(litProblems["notFitting"]), "litFilesShorterThanTheirCount": sorted(litProblems["short"]),
    "placedObjects": sum(placedCounts.values()), "looseZoneFile": "zonPath" in source,
    "objectArchives": objects.archives, "missingModels": sorted(objects.missing), "particleCloudsNotDrawn": 0,
  } | written


def eqgLitColors(litBytes, sourceName):
  """An EQG placement's .lit file (EQGP): a D3DCOLOR per vertex."""
  if litBytes[:4] != b"EQGP":
    raise ValueError(f"{sourceName}: not an EQGP baked light file")
  count = struct.unpack_from("<I", litBytes, 4)[0]
  if len(litBytes) != 8 + 4 * count:
    raise ValueError(f"{sourceName}: {count} colors in {len(litBytes)} bytes")
  return numpy.frombuffer(litBytes, dtype="<u4", count=count, offset=8)


def eqgZoneParts(library, zone, zoneArchive, label):
  """An EQG zone's placements as mesh parts. Baked light comes from the .zon (version 2) or each placement's <name>.lit (version 1)
  and is drawn only where its count equals the model's vertices, as the client takes it (EQGraphicsDX9.dll 0x100548d0). The client
  computes its own light for a placement whose count differs; that is not drawn yet, so those placements are listed and drawn with
  none. A model no linked archive holds is left out, as the client draws nothing for it."""
  parts, missing, notFitting, placedCounts = [], set(), [], {}
  for placement in zone["placements"]:
    model = library.model(placement["model"])
    if model is None:
      missing.add(placement["model"])
      continue
    if model["bones"] is not None:
      raise ValueError(f"{label} places {placement['model']}, a skinned model; only static models are placed yet")
    vertexCount = len(model["vertices"])
    colors = placement["colors"]
    if colors is None:
      litName = placement["name"].lower() + ".lit"
      colors = eqgLitColors(zoneArchive.read(litName), f"{label}:{litName}") if litName in zoneArchive.entries else None
    if colors is not None and len(colors) and len(colors) != vertexCount:
      notFitting.append(placement["name"])
      colors = None
    baked = colors is not None and len(colors) > 0
    rgba = bytesRGBA(colors) if baked else numpy.tile(numpy.array(unlitColor, dtype=numpy.uint8), (vertexCount, 1))
    # The terrain is drawn as the zone's regions, which take only the lights marked for baked geometry (0x1000db20).
    parts.append(placedEQGPart(model, *eqgFiles.drawnTransform(placement), rgba) | {"takesAllLights": not baked and not placement["model"].endswith(".ter")})
    placedCounts[placement["model"]] = placedCounts.get(placement["model"], 0) + 1
  if not parts:
    raise ValueError(f"{label}: none of its {len(zone['placements'])} placements has a model in {[archive.archivePath.name for archive in library.archives]}")
  return parts, {
    "zoneVersion": zone["version"], "placements": len(zone["placements"]), "placedObjects": sum(placedCounts.values()),
    "missingModels": sorted(missing), "bakedLightNotFitting": notFitting, "particleCloudsNotDrawn": 0,
  }


def buildClientEQGZone(clientRoot, cacheRoot, zoneName, source, zoneFolder):
  """A client EQG zone: its .zon (the loose one when the client has it) and the models its archive and asset archives hold."""
  archivePaths, missingArchives = zoneSources.assetArchivePaths(clientRoot, source)
  library = zoneSources.ModelLibrary(archivePaths, zoneName)
  label = f"Zone '{zoneName}'"
  parts, details = eqgZoneParts(library, clientEQGZone(source, zoneName), library.archives[0], label)
  written = eqModels.writePartsCache(zoneFolder, parts, library.archives, label)
  return details | {"looseZoneFile": "zonPath" in source, "missingAssetArchives": missingArchives} | written


def zoneFileBoundaries(archivePath):
  """What an EQG zone archive holds of its player boundaries: its terrain's invisible walls (material -1 triangles that block, in world
  positions, or None without any); its zone lines (zoneLineBoxes); and how many triangles each model lets players through."""
  archive = eqArchive.EQArchive(archivePath)
  zoneFiles = [name for name in archive.entries if name.endswith(".zon")]
  if len(zoneFiles) != 1:
    raise ValueError(f"{archivePath.name} holds {len(zoneFiles)} .zon files; a zone archive holds one")
  zone = eqgFiles.parseZone(archive.read(zoneFiles[0]), zoneFiles[0])
  models = {name: eqgFiles.parseModel(archive.read(name), f"{archivePath.name}:{name}") for name in zone["modelNames"] if name in archive.entries}
  positions, triangles, offset = [], [], 0
  for placement in zone["placements"]:
    model = models.get(placement["model"])
    if model is None or not placement["model"].endswith(".ter"):
      continue
    walls = (model["triangleMaterials"] == eqgFiles.noMaterial) & ((model["triangleFlags"] & eqgFiles.passableFlag) == 0)
    if walls.any():
      used, remapped = numpy.unique(model["triangles"][walls], return_inverse=True)
      positions.append(eqgFiles.placeVertices(model["vertices"][used], placement))
      triangles.append(remapped.reshape(-1, 3) + offset)
      offset += len(used)
  return zoneLineBoxes(zone["regions"]) | {
    "walls": {"positions": numpy.concatenate(positions).tolist(), "triangles": numpy.concatenate(triangles).tolist()} if positions else None,
    "passableTriangles": {name: int((model["triangleFlags"] & eqgFiles.passableFlag).astype(bool).sum()) for name, model in models.items() if (model["triangleFlags"] & eqgFiles.passableFlag).any()},
  }


def buildZoneFile(cacheRoot, archivePath):
  """An EQG zone archive outside the client, such as one zonewright exported: its one .zon and the models and textures it holds,
  cached by the archive's SHA-256."""
  data = archivePath.read_bytes()
  digest = hashlib.sha256(data).hexdigest()
  zoneName = archivePath.stem.lower()
  zoneFolder = cacheRoot / "zoneFiles" / f"{zoneName}@{digest[:12]}"
  stampPath = zoneFolder / "source.json"
  stamp = {"zoneCacheFormat": zoneCacheFormat, "modelCacheFormat": eqModels.modelCacheFormat, "sha256": digest}
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored.get(key) for key in stamp} == stamp:
      return zoneFolder, stored
  library = zoneSources.ModelLibrary([archivePath], zoneName)
  archive = library.archives[0]
  zoneFiles = [name for name in archive.entries if name.endswith(".zon")]
  if len(zoneFiles) != 1:
    raise ValueError(f"{archivePath.name} holds {len(zoneFiles)} .zon files; a zone archive holds one")
  label = f"Zone file '{archivePath.name}'"
  parts, details = eqgZoneParts(library, eqgFiles.parseZone(archive.read(zoneFiles[0]), zoneFiles[0]), archive, label)
  zoneFolder.mkdir(parents=True, exist_ok=True)
  written = eqModels.writePartsCache(zoneFolder, parts, [archive], label)
  details = stamp | {"zone": zoneName, "file": str(archivePath)} | details | written
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return zoneFolder, details
