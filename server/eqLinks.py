"""Which archives the client loads and in what order, read from its own lists and from eqgame.exe, so a model, animation, or
texture is only ever taken from an archive the client loads in the zone. The client keeps the first definition of any name it
registers (EQGraphicsDX9.dll 0x100c77a0), so load order decides between archives that define the same name."""
import configparser

import zoneSources

# eqgame.exe loads <zone>2_chr.s3d for these zone ids (mapped to short names) and poknowledge_obj3.eqg for zone 202.
secondCharacterZones = {"nro", "oasis", "butcher", "timorous", "skyshrine", "necropolis", "mischiefplane", "growthplane", "sleeper", "thurgadinb"}
hardcodedZoneArchives = {"poknowledge": ["poknowledge_obj3.eqg"]}

# At startup the client loads global<code>_chr2 and global<code>_chr for races 1-12, 128, and 130, male then female, when
# eqgame.exe 0x48e510 allows it: the race's own UseLuclin setting, or for human and wood elf the setting of a race that borrows
# their animations (erudite; dark, half, and high elf).
playerRaces = (
  ("hu", "Human"), ("ba", "Barbarian"), ("er", "Erudite"), ("el", "WoodElf"), ("hi", "HighElf"), ("da", "DarkElf"), ("ha", "HalfElf"),
  ("dw", "Dwarf"), ("tr", "Troll"), ("og", "Ogre"), ("ho", "Halfling"), ("gn", "Gnome"), ("ik", "Iksar"), ("ke", "VahShir"),
)
playerGenders = (("m", "Male"), ("f", "Female"))
luclinBorrowers = {"hu": ("er",), "el": ("da", "ha", "hi")}
vahShir = "ke"
startupList = "eqgame.exe startup"


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
  """The archives the client loads for the zone variant it loads (zoneSources.loadedVariant), in its load order (eqgame.exe 0x49b200),
  each with the file or rule that links it; and named archives the client lacks. An EQG zone's <zone>.eqg loads first, and then no
  classic zone archive."""
  zoneName = zoneName.lower()
  zoneKind = zoneSources.loadedVariant(clientRoot, zoneName)[1]["format"]
  archives, missing = [], []

  def add(fileName, via, codes=None):
    found = clientFile(clientRoot, fileName)
    if found:
      archives.append({"archive": found, "via": via, "codes": codes})

  if zoneKind != "wld":
    add(f"{zoneName}.eqg", "zone load order")
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
  return {"zone": zoneName, "format": zoneKind, "archives": archives, "missing": missing}


def clientSettings(clientRoot):
  """eqclient.ini [Defaults]; absent when the client has none."""
  settings = configparser.ConfigParser(interpolation=None, strict=False)
  settings.optionxform = str
  iniPath = clientRoot / "eqclient.ini"
  if iniPath.is_file():
    settings.read_string(iniPath.read_text(encoding="latin1"))
  return settings["Defaults"] if settings.has_section("Defaults") else {}


def settingIsOn(defaults, name, absentIsOn):
  """The client reads a setting as on when it starts T, t, or 1."""
  value = defaults.get(name)
  return absentIsOn if value is None else value[:1] in ("T", "t", "1")


def globalLoadLines(clientRoot):
  """Resources/GlobalLoad.txt as (phase, archive file name, line). The client reads it in runs of one phase at a time, so the phases must not go backwards."""
  lines = []
  for line in readListLines(clientRoot / "Resources" / "GlobalLoad.txt"):
    fields = line.split(",")
    if len(fields) != 5 or fields[0] not in ("1", "2", "3", "4"):
      raise ValueError(f"GlobalLoad.txt line is not phase,flag,flags,name,description with phase 1-4: {line}")
    lines.append((int(fields[0]), archiveFileName(fields[3]), line))
  phases = [phase for phase, _, _ in lines]
  if phases != sorted(phases):
    raise ValueError("GlobalLoad.txt phases go backwards; the client reads the file in runs of one phase")
  return lines


