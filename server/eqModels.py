"""EverQuest models found through the client's own links (eqLinks) and built into per-model caches: EQG models (.mod, static or skinned), EQG skinned piece models (.mds), and WLD static and skeletal actors."""
import concurrent.futures
import hashlib
import json
import multiprocessing
import re
import struct

import numpy

import eqAnimations
import eqArchive
import eqgFiles
import eqgSkeletons
import eqLinks
import eqLooks
import eqRaces
import eqSkeletons
import eqTextures
import eqWorldFile
import machineProfile
import zoneSources

indexFormat = 11
modelCacheFormat = 19
actorTrailingBytes = 4
staticKinds = ("wldStatic",)
defaultAppearance = {
  "variation": 0, "headType": 0, "textureSet": 0, "hairStyle": 0, "faceStyle": 0, "hairColor": 0, "facialHair": eqLooks.noStyle, "facialHairColor": 0,
  "eyeColor1": 0, "heritage": 0, "tattoo": 0, "details": 0,
}
eqgPlayerOnly = ("faceStyle", "hairColor", "facialHair", "facialHairColor", "eyeColor1", "heritage", "tattoo", "details")
bindTolerance = 1e-3
# A placed WLD object's mesh without vertex colors: no baked light and the full share of scene light. Not settled: the client's object
# builder fills 0xFFFFFFFF (EQGraphicsDX9.dll 0x10057060), which SModelC1 would draw at full texture brightness (docs/clientRendering.md).
colorlessMeshColor = (0, 0, 0, 255)
# No baked light and the full share of scene light: how SkinMeshOld lights a skin, which has no vertex colors.
sceneLitColor = (0, 0, 0, 255)


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


def wldAnimations(worldFile):
  """The animations a WLD registers, by resource name (<anim><code>) with their root track and track count; the client leaves
  attachment point tracks out of the count."""
  trackNames = sorted({fragment.name for fragment in worldFile.fragmentsOfType(0x13)})
  animations = {}
  for name in trackNames:
    match = next((match for pattern in eqAnimations.rootTrackPatterns for match in [pattern.match(name)] if match), None)
    if match is None:
      continue
    resource = match.group(1) + match.group(2)
    if resource in animations:
      raise ValueError(f"{worldFile.sourceName}: animation {resource} has root tracks {animations[resource]['root']} and {name}")
    animations[resource] = {"root": name, "tracks": sum(1 for other in trackNames if other.startswith(resource) and "POINT" not in other)}
  return animations


def eqgKind(entryName):
  """A .mod is an eqgModel, static or skinned by whether it stores bones; a .mds is an eqgSkinned piece model."""
  return "eqgModel" if entryName.endswith(".mod") else "eqgSkinned"


def archiveModels(archivePath):
  """Every model an archive defines, by model key (a key defined twice in one archive keeps both definitions); the animations it
  registers: its WLDs' by resource name, and its EQG .ani entries by resource name (the entry name without .ani); and the eye
  materials (CHR_EYE<n>_MDF) its WLDs define, which the client looks up by name (EQGraphicsDX9.dll 0x10040d9d)."""
  archive = eqArchive.EQArchive(archivePath)
  models, animations, eyeMaterials = {}, {}, {}
  for entryName in archive.names():
    if entryName.endswith((".mod", ".mds")):
      models.setdefault(eqLinks.modelKey(entryName[:-4]), []).append({"kind": eqgKind(entryName), "entry": entryName})
    elif entryName.endswith(".ani"):
      animations[entryName[:-4].upper()] = {"entry": entryName}
  for wldName in [name for name in archive.names() if name.endswith(".wld")]:
    worldFile = eqWorldFile.WorldFile(archive.read(wldName), f"{archivePath.name}:{wldName}")
    for fragment in worldFile.fragmentsOfType(0x14):
      models.setdefault(eqLinks.modelKey(fragment.name), []).append({"kind": actorKind(worldFile, fragment), "wld": wldName, "actor": fragment.name})
    for resource, animation in wldAnimations(worldFile).items():
      if resource in animations:
        raise ValueError(f"{archivePath.name}: animation {resource} is in two of its WLDs")
      animations[resource] = animation | {"wld": wldName}
    for fragment in worldFile.fragmentsOfType(0x30):
      if fragment.name.startswith("CHR_EYE"):
        eyeMaterials.setdefault(fragment.name, {"wld": wldName, "fragment": fragment.index})
  return models, animations, eyeMaterials


def indexedArchive(archivePath):
  """Runs in a worker process. An archive this parser cannot read is recorded with its error, so resolving through it fails loudly."""
  try:
    models, animations, eyeMaterials = archiveModels(archivePath)
    return {"models": models, "animations": animations, "eyeMaterials": eyeMaterials}
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


def loadOrder(clientRoot, zoneName):
  """Every archive the client has loaded in the zone, in the order it loads them: its startup archives, then the zone's (none when
  no zone is given). Each archive appears once, at its first load."""
  globals_ = eqLinks.globalLinks(clientRoot)
  zone = eqLinks.zoneLinks(clientRoot, zoneName) if zoneName is not None else {"archives": [], "missing": []}
  order = []
  for tier, links in (("global", globals_["archives"]), ("zone", zone["archives"])):
    for link in links:
      if not any(earlier["archive"] == link["archive"] and earlier["codes"] is None for earlier in order):
        order.append(link | {"tier": tier})
  return order, globals_["missing"] + zone["missing"]


def indexEntry(index, link):
  entry = index.get(link["archive"])
  if entry is None:
    raise ValueError(f"{link['archive']} (linked by {link['via']}) is not in the model index")
  if "error" in entry:
    raise ValueError(f"{link['archive']} (linked by {link['via']}) could not be read: {entry['error']}")
  return entry


def definitionsIn(index, link, key):
  return [definition | {"archive": link["archive"], "via": link["via"]} for definition in indexEntry(index, link)["models"].get(key, [])]


def findModel(clientRoot, cacheRoot, modelName, zoneName):
  """Every definition of a model the client could load in a zone, in its load order, plus on-demand entries and unlinked archives that also define it."""
  key = eqLinks.modelKey(modelName)
  index = loadIndex(clientRoot, cacheRoot)
  order, missingLinks = loadOrder(clientRoot, zoneName)
  linked = []
  # A character list can link one archive several times, each for one code; only links covering this model count.
  covering = [link for link in order if link["codes"] is None or key in link["codes"]]
  for link in covering:
    if not any(definition["archive"] == link["archive"] for definition in linked):
      linked += [definition | {"tier": link["tier"]} for definition in definitionsIn(index, link, key)]
  onDemand = [entry | {"kind": eqgKind(entry["entry"]), "tier": "onDemand"} for entry in eqLinks.onDemandResources(clientRoot)["models"].get(key, [])]
  coveredArchives = {link["archive"] for link in covering} | {entry["archive"] for entry in onDemand}
  unlinked = sorted(name for name, entry in index.items() if name not in coveredArchives and key in entry.get("models", {}))
  return {"model": key, "linked": linked, "onDemand": onDemand, "unlinkedArchives": unlinked, "missingLinks": missingLinks}


