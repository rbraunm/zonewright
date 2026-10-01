"""EverQuest models found through the client's own links (eqLinks) and built into per-model caches: EQG static (.mod) and skinned (.mds) models, and WLD static and skeletal actors."""
import concurrent.futures
import json
import multiprocessing
import re
import struct

import numpy

import eqArchive
import eqgFiles
import eqLinks
import eqSkeletons
import eqTextures
import eqWorldFile
import machineProfile
import zoneSources

indexFormat = 5
modelCacheFormat = 2
actorTrailingBytes = 4
staticKinds = ("eqgStatic", "wldStatic")


def actorReferences(worldFile, actorFragment):
  """The fragments an actor definition (0x14) draws: mesh references (0x2D) for a static actor, a skeleton reference (0x11) for an animated one."""
  body = actorFragment.body
  flags, _, actionCount, referenceCount, _ = struct.unpack_from("<Iiiii", body, 4)
  position = 24 + (4 if flags & 1 else 0) + (28 if flags & 2 else 0)
  for _ in range(actionCount):
    lodCount = struct.unpack_from("<I", body, position)[0]
    position += 8 + 4 * lodCount
  references = struct.unpack_from(f"<{referenceCount}i", body, position)
  if not 0 <= len(body) - (position + 4 * referenceCount) <= actorTrailingBytes:
    raise ValueError(f"{worldFile.sourceName}: actor '{actorFragment.name}' layout does not match its size")
  return [worldFile.fragments[reference - 1] for reference in references]


def actorKind(worldFile, actorFragment):
  """wldStatic or wldSkeletal when every mesh it draws is a 0x36 mesh; otherwise wldUnsupported with the fragment types found."""
  references = actorReferences(worldFile, actorFragment)
  types = sorted({fragment.fragmentType for fragment in references})
  if types == [0x2D]:
    meshes = [worldFile.fragments[struct.unpack_from("<i", reference.body, 4)[0] - 1] for reference in references]
    kind = "wldStatic"
  elif types == [0x11]:
    skeleton = worldFile.fragment(struct.unpack_from("<i", references[0].body, 4)[0], 0x10)
    dags, skins = eqSkeletons.readSkeleton(worldFile, skeleton)
    meshes = eqSkeletons.skinMeshes(worldFile, skins) + [mesh for _, mesh in eqSkeletons.boneAttachments(worldFile, dags)[0]]
    kind = "wldSkeletal"
  else:
    return "wldUnsupported:" + ",".join(f"{fragmentType:#x}" for fragmentType in types)
  meshTypes = sorted({mesh.fragmentType for mesh in meshes} - {0x36})
  return kind if not meshTypes else "wldUnsupported:mesh " + ",".join(f"{fragmentType:#x}" for fragmentType in meshTypes)


def archiveModels(archivePath):
  """Every model an archive defines, by model key; a key defined twice in one archive keeps both definitions."""
  archive = eqArchive.EQArchive(archivePath)
  models = {}
  for entryName in archive.names():
    if entryName.endswith((".mod", ".mds")):
      models.setdefault(eqLinks.modelKey(entryName[:-4]), []).append({"kind": "eqgStatic" if entryName.endswith(".mod") else "eqgSkinned", "entry": entryName})
  for wldName in [name for name in archive.names() if name.endswith(".wld")]:
    worldFile = eqWorldFile.WorldFile(archive.read(wldName), f"{archivePath.name}:{wldName}")
    for fragment in worldFile.fragmentsOfType(0x14):
      models.setdefault(eqLinks.modelKey(fragment.name), []).append({"kind": actorKind(worldFile, fragment), "wld": wldName, "actor": fragment.name})
  return models


def indexedArchive(archivePath):
  """Runs in a worker process. An archive this parser cannot read is recorded with its error, so resolving through it fails loudly."""
  try:
    return {"models": archiveModels(archivePath)}
  except (ValueError, KeyError, struct.error) as parseError:
    return {"error": f"{type(parseError).__name__}: {parseError}"}


