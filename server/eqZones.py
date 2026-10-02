"""EverQuest zones built from the client's files into a cache the bridge places as one mesh: a classic (WLD) zone's region meshes and
the objects its objects.wld places, with the normals and vertex colors the client lights them by."""
import json
import math
import struct

import numpy

import eqArchive
import eqModels
import eqWorldFile
import zoneSources

zoneCacheFormat = 2
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
  variants = zoneSources.zoneVariants(clientRoot, zoneName)
  classic = variants.get(f"{zoneName}:wld")
  if classic is None:
    raise ValueError(f"Zone '{zoneName}' has no classic (WLD) variant in the client; it has {sorted(variants)}, which are not read yet")
  return classic


def buildZone(clientRoot, cacheRoot, zoneName):
  """Write a classic zone's region meshes and placed objects into a cache folder as one mesh, reusing one built from the same client
  files. An object no archive the zone loads defines is left out, as the client draws nothing for it, and listed."""
  source = zoneSource(clientRoot, zoneName)
  zoneFolder = cacheRoot / "zones" / f"{zoneName}@wld"
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
  details = stamp | {
    "zone": zoneName, "format": "wld", "archive": source["archive"].name.lower(), "regionMeshes": regionMeshCount, "placements": len(placements),
    "placedObjects": sum(placedCounts.values()), "objectArchives": objectArchives, "missingModels": sorted(missingModels), "particleCloudsNotDrawn": particleClouds,
  } | written
  stampPath.write_text(json.dumps(details, indent=1), encoding="utf-8")
  return zoneFolder, details