def definitionSource(definition):
  return f"{definition['archive']}:{(definition.get('entry') or definition['actor']).lower()}"


def resolveModel(clientRoot, cacheRoot, modelName, zoneName, source=None):
  """The definition the client uses: the first it loads (startup archives, then the zone's, then on-demand entries, which load
  when first needed; of two on-demand lines naming one model the first registers, EQGraphicsDX9.dll 0x100c74f0). source ("archive"
  or "archive:entry") picks one definition instead. One archive defining the model twice is ambiguous."""
  found = findModel(clientRoot, cacheRoot, modelName, zoneName)
  candidates = found["linked"] + found["onDemand"]
  if source is not None:
    candidates = [definition for definition in candidates if source.lower() in (definition["archive"], definitionSource(definition))]
  if not candidates:
    unlinkedNote = f"; unlinked archives define it: {found['unlinkedArchives']}" if found["unlinkedArchives"] else ""
    linker = f"zone '{zoneName}'" if zoneName is not None else "the startup lists (no zone given)"
    raise ValueError(f"No archive {linker} loads defines model '{found['model']}'{' as ' + source if source else ''}{unlinkedNote}")
  first = candidates[0]
  rivals = [definition for definition in candidates if first["tier"] != "onDemand" and definition["tier"] == first["tier"] and definition["archive"] == first["archive"]]
  if len(rivals) > 1:
    raise ValueError(f"Model '{found['model']}' is defined {len(rivals)} times in {first['archive']}: {[definitionSource(definition) for definition in rivals]}; pass one as source to choose")
  definition = first | {"model": found["model"]}
  if definition["kind"].startswith("wldUnsupported"):
    raise ValueError(f"Model '{found['model']}' in {definition['archive']} is an actor of unsupported fragment types ({definition['kind'].split(':')[1]})")
  return definition


def animationHolder(index, order, resource):
  """The archive whose definition of an animation the client keeps: the first it loads."""
  for link in order:
    if link["codes"] is not None and resource[-3:].lower() not in link["codes"]:
      continue
    if resource in indexEntry(index, link)["animations"]:
      return link["archive"]
  return None


def findAnimation(index, links, code, isLuclin, request):
  """The animation the client gives a model (eqgame.exe 0x407800): a lettered variant for a Luclin model, else the unlettered
  animation, each under the model's own code before the code it borrows from. Returns the choice, or None with the reason."""
  name = eqAnimations.animationName(request["animation"] or eqAnimations.standAnimation)
  lettered, unlettered = eqAnimations.candidateResources(name, code, isLuclin)
  variants = {}
  for letter, resources in lettered:
    for resource in resources:
      holder = animationHolder(index, links, resource)
      if holder:
        variants[letter] = (resource, holder)
        break
  if variants:
    letter = request["variant"] or next(letter for letter in eqAnimations.variantLetters if letter in variants)
    if letter not in variants:
      raise ValueError(f"Animation {name} of {code} has variants {sorted(variants)}, not {letter}")
    resource, holder = variants[letter]
  else:
    if request["variant"] is not None:
      raise ValueError(f"Animation {name} of {code} has no lettered variants")
    letter = None
    resource, holder = next(((resource, holder) for resource in unlettered for holder in [animationHolder(index, links, resource)] if holder), (None, None))
    if resource is None:
      return None, f"no linked archive has animation {name} for {code} or the code it borrows from ({unlettered[-1][len(name):]})"
    trackCount = index[holder]["animations"][resource]["tracks"]
    if isLuclin and trackCount < eqAnimations.fewestUnletteredTracks and not code.startswith("GP") and code != "KES":
      return None, f"{resource} has {trackCount} tracks, fewer than the {eqAnimations.fewestUnletteredTracks} the client requires for a Luclin model"
  return {
    "animation": name, "label": eqAnimations.animationLabels.get(name), "variant": letter, "variants": sorted(variants),
    "resource": resource, "archive": holder, "wld": index[holder]["animations"][resource]["wld"], "root": index[holder]["animations"][resource]["root"],
    "borrowedFrom": resource[-3:] if resource[-3:] != code else None,
  }, None


def findEQGAnimation(clientRoot, index, links, code, request):
  """The EQG animation the client plays on an EQG model (EQGraphicsDX9.dll PlayAnimation, "%s_BA_1_%s"): <name>_BA_1_<code> from the
  first linked archive that defines it, else from the first on-demand line naming it (0x100c74f0 skips later ones). Returns the
  choice, or None with the reason."""
  name = eqAnimations.eqgAnimationName(request["animation"])
  resource = f"{name}_{eqAnimations.eqgAnimationPart}_{code}"
  holder = animationHolder(index, links, resource)
  if holder is not None:
    entry, via = index[holder]["animations"][resource]["entry"], "linked"
  else:
    onDemand = eqLinks.onDemandResources(clientRoot)["animations"].get(resource, [])
    if not onDemand:
      return None, f"no linked archive or on-demand entry has {resource}"
    holder, entry, via = onDemand[0]["archive"], onDemand[0]["entry"], "OnDemandResources.txt"
  return {"animation": name, "wldAnimation": eqAnimations.eqgAnimationCodes[name], "resource": resource, "archive": holder, "entry": entry, "linkedBy": via}, None


def animationTransforms(clientRoot, found, dags, code, frame, bindLocals):
  """Each bone's local transform at one frame of an animation: the track <resource><bone suffix>_TRACK (the root bone takes the
  animation's root track), or the bone's bind transform when the animation has no track for it. An animation's moving tracks
  share one frame count; its single-frame tracks hold still. Of two tracks with one name, the first in the file plays: the client
  registers tracks in file order and d3dx9 RegisterAnimationSRTKeys refuses a name already registered."""
  archive = eqArchive.EQArchive(clientRoot / found["archive"])
  worldFile = eqWorldFile.WorldFile(archive.read(found["wld"]), f"{found['archive']}:{found['wld']}")
  instances = {}
  for fragment in worldFile.fragmentsOfType(0x13):
    instances.setdefault(fragment.name, []).append(fragment)
  resource = found["resource"]
  boneTracks = {}
  for index, dag in enumerate(dags):
    if not (dag["name"].startswith(code) and dag["name"].endswith("_DAG")):
      raise ValueError(f"{found['archive']}: bone '{dag['name']}' of {code} is not named {code}<bone>_DAG")
    suffix = dag["name"][len(code):-len("_DAG")]
    trackName = f"{resource}{suffix}_TRACK" if suffix else found["root"]
    sameName = instances.get(trackName, [])
    if sameName:
      definition, millisecondsPerFrame = eqSkeletons.trackInstance(worldFile, sameName[0].index)
      boneTracks[index] = (trackName, eqSkeletons.trackFrames(worldFile, definition), millisecondsPerFrame)
  frameCount = max(len(frames) for _, frames, _ in boneTracks.values())
  irregular = sorted({len(frames) for _, frames, _ in boneTracks.values()} - {1, frameCount})
  if irregular:
    raise ValueError(f"Animation {resource} has tracks of {irregular} frames besides {frameCount}")
  if not 0 <= frame < frameCount:
    raise ValueError(f"Animation {resource} has frames 0-{frameCount - 1}, not {frame}")
  delays = sorted({delay for _, frames, delay in boneTracks.values() if len(frames) == frameCount and delay is not None})
  if len(delays) > 1:
    raise ValueError(f"Animation {resource} tracks give different frame delays {delays}")
  localTransforms = list(bindLocals)
  for index, (_, frames, _) in boneTracks.items():
    localTransforms[index] = eqSkeletons.frameTransform(frames[frame if len(frames) == frameCount else 0])
  return localTransforms, {"frame": frame, "frameCount": frameCount, "millisecondsPerFrame": delays[0] if delays else None, "bonesAnimated": len(boneTracks), "bones": len(dags)}