def buildIndex(clientRoot):
  archivePaths = sorted(clientRoot.glob("*.eqg")) + sorted(clientRoot.glob("*.s3d"))
  with concurrent.futures.ProcessPoolExecutor(machineProfile.workerCount(), mp_context=multiprocessing.get_context("spawn")) as pool:
    return {archivePath.name.lower(): entry for archivePath, entry in zip(archivePaths, pool.map(indexedArchive, archivePaths, chunksize=8))}


indexMemo = {}


def loadIndex(clientRoot, cacheRoot):
  """Archive name to the models it defines, rebuilt when any archive is added, removed, or changed; kept in memory while the listing is unchanged."""
  fingerprint = zoneSources.clientListingFingerprint(clientRoot)
  memoKey = (str(clientRoot), str(cacheRoot), fingerprint)
  if memoKey in indexMemo:
    return indexMemo[memoKey]
  indexPath = cacheRoot / "modelIndex.json"
  stored = json.loads(indexPath.read_text(encoding="utf-8")) if indexPath.is_file() else None
  if stored is not None and stored["indexFormat"] == indexFormat and stored["clientRoot"] == str(clientRoot) and stored["listingFingerprint"] == fingerprint:
    archives = stored["archives"]
  else:
    archives = buildIndex(clientRoot)
    cacheRoot.mkdir(parents=True, exist_ok=True)
    indexPath.write_text(json.dumps({"indexFormat": indexFormat, "clientRoot": str(clientRoot), "listingFingerprint": fingerprint, "archives": archives}), encoding="utf-8")
  indexMemo.clear()
  indexMemo[memoKey] = archives
  return archives


def linkTiers(clientRoot, zoneName):
  """Where the client finds a model, most specific first: the zone's archives (when a zone is given), the player models eqclient.ini enables, the global load list."""
  zone = eqLinks.zoneLinks(clientRoot, zoneName) if zoneName is not None else {"archives": [], "missing": []}
  globals_ = eqLinks.globalLinks(clientRoot)
  return [
    ("zone", zone["archives"]),
    ("playerModels", [link for link in globals_["archives"] if link["via"] == "eqclient.ini"]),
    ("global", [link for link in globals_["archives"] if link["via"] != "eqclient.ini"]),
  ], zone["missing"] + globals_["missing"]


def definitionsIn(index, link, key):
  entry = index.get(link["archive"])
  if entry is None:
    raise ValueError(f"{link['archive']} (linked by {link['via']}) is not in the model index")
  if "error" in entry:
    raise ValueError(f"{link['archive']} (linked by {link['via']}) could not be read: {entry['error']}")
  return [definition | {"archive": link["archive"], "via": link["via"]} for definition in entry["models"].get(key, [])]


def findModel(clientRoot, cacheRoot, modelName, zoneName):
  """Every definition of a model the client could load in a zone, by link tier, plus on-demand entries and unlinked archives that also define it."""
  key = eqLinks.modelKey(modelName)
  index = loadIndex(clientRoot, cacheRoot)
  tiers, missingLinks = linkTiers(clientRoot, zoneName)
  linked = {}
  linkedArchives = set()
  for tierName, links in tiers:
    seen = set()
    # A character list can link one archive several times, each for one code; only links covering this model count.
    for link in links:
      if link["codes"] is not None and key not in link["codes"]:
        continue
      linkedArchives.add(link["archive"])
      if link["archive"] in seen:
        continue
      seen.add(link["archive"])
      definitions = definitionsIn(index, link, key)
      if definitions:
        linked.setdefault(tierName, []).extend(definitions)
  onDemand = [entry | {"kind": "eqgStatic" if entry["entry"].endswith(".mod") else "eqgSkinned"} for entry in eqLinks.onDemandResources(clientRoot).get(key, [])]
  unlinked = sorted(name for name, entry in index.items() if name not in linkedArchives and key in entry.get("models", {}) and name not in {item["archive"] for item in onDemand})
  return {"model": key, "linked": linked, "onDemand": onDemand, "unlinkedArchives": unlinked, "missingLinks": missingLinks}


