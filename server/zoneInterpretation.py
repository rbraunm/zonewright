"""The zone survey's interpretive lane: Claude's readings of zones with their screenshots, current until the zone's files or the procedure's version change."""
import json
import math
import os
import re
import shutil
from pathlib import Path

import eqZones
import zoneSurvey

versionPattern = re.compile(r"^Interpretive procedure version: (\d+)$", re.MULTILINE)
zoneTypesSectionPattern = re.compile(r"^### Zone types\n(.*?)(?=^#|\Z)", re.MULTILINE | re.DOTALL)
zoneTypeRowPattern = re.compile(r"^\| `([a-z][A-Za-z0-9]*)` \| (.+?) \|$", re.MULTILINE)
termPattern = re.compile(r"^[a-z][A-Za-z0-9]*$")
zoneLinkPrefix = "zone:"
pngSignature = b"\x89PNG\r\n\x1a\n"
viewKinds = ("map", "oblique", "eyeLevel", "section")
closeViewKinds = ("oblique", "eyeLevel")
requiredFields = ("zoneType", "character", "areas", "landmarks", "definingCharacteristics", "screenshots")
optionalFields = ("structuredValues",)
areaKeys = ("name", "center", "where", "what", "connections")
connectionKeys = ("to", "by")
landmarkKeys = ("name", "location", "what", "significance")
screenshotKeys = ("path", "view", "caption", "shows")
structuredValueKeys = ("value", "unit", "how")


def readProcedure(skillPath):
  """The interpretive procedure's version and zone types, as the zone-survey skill states them."""
  if not skillPath.is_file():
    raise ValueError(f"No zone-survey skill at {skillPath}; it holds the interpretive procedure")
  text = skillPath.read_text(encoding="utf-8")
  versions = versionPattern.findall(text)
  section = zoneTypesSectionPattern.search(text)
  zoneTypes = dict(zoneTypeRowPattern.findall(section.group(1))) if section else {}
  if len(versions) != 1 or not zoneTypes:
    raise ValueError(
      f"{skillPath} must state the interpretive procedure's version once, as 'Interpretive procedure version: <n>' (found {len(versions)}),"
      f" and its zone types as a table under '### Zone types' (found {len(zoneTypes)})"
    )
  return {"version": int(versions[0]), "zoneTypes": zoneTypes}


def trimmed(value):
  if isinstance(value, str):
    return value.strip()
  if isinstance(value, list):
    return [trimmed(item) for item in value]
  if isinstance(value, dict):
    return {key: trimmed(item) for key, item in value.items()}
  return value


def isText(value):
  return isinstance(value, str) and value != ""


def isNumber(value):
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def isPoint(value):
  return isinstance(value, list) and len(value) == 3 and all(isNumber(number) for number in value)


def objectsWithKeys(value, field, keys, problems, allowEmpty):
  """The items of a list of objects that each hold exactly `keys`, noting every list or item that does not."""
  if not isinstance(value, list) or (not value and not allowEmpty):
    problems.append(f"{field} is a {'' if allowEmpty else 'non-empty '}list of {{{', '.join(keys)}}}")
    return []
  wellFormed = []
  for index, item in enumerate(value):
    if isinstance(item, dict) and set(item) == set(keys):
      wellFormed.append(item)
    else:
      problems.append(f"{field}[{index}] holds {sorted(item) if isinstance(item, dict) else type(item).__name__}, not {{{', '.join(keys)}}}")
  return wellFormed


def textList(value, field, problems):
  if not isinstance(value, list) or not value or not all(isText(item) for item in value):
    problems.append(f"{field} is a non-empty list of written statements")
  elif len(set(value)) != len(value):
    problems.append(f"{field} repeats an entry")


def uniqueNames(items, field, problems):
  names = [item["name"] for item in items if isText(item["name"])]
  if len(names) != len(items):
    problems.append(f"every entry of {field} needs a name")
  repeated = sorted({name for name in names if names.count(name) > 1})
  if repeated:
    problems.append(f"{field} names repeat: {repeated}")
  return set(names)


def isNameList(value):
  return isinstance(value, list) and all(isinstance(name, str) for name in value)


def checkCharacter(character, problems):
  if not isinstance(character, dict) or set(character) != {"description", "tags"}:
    problems.append("character is {description, tags}")
    return
  if not isText(character["description"]):
    problems.append("character.description must be written: the zone's mood and setting as the pictures show them")
  tags = character["tags"]
  if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) and termPattern.match(tag) for tag in tags):
    problems.append(f"character.tags is a non-empty list of camelCase terms, got {tags!r}")
  elif len(set(tags)) != len(tags):
    problems.append("character.tags repeats a term")