def textureArchives(clientRoot, zoneName, definition):
  """Archives a model's textures may come from: its own archive; for a zone's EQG model, also the zone's other EQG archives, which the client loads with it."""
  names = [definition["archive"]]
  if definition["tier"] == "zone" and definition["kind"].startswith("eqg"):
    names += [link["archive"] for link in eqLinks.zoneLinks(clientRoot, zoneName)["archives"] if link["archive"].endswith(".eqg") and link["archive"] not in names]
  return names


def triangleKeep(vertices, triangles):
  """Triangles whose vertices are all finite; a few client models carry NaN vertices that cannot render."""
  return numpy.isfinite(vertices).all(axis=1)[triangles].all(axis=1)


def eqgAlphaMode(shader):
  """How the client blends an EQG material, by the first family its shader's name contains (EQGraphicsDX9.dll 0x100147f0): AddAlpha
  (additive; drawn opaque here, as additive blending is not drawn yet), Alpha (blended by the texture's alpha), Chroma (cut out by
  it), else opaque."""
  if "AddAlpha" in shader:
    return "opaque"
  if "Alpha" in shader:
    return "blended"
  return "cutout" if "Chroma" in shader else "opaque"


def eqgMaterialTextures(materials, triangleMaterials, diffuseSwaps):
  """Per triangle: its diffuse texture (a layer's swap first), or None for triangles the client does not draw, and its alpha mode."""
  textures, alphaModes = [], []
  for materialIndex in triangleMaterials:
    diffuse = (diffuseSwaps.get(materialIndex) or materials[materialIndex]["properties"].get("e_TextureDiffuse0")) if materialIndex >= 0 else None
    textures.append(diffuse.lower() if diffuse else None)
    alphaModes.append(eqgAlphaMode(materials[materialIndex]["shader"]) if diffuse else "opaque")
  return textures, alphaModes


liquidShaders = {"opaque_maxwater.fx": "water", "opaque_maxwaterfall.fx": "waterfall", "opaque_maxlava.fx": "lava"}
slideProperties = ("e_fSlide1X", "e_fSlide1Y", "e_fSlide2X", "e_fSlide2Y")


def liquidColor(value):
  return [((value >> shift) & 0xFF) / 255 for shift in (16, 8, 0)]


def eqgLiquid(material):
  """A material drawn with one of the client's liquid shaders as the preview draws liquids: its liquid, its shader values, and its
  textures beyond the diffuse; None for any other material. The client's own materials leave some of these out (about one water material
  in thirty has no environment map); what a material leaves out is left out here, and the preview draws without it."""
  liquid = liquidShaders.get(material["shader"].lower())
  if liquid is None:
    return None
  properties = material["properties"]
  values = {"slides": [properties[name] for name in slideProperties]} if all(name in properties for name in slideProperties) else {}
  textureKeys = {"water": {"normal": "e_TextureNormal0", "environment": "e_TextureEnvironment0"}, "waterfall": {}, "lava": {"normal": "e_TextureNormal0", "secondDiffuse": "e_TextureDiffuse1"}}[liquid]
  if liquid == "water":
    values |= {key: properties[name] for key, name in (("fresnelBias", "e_fFresnelBias"), ("fresnelPower", "e_fFresnelPower"), ("reflectionAmount", "e_fReflectionAmount")) if name in properties}
    values |= {key: liquidColor(properties[name]) for key, name in (("reflectionColor", "e_fReflectionColor"), ("waterColor1", "e_fWaterColor1"), ("waterColor2", "e_fWaterColor2")) if name in properties}
  return {"liquid": liquid, "values": values, "textures": {key: properties[name].lower() for key, name in textureKeys.items() if properties.get(name)}}


def eqgLiquids(materials, triangleMaterials):
  return [eqgLiquid(materials[index]) if index >= 0 else None for index in triangleMaterials]


def staticEQGUVs(uvs):
  """A static (boneless) EQG model's texture coordinates as Blender counts them, v up from a texture's bottom: measured against
  screenshots, the Neighborhood's map board and guild gate show upright only with v flipped, while skinned models (a Drakkin's face)
  show upright unflipped."""
  return uvs * (1, -1) + (0, 1)


def meshPart(vertices, triangles, uvs, textures, alphaModes, lighting=None, liquids=None, passable=None):
  """Drawn triangles only; triangles with non-finite vertices are dropped and counted. lighting is the file's per-vertex normals and
  RGBA colors as the client lights them ({normals, colors}), or None for a mesh lit without them; liquids, each triangle's liquid
  (eqgLiquid) or None; passable, whether the file lets players through each triangle, or None for a model that does not say."""
  finite = triangleKeep(vertices, triangles)
  keep = numpy.array([texture is not None for texture in textures], dtype=bool) & finite
  keptTextures = [texture for texture, kept in zip(textures, keep) if kept]
  return {
    "vertices": vertices, "triangles": triangles[keep], "uvs": uvs, "textures": keptTextures, "alphaModes": [mode for mode, kept in zip(alphaModes, keep) if kept],
    "tints": [eqLooks.untinted] * len(keptTextures), "dropped": int((~finite).sum()), "lighting": lighting,
    "liquids": [None] * len(keptTextures) if liquids is None else [liquid for liquid, kept in zip(liquids, keep) if kept],
    "passable": None if passable is None else numpy.asarray(passable, dtype=bool)[keep],
  }


def changedAppearance(appearance):
  return {key: value for key, value in appearance.items() if value != defaultAppearance[key]}


def eqgSkeletonPose(bones, animation, sourceName):
  """A skeleton's bone matrices by bone name, bound and at the animation's frame."""
  parents, order = eqgSkeletons.boneParents(bones, sourceName)
  bind = eqgSkeletons.worldMatrices(eqgSkeletons.bindLocals(bones), parents, order)
  posed = eqgSkeletons.worldMatrices(eqgSkeletons.animatedLocals(bones, animation["tracks"], animation["frame"], animation["rootDrop"]), parents, order)
  return {"bind": dict(zip(bones["names"], bind)), "posed": dict(zip(bones["names"], posed))}