def definitionSource(definition):
  return f"{definition['archive']}:{(definition.get('entry') or definition['actor']).lower()}"


def resolveModel(clientRoot, cacheRoot, modelName, zoneName, source=None):
  """The one definition the client uses: the first link tier that defines the model, else its on-demand entry. Several definitions in one tier are ambiguous unless source ("archive" or "archive:entry") picks one of them."""
  found = findModel(clientRoot, cacheRoot, modelName, zoneName)
  tiers = [(tierName, definitions) for tierName, definitions in found["linked"].items()] + ([("onDemand", found["onDemand"])] if found["onDemand"] else [])
  if source is not None:
    tiers = [(tierName, [definition for definition in definitions if source.lower() in (definition["archive"], definitionSource(definition))]) for tierName, definitions in tiers]
    tiers = [(tierName, definitions) for tierName, definitions in tiers if definitions]
  if not tiers:
    unlinkedNote = f"; unlinked archives define it: {found['unlinkedArchives']}" if found["unlinkedArchives"] else ""
    linker = f"zone '{zoneName}'" if zoneName is not None else "the global lists (no zone given)"
    raise ValueError(f"No archive {linker} links defines model '{found['model']}'{' as ' + source if source else ''}{unlinkedNote}")
  tierName, definitions = tiers[0]
  if len(definitions) > 1:
    raise ValueError(f"Model '{found['model']}' is defined {len(definitions)} times in the {tierName} tier: {[definitionSource(definition) for definition in definitions]}; pass one as source to choose")
  definition = definitions[0] | {"tier": tierName, "model": found["model"]}
  if definition["kind"].startswith("wldUnsupported"):
    raise ValueError(f"Model '{found['model']}' in {definition['archive']} is an actor of unsupported fragment types ({definition['kind'].split(':')[1]})")
  return definition


def textureArchives(clientRoot, zoneName, definition):
  """Archives a model's textures may come from: its own archive; for a zone's EQG model, also the zone's other EQG archives, which the client loads with it."""
  names = [definition["archive"]]
  if definition["tier"] == "zone" and definition["kind"].startswith("eqg"):
    names += [link["archive"] for link in eqLinks.zoneLinks(clientRoot, zoneName)["archives"] if link["archive"].endswith(".eqg") and link["archive"] not in names]
  return names


def triangleKeep(vertices, triangles):
  """Triangles whose vertices are all finite; a few client models carry NaN vertices that cannot render."""
  return numpy.isfinite(vertices).all(axis=1)[triangles].all(axis=1)


def eqgMaterialTextures(materials, triangleMaterials, diffuseSwaps):
  """Per triangle: its diffuse texture (a texture set's swap first) and whether its shader cuts out by alpha, or None for triangles the client does not draw."""
  textures, cutouts = [], []
  for materialIndex in triangleMaterials:
    diffuse = (diffuseSwaps.get(materialIndex) or materials[materialIndex]["properties"].get("e_TextureDiffuse0")) if materialIndex >= 0 else None
    textures.append(diffuse.lower() if diffuse else None)
    cutouts.append(bool(diffuse) and materials[materialIndex]["shader"].lower().startswith("chroma"))
  return textures, cutouts


def meshPart(vertices, triangles, uvs, textures, cutouts):
  """Drawn triangles only; triangles with non-finite vertices are dropped and counted."""
  finite = triangleKeep(vertices, triangles)
  keep = numpy.array([texture is not None for texture in textures], dtype=bool) & finite
  return {"vertices": vertices, "triangles": triangles[keep], "uvs": uvs, "textures": [texture for texture, kept in zip(textures, keep) if kept], "cutouts": [cutout for cutout, kept in zip(cutouts, keep) if kept], "dropped": int((~finite).sum())}


