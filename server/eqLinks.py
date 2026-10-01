"""Which archives the client loads, read from its own lists and from the load order in eqgame.exe, so a model or texture is only ever taken from an archive the client links to the zone."""
import configparser

import zoneSources

# eqgame.exe loads <zone>2_chr.s3d for these zone ids (mapped to short names) and poknowledge_obj3.eqg for zone 202.
secondCharacterZones = {"nro", "oasis", "butcher", "timorous", "skyshrine", "necropolis", "mischiefplane", "growthplane", "sleeper", "thurgadinb"}
hardcodedZoneArchives = {"poknowledge": ["poknowledge_obj3.eqg"]}

# At login the client loads global<code>_chr2 and global<code>_chr for races 1-12, 128, and 130, both genders, unless eqclient.ini turns that race's Luclin model off.
playerRaces = {
  "hu": "Human", "ba": "Barbarian", "er": "Erudite", "el": "WoodElf", "hi": "HighElf", "da": "DarkElf", "ha": "HalfElf",
  "dw": "Dwarf", "tr": "Troll", "og": "Ogre", "ho": "Halfling", "gn": "Gnome", "ik": "Iksar", "ke": "VahShir",
}
playerGenders = {"m": "Male", "f": "Female"}


def clientFile(clientRoot, fileName):
  """The client file of that name (the client's names are case-insensitive), or None."""
  path = clientRoot / fileName
  return path.name.lower() if path.is_file() else None


def archiveFileName(listedName):
  """A load list names an EQG with its extension and an S3D without one."""
  name = listedName.strip().lower()
  return name if name.endswith((".eqg", ".s3d")) else name + ".s3d"


def readListLines(path):
  return [line.strip() for line in path.read_text(encoding="latin1").splitlines() if line.strip()]


def zoneFormat(clientRoot, zoneName):
  formats = {source["format"] for source in zoneSources.zoneVariants(clientRoot, zoneName).values()}
  if not formats:
    raise ValueError(f"'{zoneName}' is not a zone in {clientRoot}")
  if len(formats) > 1:
    raise ValueError(f"Zone '{zoneName}' ships both a classic and an EQG version ({sorted(formats)}); which one the client loads is not linked in its files")
  return formats.pop()


def characterListArchives(clientRoot, listName):
  """<zone>_chr.txt: a count line, then code,source. When the source starts with the code the client loads source.eqg (or, failing that, source.s3d); otherwise it loads only that code's actor from source.s3d."""
  path = clientRoot / listName
  if not path.is_file():
    return [], []
  archives, missing = [], []
  for line in readListLines(path)[1:]:
    code, source = (field.strip() for field in line.split(",", 1))
    if source[:3] == code[:3]:
      candidates = [source.lower() + ".eqg", source.lower() + ".s3d"]
      codes = None
    else:
      candidates = [source.lower() + ".s3d"]
      codes = [code.lower()]
    found = next((name for name in candidates if clientFile(clientRoot, name)), None)
    if found is None:
      missing.append({"list": listName, "line": line})
    else:
      archives.append({"archive": found, "via": listName, "codes": codes})
  return archives, missing


def zoneLinks(clientRoot, zoneName):
  """The archives the client loads for a zone, in its load order, each with the file or rule that links it; and named archives the client lacks."""
  zoneName = zoneName.lower()
  zoneKind = zoneFormat(clientRoot, zoneName)
  archives, missing = [], []

  def add(fileName, via, codes=None):
    found = clientFile(clientRoot, fileName)
    if found:
      archives.append({"archive": found, "via": via, "codes": codes})

  listed, listMissing = characterListArchives(clientRoot, f"{zoneName}_pre_chr.txt")
  archives += listed
  missing += listMissing
  for fileName in hardcodedZoneArchives.get(zoneName, []):
    add(fileName, "eqgame.exe")
  if zoneKind == "wld":
    for pattern in ("{}_obj2.s3d", "{}_obj.s3d", "{}.s3d", "{}_2_obj.s3d", "{}_chr2.s3d"):
      add(pattern.format(zoneName), "zone load order")
    if zoneName in secondCharacterZones:
      add(f"{zoneName}2_chr.s3d", "eqgame.exe")
    add(f"{zoneName}_chr.s3d", "zone load order")
  listed, listMissing = characterListArchives(clientRoot, f"{zoneName}_chr.txt")
  archives += listed
  missing += listMissing
  assetList = clientRoot / f"{zoneName}_assets.txt"
  if assetList.is_file():
    for line in readListLines(assetList):
      if not line.lower().endswith(".eqg"):
        missing.append({"list": assetList.name, "line": line, "reason": "not an .eqg entry"})
      elif clientFile(clientRoot, line):
        archives.append({"archive": line.lower(), "via": assetList.name, "codes": None})
      else:
        missing.append({"list": assetList.name, "line": line})
  if zoneKind != "wld":
    add(f"{zoneName}.eqg", "zone load order")
  return {"zone": zoneName, "format": zoneKind, "archives": archives, "missing": missing}