def animationRootDrop(resource, rootOffsets):
  """How far EQGraphicsDX9.dll lowers ROOT_BONE's keys as it loads an animation (0x1003c960): by the moddat.ini ROffset of the section
  named by the resource name's last three characters, or 3.125 without one, except in an animation named _MT_ or whose first _IT is
  followed by a digit."""
  name = resource.upper()
  marker = name.find("_IT")
  if "_MT_" in name or (marker >= 0 and name[marker + 3:marker + 4].isdigit()) or len(name) <= 2:
    return 0.0
  return rootOffsets.get(name[-3:], eqRaces.defaultAvatarOffset)


def placedSkinnedPose(model, tracks, resource, rootOffsets, sourceName):
  """A skinned .mod a zone places, as the client poses it: a CHierarchicalActor that loops its <model>_DEFAULT animation from a random
  point (rand() / 32767 of its length, EQGraphicsDX9.dll 0x10044550), drawn here at the animation's first key; the bind pose when the
  zone registers no such animation (tracks None). Returns the posed vertices and normals."""
  if tracks is None:
    return model["vertices"], model["normals"]
  bones = model["bones"]
  parents, order = eqgSkeletons.boneParents(bones, sourceName)
  bindWorlds = eqgSkeletons.worldMatrices(eqgSkeletons.bindLocals(bones), parents, order)
  poseWorlds = eqgSkeletons.worldMatrices(eqgSkeletons.animatedLocals(bones, tracks, 0, animationRootDrop(resource, rootOffsets)), parents, order)
  matrices = eqgSkeletons.skinMatrices(bindWorlds, poseWorlds)
  vertices, unweighted = eqgSkeletons.skinVertices(model["vertices"], model["weights"], matrices, sourceName)
  if unweighted[model["triangles"]].any():
    raise ValueError(f"{sourceName}: triangles use vertices with no bone weights; how the client poses them is not known")
  return vertices, eqgSkeletons.skinNormals(model["normals"], model["weights"], matrices, sourceName)


def eqgSkinnedPart(mesh, bones, pose, textures, alphaModes, sourceName, pointBone=None):
  """A skinned mesh moved from its own bind pose to the skeleton's pose, or as stored when the skeleton keeps its bind pose; drawn
  triangles must use weighted vertices. A piece attached at a point bone shares that bone's matrix for its first bone and the
  skeleton's bone of the same name for each other bone (EQGraphicsDX9.dll 0x10043e10)."""
  if pose is None:
    return meshPart(mesh["vertices"], mesh["triangles"], mesh["uvs"], textures, alphaModes)
  if mesh["weights"] is None:
    raise ValueError(f"{sourceName}: stores no weight records, so how the client poses it is not known")
  targets = list(bones["names"]) if pointBone is None else [pointBone] + list(bones["names"][1:])
  missing = [name for name in targets if name not in pose["posed"]]
  if missing:
    raise ValueError(f"{sourceName}: bones {missing} are not in the skeleton it is attached to")
  parents, order = eqgSkeletons.boneParents(bones, sourceName)
  bindWorlds = eqgSkeletons.worldMatrices(eqgSkeletons.bindLocals(bones), parents, order)
  weighted = numpy.unique(mesh["weights"]["influences"]["bone"][numpy.arange(4)[None, :] < mesh["weights"]["count"][:, None]])
  unaligned = [bones["names"][bone] for bone in weighted if numpy.abs(bindWorlds[bone] - pose["bind"][targets[bone]]).max() > bindTolerance]
  if unaligned:
    raise ValueError(f"{sourceName}: weighted bones {unaligned} bind elsewhere than the skeleton bones they follow, so how the client poses it is not known")
  poseWorlds = numpy.stack([pose["posed"][name] for name in targets])
  posed, unweighted = eqgSkeletons.skinVertices(mesh["vertices"], mesh["weights"], eqgSkeletons.skinMatrices(bindWorlds, poseWorlds), sourceName)
  part = meshPart(posed, mesh["triangles"], mesh["uvs"], textures, alphaModes)
  if unweighted[part["triangles"]].any():
    raise ValueError(f"{sourceName}: drawn triangles use vertices with no bone weights; how the client poses them is not known")
  return part


def eqgLayers(archive):
  """Every layer the archive's .lay files define, by name."""
  layers = {}
  for entry in sorted(name for name in archive.entries if name.endswith(".lay")):
    for name, textures in eqgFiles.parseLayers(archive.read(entry), f"{archive.archivePath.name}:{entry}").items():
      if layers.setdefault(name, textures) != textures:
        raise ValueError(f"{archive.archivePath.name}: two .lay files define layer {name} differently")
  return layers


def layerDiffuse(textures):
  """The diffuse a layer swaps in, or None: EQGraphicsDX9.dll types a layer's textures by the uppercase letter before .DDS (C
  diffuse, N normal, E environment; 0x100169cd) and sets each in turn, so the last C wins (0x10048180)."""
  diffuses = [texture for texture in textures if texture[-5:-4] == "C"]
  return diffuses[-1].lower() if diffuses else None


def modPalette(triangleMaterials):
  """A .mod's material palette as EQGraphicsDX9.dll builds it (0x100620f0): the materials its triangles use, in file order."""
  return sorted({int(index) for index in triangleMaterials if index >= 0})


def layerSwaps(layers, palette, applied, sourceName):
  """Diffuse swaps by material index for layers applied as (palette entry, layer name)."""
  swaps = {}
  for entry, name in applied:
    if name not in layers:
      raise ValueError(f"{sourceName}: no .lay in its archive defines layer {name}")
    if entry >= len(palette):
      raise ValueError(f"{sourceName}: layer {name} goes on palette entry {entry}, but its palette has {len(palette)}")
    diffuse = layerDiffuse(layers[name])
    if diffuse is not None:
      swaps[palette[entry]] = diffuse
  return swaps


def eqgPieces(context, skeletonBones, pose, requests):
  """The look pieces the client attaches by name (EQGraphicsDX9.dll 0x10042c60), requested as (name, point bone, tint, layer for
  palette entry 0 or None). One that no archive the client loads defines, or whose point bone the skeleton lacks, is not attached
  (nor drawn by the client), and is listed with the reason."""
  attached, unattached = [], []
  for name, pointBone, color, layerName in requests:
    definition = context.attachment(name) if pointBone in skeletonBones else None
    if definition is None:
      unattached.append({"piece": name, "reason": f"the skeleton has no {pointBone}" if pointBone not in skeletonBones else "no archive the client loads defines it"})
      continue
    if definition["kind"] != "eqgModel":
      raise ValueError(f"{name} in {definition['archive']} is a {definition['kind']} model, not an EQG model")
    sourceName = f"{definition['archive']}:{definition['entry']}"
    pieceArchive = eqArchive.EQArchive(context.clientRoot / definition["archive"])
    piece = eqgFiles.parseModel(pieceArchive.read(definition["entry"]), sourceName)
    if piece["bones"] is None:
      raise ValueError(f"{sourceName}: a piece without bones; how the client places it is not known")
    swaps = {} if layerName is None else layerSwaps(eqgLayers(pieceArchive), modPalette(piece["triangleMaterials"]), [(0, layerName)], sourceName)
    part = eqgSkinnedPart(piece, piece["bones"], pose, *eqgMaterialTextures(piece["materials"], piece["triangleMaterials"], swaps), sourceName, pointBone)
    attached.append({"part": part | {"tints": [color] * len(part["textures"])}, "piece": name, "archive": definition["archive"]})
  return attached, unattached