def globalLinks(clientRoot):
  """Archives the client loads once at startup (eqgame.exe 0x491c20), in its order: GlobalLoad.txt phases 1 and 2; the Luclin
  player models; Global5 and frog mounts when UseLuclinElementals is on; Luclin equipment when any Luclin model loaded; classic
  Vah Shir models and equipment unless both Vah Shir Luclin settings are on; GlobalLoad.txt phases 3 and 4; GlobalLoad_chr.txt."""
  defaults = clientSettings(clientRoot)
  archives, missing = [], []

  def add(fileName, via, line=None):
    found = clientFile(clientRoot, fileName)
    if found:
      archives.append({"archive": found, "via": via, "codes": None})
    elif line is not None:
      missing.append({"list": via, "line": line})

  lines = globalLoadLines(clientRoot)
  for phase, fileName, line in lines:
    if phase in (1, 2):
      add(fileName, "GlobalLoad.txt", line)
  allOff = settingIsOn(defaults, "AllLuclinPcModelsOff", False)
  luclinOn = {(race, gender): settingIsOn(defaults, f"UseLuclin{raceName}{genderName}", True) for race, raceName in playerRaces for gender, genderName in playerGenders}
  anyLuclinLoaded = False
  for race, _ in playerRaces:
    for gender, _ in playerGenders:
      loads = luclinOn[(race, gender)] if race == vahShir else not allOff and (luclinOn[(race, gender)] or any(luclinOn[(borrower, gender)] for borrower in luclinBorrowers.get(race, ())))
      if not loads:
        continue
      add(f"global{race}{gender}_chr2.s3d", startupList)
      add(f"global{race}{gender}_chr.s3d", startupList)
      if race != vahShir:
        anyLuclinLoaded = True
      elif gender == "m" or not luclinOn[(vahShir, "m")]:
        add("vequip.s3d", startupList)
  if settingIsOn(defaults, "UseLuclinElementals", True):
    for fileName in ("global5_chr2.s3d", "global5_chr.s3d", "frog_mount_chr.s3d"):
      add(fileName, startupList)
  if anyLuclinLoaded:
    for fileName in ("lgequip_amr2.s3d", "lgequip_amr.s3d", "lgequip2.s3d", "lgequip.s3d"):
      add(fileName, startupList)
  if not (luclinOn[(vahShir, "m")] and luclinOn[(vahShir, "f")]):
    for fileName in ("gequip6.s3d", "global7_chr.s3d"):
      add(fileName, startupList)
  for phase, fileName, line in lines:
    if phase in (3, 4):
      add(fileName, "GlobalLoad.txt", line)
  listed, listMissing = characterListArchives(clientRoot, "Resources/GlobalLoad_chr.txt")
  return {"archives": archives + listed, "missing": missing + listMissing}


onDemandMemo = {}


def onDemandResources(clientRoot):
  """Resources/OnDemandResources.txt: archive^entry^resource^type, loaded when a resource is first needed. models maps a model name to its
  model (EQGM) and skinned (EQGS) entries; animations maps an EQG animation resource (EQGA) to its entries."""
  listPath = clientRoot / "Resources" / "OnDemandResources.txt"
  status = listPath.stat()
  memoKey = (str(listPath), status.st_size, status.st_mtime_ns)
  if memoKey not in onDemandMemo:
    onDemandMemo.clear()
    onDemandMemo[memoKey] = readOnDemandResources(listPath)
  return onDemandMemo[memoKey]


def readOnDemandResources(listPath):
  models, animations = {}, {}
  for line in readListLines(listPath):
    fields = line.split("^")
    if len(fields) != 4:
      raise ValueError(f"OnDemandResources.txt line has {len(fields)} fields: {line}")
    archive, entry, resource, resourceType = (field.strip() for field in fields)
    found = {"archive": archive.lower(), "entry": entry.lower(), "via": "OnDemandResources.txt"}
    if resourceType in ("EQGM", "EQGS"):
      models.setdefault(modelKey(resource), []).append(found)
    elif resourceType == "EQGA":
      animations.setdefault(resource.upper(), []).append(found)
  return {"models": models, "animations": animations}


def modelKey(name):
  """Doors, objects, and spawns name a model with or without _ACTORDEF; archives name it by entry or actor. All compare as the bare lowercase name."""
  return name.lower().removesuffix("_actordef")