def checkAreas(areas, clientZones, problems):
  """Area names, after noting every problem with the areas and the connections between them."""
  names = uniqueNames(areas, "areas", problems)
  for area in areas:
    label = f"area {area['name']!r}"
    if not isPoint(area["center"]):
      problems.append(f"{label}: center is [x, y, z] in the scene's coordinates")
    for field in ("where", "what"):
      if not isText(area[field]):
        problems.append(f"{label}: {field} must be written")
    connections = objectsWithKeys(area["connections"], f"{label} connections", connectionKeys, problems, allowEmpty=len(areas) == 1)
    for connection in connections:
      target = connection["to"]
      if isinstance(target, str) and target.startswith(zoneLinkPrefix):
        if target[len(zoneLinkPrefix):] not in clientZones:
          problems.append(f"{label}: connection to {target!r} names no zone in the client")
      elif not isinstance(target, str) or target == area["name"] or target not in names:
        problems.append(f"{label}: connection to {target!r} is neither another area nor {zoneLinkPrefix}<zone short name>")
      if not isText(connection["by"]):
        problems.append(f"{label}: its connection to {target!r} must say by what (a gate, tunnel, ramp, bridge, zone line, ...)")
  return names


def checkLandmarks(landmarks, areaNames, problems):
  names = uniqueNames(landmarks, "landmarks", problems)
  shared = sorted(names & areaNames)
  if shared:
    problems.append(f"names both an area and a landmark, so a screenshot's shows cannot tell them apart: {shared}")
  for landmark in landmarks:
    label = f"landmark {landmark['name']!r}"
    if not isPoint(landmark["location"]):
      problems.append(f"{label}: location is [x, y, z] in the scene's coordinates")
    for field in ("what", "significance"):
      if not isText(landmark[field]):
        problems.append(f"{label}: {field} must be written")
  return names


def checkScreenshots(screenshots, areaNames, landmarkNames, problems):
  seenPaths = set()
  for index, screenshot in enumerate(screenshots):
    label = f"screenshots[{index}]"
    path = Path(screenshot["path"]) if isText(screenshot["path"]) else None
    if path is None or not path.is_absolute() or not path.is_file():
      problems.append(f"{label}: path {screenshot['path']!r} is not an absolute path to an existing file")
    else:
      with path.open("rb") as image:
        if image.read(len(pngSignature)) != pngSignature:
          problems.append(f"{label}: {path} is not a PNG")
      normalized = os.path.normcase(str(path.resolve()))
      if normalized in seenPaths:
        problems.append(f"{label}: {path} is listed twice")
      seenPaths.add(normalized)
    if screenshot["view"] not in viewKinds:
      problems.append(f"{label}: view {screenshot['view']!r} is one of {list(viewKinds)}")
    if not isText(screenshot["caption"]):
      problems.append(f"{label}: caption must say what it shows and from where")
    shows = screenshot["shows"]
    if not isNameList(shows):
      problems.append(f"{label}: shows is a list of area and landmark names")
    else:
      unknown = sorted(set(shows) - areaNames - landmarkNames)
      if unknown:
        problems.append(f"{label}: shows names no area or landmark: {unknown}")
      if not shows and screenshot["view"] in viewKinds and screenshot["view"] != "map":
        problems.append(f"{label}: shows names none of the areas and landmarks in it")
  views = [(screenshot["view"], set(screenshot["shows"]) if isNameList(screenshot["shows"]) else set()) for screenshot in screenshots]
  if not any(view == "map" for view, _ in views):
    problems.append("no map view: the procedure starts from a map of the whole zone")
  for name in sorted(areaNames):
    for kind in closeViewKinds:
      if not any(view == kind and name in shown for view, shown in views):
        problems.append(f"area {name!r} has no {kind} view")
  for name in sorted(landmarkNames):
    if not any(view in closeViewKinds and name in shown for view, shown in views):
      problems.append(f"landmark {name!r} has no oblique or eyeLevel view")


def checkStructuredValues(values, problems):
  if not isinstance(values, dict):
    problems.append("structuredValues is {camelCaseName: {value, unit, how}}")
    return
  for name, entry in values.items():
    if not termPattern.match(name):
      problems.append(f"structuredValues name {name!r} is one camelCase term")
    if not isinstance(entry, dict) or set(entry) != set(structuredValueKeys):
      problems.append(f"structuredValues {name!r} is {{value, unit, how}}")
    elif not isNumber(entry["value"]) or not isText(entry["unit"]) or not isText(entry["how"]):
      problems.append(f"structuredValues {name!r}: value is a number, and unit and how (what was measured, and how) are written")