def eqgHairRequests(context, code, hairStyle):
  """The hair piece <code>_HAIR_<style> an EQG model other than a player's attaches (eqgame.exe 0x40ac80), when the client defines
  one; most define none."""
  name = f"{code}_HAIR_{hairStyle:02d}"
  return [] if context.attachment(name) is None else [(name, eqLooks.hairBone, eqLooks.untinted, None)]


def eqgModelParts(archive, definition, appearance, context):
  """A .mod model: static, or skinned to its bones and posed by the requested animation, with the looks and pieces the client gives
  it: an EQG player model's from PlayerCustomization.txt, another's hair piece."""
  sourceName = f"{definition['archive']}:{definition['entry']}"
  model = eqgFiles.parseModel(archive.read(definition["entry"]), sourceName)
  if model["bones"] is None:
    if context.request is not None and context.request["animation"] is not None:
      raise ValueError(f"Model '{definition['model']}' is static; it has no animations")
    if changedAppearance(appearance):
      raise ValueError(f"Model '{definition['model']}' is static; appearance does not apply, got {changedAppearance(appearance)}")
    return {"parts": [meshPart(model["vertices"], model["triangles"], staticEQGUVs(model["uvs"]), *eqgMaterialTextures(model["materials"], model["triangleMaterials"], {}))], "pose": {"static": True}}
  code = definition["model"].upper()
  if eqLooks.isEQGPlayerModel(code):
    unread = {key: value for key, value in changedAppearance(appearance).items() if key in ("variation", "headType", "textureSet")}
    if unread:
      raise ValueError(f"Model '{definition['model']}' is an EQG player model; variation, headType, and textureSet are not read for it yet, got {unread}")
    looks = eqLooks.eqgPlayerLooks(context.clientRoot, code, appearance)
    swaps = layerSwaps(eqgLayers(archive), modPalette(model["triangleMaterials"]), looks["layers"], sourceName)
    requests = looks["pieces"]
  else:
    unread = {key: value for key, value in changedAppearance(appearance).items() if key != "hairStyle"}
    if unread:
      raise ValueError(f"Model '{definition['model']}' is an EQG model built from attached pieces; only its hairStyle is read yet, got {unread}")
    swaps, requests = {}, eqgHairRequests(context, code, appearance["hairStyle"])
  animation, description = context.eqgAnimation(code)
  pose = None if animation is None else eqgSkeletonPose(model["bones"], animation, sourceName)
  parts = [eqgSkinnedPart(model, model["bones"], pose, *eqgMaterialTextures(model["materials"], model["triangleMaterials"], swaps), sourceName)]
  attached, unattached = eqgPieces(context, model["bones"]["names"], pose, requests)
  return {
    "parts": parts + [piece["part"] for piece in attached], "pose": description, "pieces": [definition["entry"]] + [piece["piece"] for piece in attached],
    "swappedMaterials": len(swaps), "attachedArchives": [piece["archive"] for piece in attached], "unattached": unattached,
  }


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


def eqgLayerDiffuses(archive, code, materialCount, textureSet):
  """Per material index, the diffuse its texture-set layer C_<code>_S<set>_M<index + 1> swaps in (EQGraphicsDX9.dll 0x100433d0; a
  .mds palette holds every material, 0x100631e0). Set 0 keeps the materials' textures."""
  if textureSet == 0:
    return {}
  layers = eqgLayers(archive)
  swaps = {}
  for index in range(materialCount):
    diffuse = layerDiffuse(layers.get(f"C_{code}_S{textureSet:02d}_M{index + 1:02d}", []))
    if diffuse is not None:
      swaps[index] = diffuse
  return swaps


def eqgSkinnedParts(archive, definition, appearance, context):
  """The pieces the client shows for this appearance, textured by its texture set, posed by the requested animation, with the hair piece the client attaches."""
  sourceName = f"{definition['archive']}:{definition['entry']}"
  unread = {key: value for key, value in changedAppearance(appearance).items() if key in eqgPlayerOnly}
  if unread:
    raise ValueError(f"Model '{definition['model']}' is an EQG skinned model; only variation, headType, textureSet, and hairStyle are read yet, got {unread}")
  model = eqgFiles.parseSkinnedModel(archive.read(definition["entry"]), sourceName)
  code = definition["model"].upper()
  pieces = {piece["name"].upper(): piece for piece in model["pieces"]}
  chosen = chosenPieces(list(pieces), code, appearance, sourceName)
  swaps = eqgLayerDiffuses(archive, code, len(model["materials"]), appearance["textureSet"])
  animation, description = context.eqgAnimation(code)
  pose = None if animation is None else eqgSkeletonPose(model["bones"], animation, sourceName)
  parts = [eqgSkinnedPart(pieces[name], model["bones"], pose, *eqgMaterialTextures(model["materials"], pieces[name]["triangleMaterials"], swaps), f"{sourceName} piece {name}") for name in chosen]
  attached, unattached = eqgPieces(context, model["bones"]["names"], pose, eqgHairRequests(context, code, appearance["hairStyle"]))
  return {
    "parts": parts + [piece["part"] for piece in attached], "pose": description, "pieces": chosen + [piece["piece"] for piece in attached],
    "swappedMaterials": len(swaps), "attachedArchives": [piece["archive"] for piece in attached], "unattached": unattached,
  }


def wldMeshPart(mesh, materialSwaps, colorless):
  """A WLD mesh's drawn triangles with the per-vertex values the client builds it with: a UV of (0, 0) and a zero normal where the file
  stores none (EQGraphicsDX9.dll 0x1001f630 regions, 0x10057060 objects, 0x1004ad50 skins). Lit by its normals and vertex colors,
  colorless (RGBA) standing for colors the file does not store; unlit when colorless is None (a posed character, whose normals and
  colors would no longer match it)."""
  materials = [materialSwaps.get(material["name"].upper(), material) for material in mesh["materials"]]
  textures, alphaModes = [], []
  for index in mesh["triangleMaterials"]:
    material = materials[index]
    drawn = material["renderMethod"] != eqWorldFile.invisibleRenderMethod and bool(material["textureNames"])
    textures.append(material["textureNames"][0].removesuffix("_layer") if drawn else None)
    alphaModes.append("cutout" if drawn and material["renderMethod"] & 0xFF == 0x13 else "opaque")
  vertexCount = len(mesh["vertices"])
  # The client's (0, 0), flipped as every WLD texture coordinate is.
  uvs = mesh["uvs"] if mesh["uvs"] is not None else numpy.tile((0.0, 1.0), (vertexCount, 1))
  lighting = None
  if colorless is not None:
    colors = mesh["colors"] if mesh["colors"] is not None else numpy.tile(numpy.array(colorless, dtype=numpy.uint8), (vertexCount, 1))
    lighting = {"normals": mesh["normals"] if mesh["normals"] is not None else numpy.zeros((vertexCount, 3)), "colors": colors}
  return meshPart(mesh["vertices"], mesh["triangles"], uvs, textures, alphaModes, lighting)