def eqgStaticParts(archive, definition, appearance):
  model = eqgFiles.parseModel(archive.read(definition["entry"]), f"{definition['archive']}:{definition['entry']}")
  return {"parts": [meshPart(model["vertices"], model["triangles"], model["uvs"], *eqgMaterialTextures(model["materials"], model["triangleMaterials"], {}))], "pose": "static"}


def chosenPieces(pieceNames, code, appearance, sourceName):
  """The client's piece rule (EQGraphicsDX9.dll): a reset shows body <code>00 and head <code>HE00, then variation and head type swap in <code><nn> and <code>HE<nn>, a swap to a piece the model lacks leaving the current one. Pieces outside those two groups are always drawn."""
  bodies = [name for name in pieceNames if re.fullmatch(rf"{code}\d\d", name)]
  heads = [name for name in pieceNames if re.fullmatch(rf"{code}HE\d\d", name)]
  chosen = [name for name in pieceNames if name not in bodies and name not in heads]
  for group, default, requested in ((bodies, f"{code}00", f"{code}{appearance['variation']:02d}"), (heads, f"{code}HE00", f"{code}HE{appearance['headType']:02d}")):
    if not group:
      continue
    if requested in group:
      chosen.append(requested)
    elif default in group:
      chosen.append(default)
    else:
      raise ValueError(f"{sourceName}: neither {requested} nor the default {default} is among its pieces {group}; which one the client shows is not known")
  return chosen


def eqgLayerDiffuses(archive, code, materialCount, textureSet, sourceName):
  """Per material index, the diffuse its texture-set layer C_<code>_S<set>_M<index + 1> swaps in (EQGraphicsDX9.dll), from the model's own .lay. Set 0 keeps the materials' textures."""
  layerEntry = f"{code.lower()}.lay"
  if textureSet == 0 or layerEntry not in archive.entries:
    return {}
  layers = eqgFiles.parseLayers(archive.read(layerEntry), f"{archive.archivePath.name}:{layerEntry}")
  swaps = {}
  for index in range(materialCount):
    textures = layers.get(f"C_{code}_S{textureSet:02d}_M{index + 1:02d}")
    if textures is None:
      continue
    colors = [texture for texture in textures if not texture.endswith("_n.dds")]
    if len(colors) != 1:
      raise ValueError(f"{sourceName}: layer C_{code}_S{textureSet:02d}_M{index + 1:02d} has {colors} besides normal maps; which is the diffuse is not known")
    swaps[index] = colors[0]
  return swaps


def eqgSkinnedParts(archive, definition, appearance):
  """The pieces the client shows for this appearance, textured by its texture set, in bind pose."""
  sourceName = f"{definition['archive']}:{definition['entry']}"
  model = eqgFiles.parseSkinnedModel(archive.read(definition["entry"]), sourceName)
  code = definition["model"].upper()
  pieces = {piece["name"].upper(): piece for piece in model["pieces"]}
  chosen = chosenPieces(list(pieces), code, appearance, sourceName)
  swaps = eqgLayerDiffuses(archive, code, len(model["materials"]), appearance["textureSet"], sourceName)
  parts = [meshPart(pieces[name]["vertices"], pieces[name]["triangles"], pieces[name]["uvs"], *eqgMaterialTextures(model["materials"], pieces[name]["triangleMaterials"], swaps)) for name in chosen]
  return {"parts": parts, "pose": "bind", "pieces": chosen, "swappedMaterials": len(swaps)}


def wldMeshPart(mesh, materialSwaps):
  if mesh["uvs"] is None:
    raise ValueError(f"mesh '{mesh['name']}' has no per-vertex UVs")
  materials = [materialSwaps.get(material["name"].upper(), material) for material in mesh["materials"]]
  textures, cutouts = [], []
  for index in mesh["triangleMaterials"]:
    material = materials[index]
    drawn = material["renderMethod"] != eqWorldFile.invisibleRenderMethod and bool(material["textureNames"])
    textures.append(material["textureNames"][0].removesuffix("_layer") if drawn else None)
    cutouts.append(drawn and material["renderMethod"] & 0xFF == 0x13)
  return meshPart(mesh["vertices"], mesh["triangles"], mesh["uvs"], textures, cutouts)