def validatedInterpretation(interpretation, zoneTypes, clientZones):
  """The interpretation with its text trimmed, or ValueError listing every problem in it."""
  interpretation = trimmed(interpretation)
  problems = []
  unknownFields = sorted(set(interpretation) - set(requiredFields) - set(optionalFields))
  if unknownFields:
    problems.append(f"unknown fields {unknownFields}; an interpretation holds {list(requiredFields)} and optionally {list(optionalFields)}")
  problems += [f"{field} is missing" for field in requiredFields if field not in interpretation]
  if "zoneType" in interpretation and interpretation["zoneType"] not in zoneTypes:
    problems.append(f"zoneType {interpretation['zoneType']!r} is not one of the zone-survey skill's zone types: {sorted(zoneTypes)}")
  if "character" in interpretation:
    checkCharacter(interpretation["character"], problems)
  if "definingCharacteristics" in interpretation:
    textList(interpretation["definingCharacteristics"], "definingCharacteristics", problems)
  areaNames = checkAreas(objectsWithKeys(interpretation["areas"], "areas", areaKeys, problems, allowEmpty=False), clientZones, problems) if "areas" in interpretation else set()
  landmarks = objectsWithKeys(interpretation["landmarks"], "landmarks", landmarkKeys, problems, allowEmpty=True) if "landmarks" in interpretation else []
  landmarkNames = checkLandmarks(landmarks, areaNames, problems)
  if "screenshots" in interpretation:
    checkScreenshots(objectsWithKeys(interpretation["screenshots"], "screenshots", screenshotKeys, problems, allowEmpty=False), areaNames, landmarkNames, problems)
  if "structuredValues" in interpretation:
    checkStructuredValues(interpretation["structuredValues"], problems)
  if problems:
    raise ValueError("Interpretation refused, nothing kept:\n- " + "\n- ".join(problems))
  return interpretation


def interpretationFolder(toolingRoot, zoneName):
  return toolingRoot / "survey" / "interpretations" / zoneName


def drawnVariantHashes(clientRoot, cache, zoneName):
  """The key of the variant importZone draws for the zone, and its source files' SHA-256 now."""
  key, source = eqZones.drawnVariant(clientRoot, zoneName)
  return key, zoneSurvey.variantFileHashes(clientRoot, cache, {key: source})[key]


def clientZoneNames(clientRoot, cache):
  return {variant["zone"] for variant in zoneSurvey.discoveredVariants(clientRoot, cache).values()}


def withScreenshotPaths(interpretation, folder):
  return interpretation | {"screenshots": [screenshot | {"path": str(folder / screenshot["path"])} for screenshot in interpretation["screenshots"]]}


def recordInterpretation(clientRoot, toolingRoot, skillPath, zoneName, interpretation):
  """Validate an interpretation and keep it, with copies of its screenshots, in place of the zone's earlier one."""
  procedure = readProcedure(skillPath)
  cache = zoneSurvey.SurveyCache(toolingRoot)
  clientZones = clientZoneNames(clientRoot, cache)
  if zoneName not in clientZones:
    raise ValueError(f"'{zoneName}' is not a zone in {clientRoot}")
  validated = validatedInterpretation(interpretation, procedure["zoneTypes"], clientZones)
  key, fileHashes = drawnVariantHashes(clientRoot, cache, zoneName)
  cache.save()
  folder = interpretationFolder(toolingRoot, zoneName)
  # Built beside the kept one and swapped in, so a record whose screenshots are the kept copies of the last one still finds them.
  staging = folder.with_name(f"{zoneName}.recording")
  if staging.exists():
    shutil.rmtree(staging)
  staging.mkdir(parents=True)
  screenshots = []
  for number, screenshot in enumerate(validated["screenshots"], start=1):
    fileName = f"screenshot{number:02d}.png"
    shutil.copyfile(screenshot["path"], staging / fileName)
    screenshots.append(screenshot | {"path": fileName})
  stored = {"zone": zoneName, "variant": key, "procedureVersion": procedure["version"], "fileHashes": fileHashes, "interpretation": validated | {"screenshots": screenshots}}
  (staging / "interpretation.json").write_text(json.dumps(stored, indent=1), encoding="utf-8")
  if folder.exists():
    shutil.rmtree(folder)
  staging.rename(folder)
  return {
    "zone": zoneName, "variant": key, "procedureVersion": procedure["version"], "state": "current", "areas": len(validated["areas"]),
    "landmarks": len(validated["landmarks"]), "screenshots": [str(folder / screenshot["path"]) for screenshot in screenshots],
  }


def interpretationState(clientRoot, toolingRoot, skillPath, zoneName):
  """The zone's interpretation: none, current, or stale with why (its zone files or the procedure's version changed since it was recorded)."""
  procedure = readProcedure(skillPath)
  cache = zoneSurvey.SurveyCache(toolingRoot)
  key, fileHashes = drawnVariantHashes(clientRoot, cache, zoneName)
  cache.save()
  folder = interpretationFolder(toolingRoot, zoneName)
  current = {"variant": key, "procedureVersion": procedure["version"]}
  storedPath = folder / "interpretation.json"
  if not storedPath.is_file():
    return {"state": "none"} | current
  stored = json.loads(storedPath.read_text(encoding="utf-8"))
  staleBecause = {}
  changedFiles = sorted(name for name in set(stored["fileHashes"]) | set(fileHashes) if stored["fileHashes"].get(name) != fileHashes.get(name))
  if changedFiles:
    staleBecause["zoneFilesChanged"] = changedFiles
  if stored["procedureVersion"] != procedure["version"]:
    staleBecause["procedureChanged"] = {"recorded": stored["procedureVersion"], "current": procedure["version"]}
  return (
    {"state": "stale" if staleBecause else "current"} | current | ({"staleBecause": staleBecause} if staleBecause else {})
    | {"interpretation": withScreenshotPaths(stored["interpretation"], folder)}
  )