def luclinPlayerCodes(clientRoot):
  """Player model codes whose Luclin model eqclient.ini leaves on (the client's default when a setting is absent)."""
  settings = configparser.ConfigParser(interpolation=None, strict=False)
  settings.optionxform = str
  iniPath = clientRoot / "eqclient.ini"
  if iniPath.is_file():
    settings.read_string(iniPath.read_text(encoding="latin1"))
  defaults = settings["Defaults"] if settings.has_section("Defaults") else {}
  if defaults.get("AllLuclinPcModelsOff", "FALSE").upper() == "TRUE":
    return []
  return [race + gender for race, raceName in playerRaces.items() for gender, genderName in playerGenders.items() if defaults.get(f"UseLuclin{raceName}{genderName}", "TRUE").upper() == "TRUE"]


def globalLinks(clientRoot):
  """Archives loaded for every zone: the player models eqclient.ini enables, Resources/GlobalLoad.txt (phase,flag,flags,name,description), and Resources/GlobalLoad_chr.txt (read like a zone's _chr.txt)."""
  archives, missing = [], []
  for code in luclinPlayerCodes(clientRoot):
    for fileName in (f"global{code}_chr2.s3d", f"global{code}_chr.s3d"):
      found = clientFile(clientRoot, fileName)
      if found:
        archives.append({"archive": found, "via": "eqclient.ini", "codes": None})
  for line in readListLines(clientRoot / "Resources" / "GlobalLoad.txt"):
    fields = line.split(",")
    if len(fields) != 5:
      raise ValueError(f"GlobalLoad.txt line has {len(fields)} fields: {line}")
    fileName = archiveFileName(fields[3])
    if clientFile(clientRoot, fileName):
      archives.append({"archive": fileName, "via": "GlobalLoad.txt", "codes": None})
    else:
      missing.append({"list": "GlobalLoad.txt", "line": line})
  listed, listMissing = characterListArchives(clientRoot, "Resources/GlobalLoad_chr.txt")
  return {"archives": archives + listed, "missing": missing + listMissing}


onDemandMemo = {}


def onDemandResources(clientRoot):
  """Resources/OnDemandResources.txt: archive^entry^actor^type, loaded when an actor is first needed. Model name to its static (EQGM) and skinned (EQGS) entries."""
  listPath = clientRoot / "Resources" / "OnDemandResources.txt"
  status = listPath.stat()
  memoKey = (str(listPath), status.st_size, status.st_mtime_ns)
  if memoKey not in onDemandMemo:
    onDemandMemo.clear()
    onDemandMemo[memoKey] = readOnDemandResources(listPath)
  return onDemandMemo[memoKey]


def readOnDemandResources(listPath):
  models = {}
  for line in readListLines(listPath):
    fields = line.split("^")
    if len(fields) != 4:
      raise ValueError(f"OnDemandResources.txt line has {len(fields)} fields: {line}")
    archive, entry, actor, resourceType = (field.strip() for field in fields)
    if resourceType in ("EQGM", "EQGS"):
      models.setdefault(modelKey(actor), []).append({"archive": archive.lower(), "entry": entry.lower(), "via": "OnDemandResources.txt"})
  return models


def modelKey(name):
  """Doors, objects, and spawns name a model with or without _ACTORDEF; archives name it by entry or actor. All compare as the bare lowercase name."""
  return name.lower().removesuffix("_actordef")