def wldActor(archive, definition):
  worldFile = eqWorldFile.WorldFile(archive.read(definition["wld"]), f"{definition['archive']}:{definition['wld']}")
  return worldFile, next(fragment for fragment in worldFile.fragmentsOfType(0x14) if fragment.name == definition["actor"])


def wldStaticParts(archive, definition, appearance):
  worldFile, actor = wldActor(archive, definition)
  meshes = [worldFile.fragment(struct.unpack_from("<i", reference.body, 4)[0], 0x36) for reference in actorReferences(worldFile, actor)]
  return {"parts": [wldMeshPart(worldFile.mesh(meshFragment), {}) for meshFragment in meshes], "pose": "static"}


def wldTextureSetSwaps(worldFile, code, textureSet):
  """WLD character materials are <code><part><set><index>_MDF (eqgame.exe %s%02d01_MDF); a texture set swaps in the same part and index from that set where the file has it."""
  if textureSet == 0:
    return {}
  materialIndices = {fragment.name.upper(): fragment.index for fragment in worldFile.fragmentsOfType(0x30)}
  swaps = {}
  for name in materialIndices:
    match = re.fullmatch(rf"({code}[A-Z]{{2}})00(\d\d)_MDF", name)
    candidate = match and f"{match.group(1)}{textureSet:02d}{match.group(2)}_MDF"
    if candidate in materialIndices:
      swaps[name] = worldFile.material(materialIndices[candidate])
  return swaps


def wldSkeletalParts(archive, definition, appearance):
  """The skeleton's skins with the body and head swapped as the client does (EQGraphicsDX9.dll): <code><nn>_DMSPRITEDEF for a variation, <code>HE<nn>_DMSPRITEDEF for a head type, when the file has them."""
  worldFile, actor = wldActor(archive, definition)
  skeleton = worldFile.fragment(struct.unpack_from("<i", actorReferences(worldFile, actor)[0].body, 4)[0], 0x10)
  meshes = eqSkeletons.skinMeshes(worldFile, eqSkeletons.readSkeleton(worldFile, skeleton)[1])
  code = definition["model"].upper()
  meshesByName = {fragment.name.upper(): fragment for fragment in worldFile.fragmentsOfType(0x36)}
  replacements = {}
  if appearance["variation"] != 0:
    replacements[f"{code}_DMSPRITEDEF"] = f"{code}{appearance['variation']:02d}_DMSPRITEDEF"
  replacements[f"{code}HE00_DMSPRITEDEF"] = f"{code}HE{appearance['headType']:02d}_DMSPRITEDEF"
  chosen = [meshesByName.get(replacements.get(mesh.name.upper()), mesh) for mesh in meshes]
  swaps = wldTextureSetSwaps(worldFile, code, appearance["textureSet"])
  parts, pose, particleClouds = eqSkeletons.posedSkeleton(worldFile, skeleton, chosen, lambda mesh: wldMeshPart(mesh, swaps))
  return {"parts": parts, "pose": pose, "pieces": [mesh.name for mesh in chosen], "swappedMaterials": len(swaps), "particleCloudsNotDrawn": particleClouds}


partBuilders = {"eqgStatic": eqgStaticParts, "eqgSkinned": eqgSkinnedParts, "wldStatic": wldStaticParts, "wldSkeletal": wldSkeletalParts}


def archiveStamp(clientRoot, archiveNames):
  return {name: [(clientRoot / name).stat().st_size, (clientRoot / name).stat().st_mtime_ns] for name in archiveNames}