def wldActor(archive, definition):
  worldFile = eqWorldFile.WorldFile(archive.read(definition["wld"]), f"{definition['archive']}:{definition['wld']}")
  return worldFile, next(fragment for fragment in worldFile.fragmentsOfType(0x14) if fragment.name == definition["actor"])


def wldStaticParts(archive, definition, appearance, context):
  worldFile, actor = wldActor(archive, definition)
  meshes = [worldFile.fragment(struct.unpack_from("<i", reference.body, 4)[0], 0x36) for reference in actorReferences(worldFile, actor)]
  return {"parts": [wldMeshPart(worldFile.mesh(meshFragment), {}, colorlessMeshColor) for meshFragment in meshes], "pose": {"static": True}}


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


def wldSkeletalBindParts(archive, definition):
  """A skeletal WLD actor's meshes in the bind pose, lit by their normals: a placed zone object such as a torch. Its skins take no
  vertex colors: the client builds them without any (EQGraphicsDX9.dll 0x1004ae5f, vertex format 0x112) and lights them by scene light
  alone (SkinMeshOld); the meshes on its bones keep theirs."""
  worldFile, actor = wldActor(archive, definition)
  skeleton = worldFile.fragment(struct.unpack_from("<i", actorReferences(worldFile, actor)[0].body, 4)[0], 0x10)
  _, skins = eqSkeletons.readSkeleton(worldFile, skeleton)
  parts, particleClouds, _ = eqSkeletons.posedSkeleton(
    worldFile, skeleton, eqSkeletons.skinMeshes(worldFile, skins), lambda mesh: wldMeshPart(mesh | {"colors": None}, {}, sceneLitColor),
    lambda mesh: wldMeshPart(mesh, {}, colorlessMeshColor),
  )
  return parts, particleClouds


def wldSkeletalParts(archive, definition, appearance, context):
  """The skeleton's skins with the body and head swapped as the client does (EQGraphicsDX9.dll): <code><nn>_DMSPRITEDEF for a
  variation, <code>HE<nn>_DMSPRITEDEF for a head type, when the file has them; posed by the requested animation frame, or the bind pose."""
  worldFile, actor = wldActor(archive, definition)
  skeleton = worldFile.fragment(struct.unpack_from("<i", actorReferences(worldFile, actor)[0].body, 4)[0], 0x10)
  dags, skins = eqSkeletons.readSkeleton(worldFile, skeleton)
  meshes = eqSkeletons.skinMeshes(worldFile, skins)
  code = definition["model"].upper()
  localTransforms, pose = context.wldPose(worldFile, dags, code)
  meshesByName = {fragment.name.upper(): fragment for fragment in worldFile.fragmentsOfType(0x36)}
  replacements = {}
  if appearance["variation"] != 0:
    replacements[f"{code}_DMSPRITEDEF"] = f"{code}{appearance['variation']:02d}_DMSPRITEDEF"
  replacements[f"{code}HE00_DMSPRITEDEF"] = f"{code}HE{appearance['headType']:02d}_DMSPRITEDEF"
  chosen = [meshesByName.get(replacements.get(mesh.name.upper()), mesh) for mesh in meshes]
  swaps = wldTextureSetSwaps(worldFile, code, appearance["textureSet"])
  materialIndices = {fragment.name.upper(): fragment.index for fragment in worldFile.fragmentsOfType(0x30)}
  faceSwaps = eqLooks.faceSwaps(code, appearance["faceStyle"], set(materialIndices))
  clashing = sorted(set(faceSwaps) & set(swaps))
  if clashing:
    raise ValueError(f"Model {code}: texture set {appearance['textureSet']} and face {appearance['faceStyle']} both swap {clashing}; which the client keeps is not known")
  swaps |= {name: worldFile.material(materialIndices[candidate]) for name, candidate in faceSwaps.items()}
  eyes, eyeArchives = wldEyeSwaps(worldFile, dags, chosen, code, appearance, context)
  clashing = sorted(set(eyes) & set(swaps))
  if clashing:
    raise ValueError(f"Model {code}: the eye colors and the face or texture set both swap {clashing}; which the client keeps is not known")
  swaps |= eyes
  def unlit(mesh):
    return wldMeshPart(mesh, swaps, None)

  parts, particleClouds, boneWorlds = eqSkeletons.posedSkeleton(worldFile, skeleton, chosen, unlit, unlit, localTransforms)
  attached, unattached = wldHeadItems(context, code, appearance, boneWorlds)
  return {
    "parts": parts + [item["part"] for item in attached], "pose": pose, "pieces": [mesh.name for mesh in chosen] + [item["piece"] for item in attached],
    "swappedMaterials": len(swaps), "particleCloudsNotDrawn": particleClouds, "attachedArchives": [item["archive"] for item in attached] + eyeArchives, "unattached": unattached,
  }


def wldEyeSwaps(worldFile, dags, meshes, code, appearance, context):
  """A Luclin model's eye materials, which the client swaps for the CHR_EYE materials its eye color names (eqLooks.luclinEyes),
  with the archives they come from. A model without <code>TUNIC_POINT_DAG is not Luclin and keeps its eyes (eqgame.exe 0x40796b),
  as does an eye whose material no loaded archive defines."""
  if not any(dag["name"] == f"{code}TUNIC_POINT_DAG" for dag in dags):
    return {}, []
  paletteReferences = sorted({struct.unpack_from("<i", mesh.body, 8)[0] for mesh in meshes})
  if len(paletteReferences) != 1:
    raise ValueError(f"{worldFile.sourceName}: the meshes of {code} use material lists {paletteReferences}, not one palette")
  swaps, archives = {}, []
  for paletteName, eyeName in eqLooks.luclinEyes(code, appearance, [material["name"].upper() for material in worldFile.materialList(paletteReferences[0])]).items():
    found = context.eyeMaterial(eyeName)
    if found is not None:
      swaps[paletteName] = found["material"]
      archives.append(found["archive"])
  return swaps, archives


def wldHeadItems(context, code, appearance, boneWorlds):
  """The hair and beard items the client attaches at the skeleton's head points (eqgame.exe 0x40aa30): each item model in its bind
  pose, placed at the point and tinted by its color. An item the client finds no point or model for is not attached (nor drawn
  by the client), and is listed with the reason."""
  if not any(f"{code}{point}" in boneWorlds for point in (eqLooks.hairPoint, eqLooks.beardPoint)):
    return [], []
  attached, unattached = [], []
  for item, point, color in eqLooks.headItems(code, appearance):
    pointWorld = boneWorlds.get(f"{code}{point}")
    definition = context.attachment(item) if pointWorld is not None else None
    if definition is None:
      unattached.append({"piece": item, "reason": f"the skeleton has no {code}{point}" if pointWorld is None else "no archive the client loads defines it"})
      continue
    if definition["kind"] not in ("wldSkeletal", "wldStatic"):
      raise ValueError(f"{item} in {definition['archive']} is a {definition['kind']} model, not a WLD item")
    itemArchive = eqArchive.EQArchive(context.clientRoot / definition["archive"])
    itemAppearance = defaultAppearance | {"hairStyle": eqLooks.noStyle}
    built = partBuilders[definition["kind"]](itemArchive, definition | {"model": item.lower()}, itemAppearance, ModelContext(context.clientRoot, context.cacheRoot, context.zoneName, None))
    for part in built["parts"]:
      attached.append({
        "part": part | {"vertices": part["vertices"] @ pointWorld[:3, :3].T + pointWorld[:3, 3], "tints": [color] * len(part["textures"]), "lighting": None},
        "piece": item, "archive": definition["archive"],
      })
  return attached, unattached


partBuilders = {"eqgModel": eqgModelParts, "eqgSkinned": eqgSkinnedParts, "wldStatic": wldStaticParts, "wldSkeletal": wldSkeletalParts}


class ModelContext:
  """What building a model needs beyond its own archive: the client, the zone's links (for animations and attached pieces), and the
  requested pose ({animation, variant, frame}, animation None for the client's stand), or None for none."""

  def __init__(self, clientRoot, cacheRoot, zoneName, request):
    self.clientRoot = clientRoot
    self.cacheRoot = cacheRoot
    self.zoneName = zoneName
    self.index = loadIndex(clientRoot, cacheRoot)
    self.links = loadOrder(clientRoot, zoneName)[0]
    self.request = request

  def attachment(self, name):
    """The definition the client uses for a piece it attaches by name, or None when nothing it loads defines one."""
    found = findModel(self.clientRoot, self.cacheRoot, name, self.zoneName)
    if not found["linked"] and not found["onDemand"]:
      return None
    return resolveModel(self.clientRoot, self.cacheRoot, name, self.zoneName)

  def eyeMaterial(self, name):
    """An eye material the client looks up by name, from the first archive it loads that defines it (it registers a name once), or
    None when none does."""
    for link in self.links:
      found = indexEntry(self.index, link)["eyeMaterials"].get(name)
      if found is not None:
        sourceName = f"{link['archive']}:{found['wld']}"
        worldFile = eqWorldFile.WorldFile(eqArchive.EQArchive(self.clientRoot / link["archive"]).read(found["wld"]), sourceName)
        return {"material": worldFile.material(found["fragment"]), "archive": link["archive"]}
    return None

  def eqgAnimation(self, code):
    """The requested EQG animation frame ({tracks, frame, rootDrop}) and its description, or None (the bind pose) and the reason.
    The DLL lowers ROOT_BONE's keys of every animation but _MT_ ones by the model's moddat.ini ROffset (0x1003cd5b); the _BA_1_
    animations a spawn plays are all lowered."""
    if self.request is None:
      return None, {"bind": "no animation requested"}
    if self.request["variant"] is not None:
      raise ValueError(f"Model {code} is an EQG model; its animations have no lettered variants")
    found, reason = findEQGAnimation(self.clientRoot, self.index, self.links, code, self.request)
    if found is None:
      if self.request["animation"] is not None:
        raise ValueError(f"Model {code} has no animation {self.request['animation']}: {reason}")
      return None, {"bind": reason}
    sourceName = f"{found['archive']}:{found['entry']}"
    tracks = eqgFiles.parseAnimation(eqArchive.EQArchive(self.clientRoot / found["archive"]).read(found["entry"]), sourceName)
    frameCounts = sorted({len(frames) for frames in tracks.values()} - {1}) or [1]
    if len(frameCounts) > 1:
      raise ValueError(f"{sourceName}: tracks of {frameCounts} frames")
    frameCount, frame = frameCounts[0], self.request["frame"]
    if not 0 <= frame < frameCount:
      raise ValueError(f"Animation {found['resource']} has frames 0-{frameCount - 1}, not {frame}")
    timed = next(frames for frames in tracks.values() if len(frames) == frameCount)
    return {"tracks": tracks, "frame": frame, "rootDrop": eqRaces.avatarOffset(self.clientRoot, code)}, found | {
      "frame": frame, "frameCount": frameCount, "frameMilliseconds": int(timed["time"][frame]), "durationMilliseconds": int(timed["time"][-1]),
    }

  def wldPose(self, worldFile, dags, code):
    if self.request is None:
      return None, {"bind": "no animation requested"}
    if dags[0]["name"] != f"{code}_DAG":
      raise ValueError(f"{worldFile.sourceName}: the root bone of {code} is '{dags[0]['name']}', not {code}_DAG")
    isLuclin = any(dag["name"] == f"{code}TUNIC_POINT_DAG" for dag in dags)
    found, reason = findAnimation(self.index, self.links, code, isLuclin, self.request)
    if found is None:
      if self.request["animation"] is not None:
        raise ValueError(f"Model {code} has no animation {self.request['animation']}: {reason}")
      return None, {"bind": reason}
    localTransforms, timing = animationTransforms(self.clientRoot, found, dags, code, self.request["frame"], eqSkeletons.bindTransforms(worldFile, dags))
    return localTransforms, found | timing


def archiveStamp(clientRoot, archiveNames):
  return {name: [(clientRoot / name).stat().st_size, (clientRoot / name).stat().st_mtime_ns] for name in archiveNames}


def buildModel(clientRoot, cacheRoot, modelName, zoneName, source=None, appearance=None, animation=None):
  """Resolve a model through the zone's links, then write its geometry (model.npz) and textures into a cache folder, reusing one
  built from the same client files, appearance, and animation. Appearance (variation, headType, textureSet) applies to skinned and
  skeletal models only; animation ({animation, variant, frame}, animation None for the client's stand) poses a spawn's skeleton."""
  definition = resolveModel(clientRoot, cacheRoot, modelName, zoneName, source)
  if animation is not None and animation["frame"] < 0:
    raise ValueError(f"Animation frame must be 0 or more, got {animation['frame']}")
  if definition["kind"] in staticKinds:
    if animation is not None and animation["animation"] is not None:
      raise ValueError(f"Model '{definition['model']}' is static; it has no animations")
    if changedAppearance(defaultAppearance | (appearance or {})):
      raise ValueError(f"Model '{definition['model']}' is static; appearance does not apply, got {changedAppearance(defaultAppearance | (appearance or {}))}")
    appearance, animation = {}, None
  else:
    appearance = defaultAppearance | (appearance or {})
    negative = {key: value for key, value in appearance.items() if value < 0}
    if negative:
      raise ValueError(f"Appearance values must be 0 or more, got {negative}")
  searched = textureArchives(clientRoot, zoneName, definition)
  # The folder is named by a digest of the archive, look, and pose, which source.json spells out: spelled out, they push file paths past
  # Windows' 260 characters, and the material names that start with the folder name past Blender's 63.
  variantKey = json.dumps({"archive": definition["archive"], "appearance": changedAppearance(appearance), "animation": animation}, sort_keys=True)
  modelFolder = cacheRoot / "built" / f"{definition['model']}@{hashlib.sha256(variantKey.encode('utf-8')).hexdigest()[:12]}"
  stampPath = modelFolder / "source.json"
  # The listing fingerprint covers the archives an animation may come from.
  stamp = {
    "cacheFormat": modelCacheFormat, "indexFormat": indexFormat, "definition": definition, "appearance": appearance, "animation": animation,
    "archives": archiveStamp(clientRoot, searched), "listingFingerprint": zoneSources.clientListingFingerprint(clientRoot),
  }
  if stampPath.is_file():
    stored = json.loads(stampPath.read_text(encoding="utf-8"))
    if {key: stored.get(key) for key in stamp} == json.loads(json.dumps(stamp)):
      return modelFolder, stored
  archive = eqArchive.EQArchive(clientRoot / definition["archive"])
  built = partBuilders[definition["kind"]](archive, definition, appearance, ModelContext(clientRoot, cacheRoot, zoneName, animation))
  searched = searched + [name for name in built.get("attachedArchives", []) if name not in searched]
  written = writePartsCache(modelFolder, built["parts"], [eqArchive.EQArchive(clientRoot / name) if name != definition["archive"] else archive for name in searched], f"Model '{definition['model']}' from {definition['archive']}")
  details = stamp | {
    "model": definition["model"],
    "pose": built["pose"],
    "pieces": built.get("pieces"),
    "swappedMaterials": built.get("swappedMaterials"),
    "particleCloudsNotDrawn": built.get("particleCloudsNotDrawn", 0),
    "unattached": built.get("unattached", []),
    "searchedArchives": searched,
  } | written
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return modelFolder, details