def buildModel(clientRoot, cacheRoot, modelName, zoneName, source=None, appearance=None):
  """Resolve a model through the zone's links, then write its geometry (model.npz) and textures into a cache folder, reusing one built from the same archives and appearance. Appearance (variation, headType, textureSet) applies to skinned and skeletal models only."""
  definition = resolveModel(clientRoot, cacheRoot, modelName, zoneName, source)
  if definition["kind"] in staticKinds:
    if any((appearance or {}).values()):
      raise ValueError(f"Model '{definition['model']}' is static; variation, headType, and textureSet do not apply, got {appearance}")
    appearance = {}
  else:
    appearance = {"variation": 0, "headType": 0, "textureSet": 0} | (appearance or {})
    negative = {key: value for key, value in appearance.items() if value < 0}
    if negative:
      raise ValueError(f"Appearance values must be 0 or more, got {negative}")
  searched = textureArchives(clientRoot, zoneName, definition)
  folderName = f"{definition['model']}@{definition['archive']}" + "".join(f"@{key}{value}" for key, value in sorted(appearance.items()))
  modelFolder = cacheRoot / "built" / folderName
  stampPath = modelFolder / "source.json"
  stamp = {"cacheFormat": modelCacheFormat, "definition": definition, "appearance": appearance, "archives": archiveStamp(clientRoot, searched), "armsDownDegrees": eqSkeletons.armsDownDegrees}
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored.get(key) for key in stamp} == json.loads(json.dumps(stamp)):
      return modelFolder, stored
  archive = eqArchive.EQArchive(clientRoot / definition["archive"])
  built = partBuilders[definition["kind"]](archive, definition, appearance)
  parts = built["parts"]
  vertexChunks, triangleChunks, uvChunks, textures, cutouts = [], [], [], [], []
  offset = 0
  for part in parts:
    vertexChunks.append(part["vertices"])
    triangleChunks.append(part["triangles"] + offset)
    uvChunks.append(part["uvs"])
    textures += part["textures"]
    cutouts += part["cutouts"]
    offset += len(part["vertices"])
  triangles = numpy.concatenate(triangleChunks)
  if len(triangles) == 0:
    raise ValueError(f"Model '{definition['model']}' from {definition['archive']} has no drawn triangles")
  # Only drawn geometry is kept, so the cached mesh measures what the client shows.
  used = numpy.unique(triangles)
  vertices, uvs, triangles = numpy.concatenate(vertexChunks)[used], numpy.concatenate(uvChunks)[used], numpy.searchsorted(used, triangles)
  textureHolders = [eqArchive.EQArchive(clientRoot / name) if name != definition["archive"] else archive for name in searched]
  textureSources = {}
  # A texture absent from every linked archive is absent for the client too; its faces are drawn as missing and reported.
  missingTextures = []
  for textureName in sorted(set(textures)):
    holder = next((candidate for candidate in textureHolders if textureName in candidate.entries), None)
    if holder is None:
      missingTextures.append(textureName)
    else:
      textureSources[textureName] = holder.archivePath.name.lower()
  modelFolder.mkdir(parents=True, exist_ok=True)
  for textureName, holderName in textureSources.items():
    holder = next(candidate for candidate in textureHolders if candidate.archivePath.name.lower() == holderName)
    (modelFolder / textureName).write_bytes(eqTextures.readableTexture(textureName, holder.read(textureName)))
  numpy.savez(modelFolder / "model.npz", vertices=vertices, triangles=triangles, uvs=uvs, textureNames=numpy.array(textures), cutouts=numpy.array(cutouts), missingTextures=numpy.array(missingTextures, dtype=str))
  details = stamp | {
    "model": definition["model"],
    "pose": built["pose"],
    "pieces": built.get("pieces"),
    "swappedMaterials": built.get("swappedMaterials"),
    "particleCloudsNotDrawn": built.get("particleCloudsNotDrawn", 0),
    "textureSources": textureSources,
    "missingTextures": missingTextures,
    "searchedArchives": searched,
    "droppedTriangles": sum(part["dropped"] for part in parts),
    "minimum": [round(float(value), 3) for value in vertices.min(0)],
    "maximum": [round(float(value), 3) for value in vertices.max(0)],
  }
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return modelFolder, details