def writePartsCache(modelFolder, parts, textureHolders, label):
  """Write parts as one mesh (model.npz) with its textures into a cache folder; returns where each texture came from, the missing
  ones, dropped triangles, and bounds. Lighting is written when every part carries it, and which triangles players pass through when
  any does."""
  vertexChunks, triangleChunks, uvChunks, textures, alphaModes, tints, liquids, passableChunks = [], [], [], [], [], [], [], []
  offset = 0
  for part in parts:
    vertexChunks.append(part["vertices"])
    triangleChunks.append(part["triangles"] + offset)
    passableChunks.append(part["passable"] if part.get("passable") is not None else numpy.zeros(len(part["triangles"]), dtype=bool))
    uvChunks.append(part["uvs"])
    textures += part["textures"]
    alphaModes += part["alphaModes"]
    tints += part["tints"]
    liquids += part.get("liquids") or [None] * len(part["textures"])
    offset += len(part["vertices"])
  triangles = numpy.concatenate(triangleChunks)
  if len(triangles) == 0:
    raise ValueError(f"{label} has no drawn triangles")
  litParts = sum(part["lighting"] is not None for part in parts)
  if litParts not in (0, len(parts)):
    raise ValueError(f"{label}: {litParts} of {len(parts)} parts carry lighting; how the client lights them together is not known")
  choosingParts = sum("takesAllLights" in part for part in parts)
  if choosingParts not in (0, len(parts)):
    raise ValueError(f"{label}: {choosingParts} of {len(parts)} parts say which point lights they take")
  # Only drawn geometry is kept, so the cached mesh measures what the client shows.
  used = numpy.unique(triangles)
  vertices, uvs, triangles = numpy.concatenate(vertexChunks)[used], numpy.concatenate(uvChunks)[used], numpy.searchsorted(used, triangles)
  lighting = {}
  if litParts:
    lighting = {key: numpy.concatenate([part["lighting"][key] for part in parts])[used] for key in ("normals", "colors")}
  # Terrain tiles carry a detail texture coordinate and a tint per vertex; only terrain materials read them, so other parts' vertices
  # hold zeros.
  lightChoice = {}
  if choosingParts:
    lightChoice = {"vertexTakesAllLights": numpy.concatenate([numpy.full(len(part["vertices"]), part["takesAllLights"]) for part in parts])[used]}
  terrainAttributes = {}
  if any("detailUVs" in part for part in parts):
    terrainAttributes = {
      key: numpy.concatenate([part[key] if key in part else numpy.zeros((len(part["vertices"]), width), dtype=dataType) for part in parts])[used]
      for key, width, dataType in (("detailUVs", 2, numpy.float64), ("vertexTints", 4, numpy.uint8))
    }
  textureSources = {}
  # A texture absent from every linked archive is absent for the client too; its faces are drawn as missing and reported.
  missingTextures = []
  liquidTextureNames = {name for liquid in liquids if liquid is not None for name in liquid["textures"].values()}
  for textureName in sorted(set(textures) | liquidTextureNames):
    if textureName.startswith("terrain:"):
      continue
    holder = next((candidate for candidate in textureHolders if textureName in candidate.entries), None)
    if holder is None:
      missingTextures.append(textureName)
    else:
      textureSources[textureName] = holder.archivePath.name.lower()
  modelFolder.mkdir(parents=True, exist_ok=True)
  fileNames = {}
  for textureName, holderName in textureSources.items():
    holder = next(candidate for candidate in textureHolders if candidate.archivePath.name.lower() == holderName)
    fileNames[textureName], readable = eqTextures.readableTexture(textureName, holder.read(textureName))
    (modelFolder / fileNames[textureName]).write_bytes(readable)
  # Triangles index a palette of materials (texture file, alpha mode, tint, liquid). A liquid names its textures by their cached files;
  # one whose texture is missing keeps the rest.
  def liquidKey(liquid):
    if liquid is None:
      return ""
    found = {key: fileNames[name] for key, name in liquid["textures"].items() if name in fileNames}
    return json.dumps(liquid | {"textures": found}, sort_keys=True)

  passable = numpy.concatenate(passableChunks)
  palette, triangleMaterials = {}, numpy.empty(len(textures), dtype=numpy.int32)
  for index, key in enumerate(zip(textures, alphaModes, tints, (liquidKey(liquid) for liquid in liquids))):
    triangleMaterials[index] = palette.setdefault(key, len(palette))
  numpy.savez(
    modelFolder / "model.npz", vertices=vertices.astype(numpy.float32), triangles=triangles.astype(numpy.int32), uvs=uvs.astype(numpy.float32),
    materialTextures=numpy.array([fileNames.get(texture, texture) for texture, _, _, _ in palette]), materialAlphaModes=numpy.array([alphaMode for _, alphaMode, _, _ in palette]),
    materialTints=numpy.array([tint for _, _, tint, _ in palette], dtype=numpy.uint32), materialLiquids=numpy.array([liquid for _, _, _, liquid in palette], dtype=str),
    triangleMaterials=triangleMaterials, missingTextures=numpy.array(missingTextures, dtype=str),
    **{key: value.astype(numpy.float32) if value.dtype == numpy.float64 else value for key, value in (lighting | terrainAttributes).items()},
    **lightChoice,
    **({"trianglePassable": passable} if passable.any() else {}),
  )
  return {
    "textureSources": textureSources, "missingTextures": missingTextures, "droppedTriangles": sum(part["dropped"] for part in parts), "lit": bool(litParts),
    "minimum": [float(value) for value in vertices.min(0)], "maximum": [float(value) for value in vertices.max(0)],
  }
