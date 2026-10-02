"""The asset catalog: client graphical assets with what was measured from their files (assetSurvey) and what they were judged to be
(descriptions written in assetVocabulary's words). The repository's catalog folder holds what is worth keeping and sharing: measured/
with one file per surveyed client source, interpreted.json with the descriptions, and vocabulary.json with the terms added to the
vocabulary. The tooling root's catalog folder holds what the client regenerates and this machine alone uses: the extracted textures and
thumbnails, and the measurements of zone archives outside the client. A source's measured facts are rebuilt when its files' SHA-256 or
the survey's version changes; descriptions stay until rewritten. Texture and model ids carry a hash of the content, so a description
follows the asset across every zone that ships it."""
import hashlib
import json
import re

import assetSurvey
import assetVocabulary
import zoneSurvey

# Facts that differ between the sources an asset appears in; everything else is a property of the asset itself.
perSourceKeys = ("archives", "shadowed", "uses", "materials", "wldMaterials", "ecosystemLayers", "archive", "modelKind", "placements", "scaleRange")
descriptionKeys = ("category", "tags", "description", "usage", "worldUnitsPerRepeat", "pairsWith")
termPattern = re.compile(r"^[a-z][A-Za-z0-9]*$")
textSearchFields = ("description", "usage", "category")


class AssetCatalog:
  def __init__(self, toolingRoot, repositoryCatalogRoot):
    self.toolingRoot = toolingRoot
    self.root = repositoryCatalogRoot
    self.cacheRoot = toolingRoot / "catalog"
    self.interpretedPath = self.root / "interpreted.json"
    self.vocabularyPath = self.root / "vocabulary.json"
    self.mergedAssets = None

  def measuredFolder(self, sourceKey):
    """Client sources are kept in the repository; a zone archive outside the client is this machine's alone."""
    return (self.root if sourceKey.startswith(("zone:", "folder:")) else self.cacheRoot) / "measured"

  def sourcePath(self, sourceKey):
    slug = re.sub(r"[^a-z0-9]+", "_", sourceKey.lower()).strip("_")[-60:]
    return self.measuredFolder(sourceKey) / f"{slug}_{hashlib.sha256(sourceKey.encode()).hexdigest()[:8]}.json"

  def fileHashes(self, paths):
    """SHA-256 by file name, through the zone survey's memo of hashes by size and modification time."""
    cache = zoneSurvey.SurveyCache(self.toolingRoot)
    hashes = zoneSurvey.hashFiles(paths, cache, False)
    cache.save()
    return {path.name.lower(): hashes[path] for path in paths}

  def texturesPresent(self, surveyed):
    return all((assetSurvey.textureFolder(self.cacheRoot, facts["sha256"]) / facts["fileName"]).is_file() and assetSurvey.thumbnailPath(self.cacheRoot, facts["sha256"]).is_file()
      for facts in surveyed["assets"].values() if facts["kind"] == "texture" and "fileName" in facts)

  def survey(self, surveyFunction, sourceKey, sourcePaths, refresh):
    """The measured lane for one source, rebuilt when its files' SHA-256 or the survey's version changed, or when this machine lacks
    the textures extracted from it."""
    path = self.sourcePath(sourceKey)
    fileHashes = self.fileHashes(sourcePaths)
    if path.is_file() and not refresh:
      cached = json.loads(path.read_text(encoding="utf-8"))
      if cached["surveyVersion"] == assetSurvey.surveyVersion and cached["fileHashes"] == fileHashes and self.texturesPresent(cached):
        return cached
    surveyed = surveyFunction() | {"surveyVersion": assetSurvey.surveyVersion, "fileHashes": fileHashes}
    if self.fileHashes(sourcePaths) != fileHashes:
      raise ValueError(f"{sourceKey}: its files changed while it was surveyed; survey it again")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(surveyed, indent=1, sort_keys=True), encoding="utf-8")
    self.mergedAssets = None
    return surveyed

  def withFilePaths(self, facts):
    """A texture's facts with where this machine keeps its extracted file and thumbnail."""
    if facts["kind"] != "texture" or "fileName" not in facts:
      return facts
    return facts | {"file": str(assetSurvey.textureFolder(self.cacheRoot, facts["sha256"]) / facts["fileName"]), "thumbnail": str(assetSurvey.thumbnailPath(self.cacheRoot, facts["sha256"]))}

  def assets(self):
    """Every surveyed asset: the facts of the first source (by source key) that holds it, and per source what differs there."""
    if self.mergedAssets is None:
      merged = {}
      paths = sorted(path for folder in (self.root / "measured", self.cacheRoot / "measured") if folder.is_dir() for path in folder.glob("*.json"))
      for source in sorted((json.loads(path.read_text(encoding="utf-8")) for path in paths), key=lambda source: source["source"]):
        for assetID, facts in source["assets"].items():
          entry = merged.setdefault(assetID, {"id": assetID, "kind": facts["kind"], "name": facts["name"], "measured": {key: value for key, value in self.withFilePaths(facts).items() if key not in perSourceKeys}, "sources": {}})
          entry["sources"][source["source"]] = {key: facts[key] for key in perSourceKeys if key in facts}
          if facts["kind"] == "emitter":
            entry["measured"]["zones"] = entry["measured"]["zones"] | facts["zones"]
      self.mergedAssets = merged
    return self.mergedAssets

  def requireAsset(self, assetID):
    assets = self.assets()
    if assetID not in assets:
      raise ValueError(f"'{assetID}' is not in the catalog; survey the zone or folder that holds it (surveyAssets) first")
    return assets[assetID]

  def interpretations(self):
    return json.loads(self.interpretedPath.read_text(encoding="utf-8")) if self.interpretedPath.is_file() else {}

  def vocabularyExtensions(self):
    return json.loads(self.vocabularyPath.read_text(encoding="utf-8")) if self.vocabularyPath.is_file() else {"categories": {}, "tags": {}}

  def vocabulary(self):
    return assetVocabulary.merged(self.vocabularyExtensions())

  def extendVocabulary(self, group, term, meaning):
    """Add a term: group is a tag group's name, or category:<kind> for an asset kind's categories."""
    vocabulary = self.vocabulary()
    if not termPattern.match(term):
      raise ValueError(f"A term is one camelCase word starting lowercase, got '{term}'")
    if not meaning.strip():
      raise ValueError("A term needs its meaning")
    extensions = self.vocabularyExtensions()
    if group.startswith("category:"):
      kind = group.split(":", 1)[1]
      if kind not in vocabulary["categories"]:
        raise ValueError(f"No asset kind '{kind}'; kinds: {list(vocabulary['categories'])}")
      if term in vocabulary["categories"][kind]:
        raise ValueError(f"'{term}' is already a {kind} category: {vocabulary['categories'][kind][term]}")
      extensions["categories"].setdefault(kind, {})[term] = meaning.strip()
    else:
      if group not in vocabulary["tagGroups"]:
        raise ValueError(f"No tag group '{group}'; groups: {list(vocabulary['tagGroups'])} (or category:<kind>)")
      if term in vocabulary["tagGroups"][group]["terms"]:
        raise ValueError(f"'{term}' is already a {group} term: {vocabulary['tagGroups'][group]['terms'][term]}")
      extensions["tags"].setdefault(group, {})[term] = meaning.strip()
    self.root.mkdir(parents=True, exist_ok=True)
    self.vocabularyPath.write_text(json.dumps(extensions, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"group": group, "term": term, "meaning": meaning.strip()}

  def validatedDescription(self, description, vocabulary):
    unknown = sorted(set(description) - {"id"} - set(descriptionKeys))
    if unknown or "id" not in description:
      raise ValueError(f"A description is {{id, {', '.join(descriptionKeys)}}}; got {sorted(description)}")
    asset = self.requireAsset(description["id"])
    kind = asset["kind"]
    if description.get("category") not in vocabulary["categories"][kind]:
      raise ValueError(f"{description['id']}: category {description.get('category')!r} is not a {kind} category: {sorted(vocabulary['categories'][kind])}")
    tags = description.get("tags", {})
    if not isinstance(tags, dict):
      raise ValueError(f"{description['id']}: tags are {{group: [terms]}}")
    for group, terms in tags.items():
      if group not in vocabulary["tagGroups"]:
        raise ValueError(f"{description['id']}: no tag group '{group}'; groups: {list(vocabulary['tagGroups'])}")
      unknownTerms = sorted(set(terms) - set(vocabulary["tagGroups"][group]["terms"])) if isinstance(terms, list) and terms else None
      if unknownTerms is None or unknownTerms:
        raise ValueError(f"{description['id']}: {group} takes a non-empty list of {sorted(vocabulary['tagGroups'][group]['terms'])}; unknown: {unknownTerms}")
    for field in ("description", "usage"):
      if not isinstance(description.get(field), str) or not description[field].strip():
        raise ValueError(f"{description['id']}: {field} must be written: {'what it shows and how it reads' if field == 'description' else 'where and how to use it, at what scale, with what'}")
    repeat = description.get("worldUnitsPerRepeat")
    if repeat is not None and (kind != "texture" or not isinstance(repeat, (int, float)) or repeat <= 0):
      raise ValueError(f"{description['id']}: worldUnitsPerRepeat is a positive number of units, for textures only")
    for partner in description.get("pairsWith", []):
      self.requireAsset(partner)
    return {"category": description["category"], "tags": tags, "description": description["description"].strip(), "usage": description["usage"].strip(),
      "worldUnitsPerRepeat": repeat, "pairsWith": description.get("pairsWith", [])}

  def describe(self, descriptions):
    """Write descriptions, all or none: each replaces what its asset had."""
    vocabulary = self.vocabulary()
    validated = {description.get("id"): self.validatedDescription(description, vocabulary) for description in descriptions}
    if len(validated) != len(descriptions):
      raise ValueError("Each asset is described once per call")
    interpretations = self.interpretations() | validated
    self.root.mkdir(parents=True, exist_ok=True)
    self.interpretedPath.write_text(json.dumps(interpretations, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return sorted(validated)

  def entry(self, assetID):
    return self.requireAsset(assetID) | {"interpreted": self.interpretations().get(assetID)}

  def find(self, kind, text, categories, tags, source, described, sortBy, limit, colors=None, minimumSide=None, tiles=None, usedOn=None):
    """Assets matching every filter given: kind, words of text (each in the id, name, description, usage, category, tags, or measured
    names and color), any of categories, every tag ({group: [terms]}, each term required), a source (zone name or source key), and
    described or not; and for textures by what was measured: any of colors (the measured color names), a smallest side of at least
    minimumSide pixels, tiling or not, and usedOn, slope bands that together hold at least half the texture's area where it is used most."""
    unknownColors = sorted(set(colors or []) - set(assetSurvey.colorNames))
    unknownBands = sorted(set(usedOn or []) - {band for band, _, _ in assetSurvey.slopeBands})
    if unknownColors or unknownBands:
      raise ValueError(f"colors are among {list(assetSurvey.colorNames)} and usedOn among {[band for band, _, _ in assetSurvey.slopeBands]}; unknown: {unknownColors + unknownBands}")
    interpretations = self.interpretations()
    words = [word for word in re.split(r"\s+", text.lower()) if word] if text else []
    matches = []
    for asset in self.assets().values():
      interpreted = interpretations.get(asset["id"])
      if kind is not None and asset["kind"] != kind:
        continue
      if described is not None and (interpreted is not None) != described:
        continue
      if source is not None and not any(key == source or key == f"zone:{source.lower()}" or key == f"folder:{source}" for key in asset["sources"]):
        continue
      if categories and (interpreted is None or interpreted["category"] not in categories):
        continue
      if tags and (interpreted is None or any(not set(terms) <= set(interpreted["tags"].get(group, [])) for group, terms in tags.items())):
        continue
      measured = asset["measured"]
      if colors and measured.get("colorName") not in colors:
        continue
      if minimumSide is not None and min(measured.get("width", 0), measured.get("height", 0)) < minimumSide:
        continue
      if tiles is not None and measured.get("tiles") != tiles:
        continue
      if usedOn:
        use = mainUse(asset)
        if use is None or sum(use["slopeShares"][band] for band in usedOn) < 0.5:
          continue
      if words:
        haystack = " ".join([asset["id"], asset["name"], asset["measured"].get("colorName", ""), json.dumps(asset["measured"].get("zones", {})), json.dumps(asset["measured"].get("names", []))]
          + ([interpreted[field] for field in textSearchFields] + [term for terms in interpreted["tags"].values() for term in terms] if interpreted else [])).lower()
        if not all(word in haystack for word in words):
          continue
        relevance = sum(" ".join(interpreted[field] for field in textSearchFields).lower().count(word) for word in words) if interpreted else 0
      else:
        relevance = 0
      matches.append((relevance, asset, interpreted))
    sorters = {
      "relevance": lambda match: (-match[0], -largestAreaShare(match[1]), match[1]["id"]),
      "areaShare": lambda match: (-largestAreaShare(match[1]), match[1]["id"]),
      "name": lambda match: match[1]["id"],
    }
    if sortBy not in sorters:
      raise ValueError(f"sortBy is one of {list(sorters)}, got '{sortBy}'")
    matches.sort(key=sorters[sortBy])
    return {"total": len(matches), "assets": [compactEntry(asset, interpreted) for _, asset, interpreted in matches[:limit]]}


def largestAreaShare(asset):
  shares = [facts["uses"]["areaShare"] for facts in asset["sources"].values() if facts.get("uses")]
  return max(shares) if shares else 0.0


def mainUse(asset):
  """The use in the source where the texture covers the largest share of the zone: how big it repeats and on what slopes."""
  uses = [(facts["uses"], key) for key, facts in asset["sources"].items() if facts.get("uses")]
  if not uses:
    return None
  use, key = max(uses, key=lambda pair: pair[0]["areaShare"])
  return {"source": key} | {field: use[field] for field in ("areaShare", "unitsPerRepeat", "slopeShares", "objectShare")}


def compactEntry(asset, interpreted):
  measured = asset["measured"]
  entry = {"id": asset["id"], "kind": asset["kind"], "sources": sorted(asset["sources"])}
  if interpreted is not None:
    entry |= {key: interpreted[key] for key in descriptionKeys if interpreted.get(key) not in (None, [], {})}
  if asset["kind"] == "texture":
    roles = {}
    for facts in asset["sources"].values():
      for role, count in (facts.get("materials") or {}).get("roles", {}).items():
        roles[role] = roles.get(role, 0) + count
    entry |= {key: measured[key] for key in ("format", "colorName", "meanColor", "tiles", "transparentShare", "file") if key in measured}
    entry["size"] = f"{measured['width']}x{measured['height']}" if "width" in measured else None
    entry |= {key: value for key, value in (("mainUse", mainUse(asset)), ("materialRoles", roles)) if value}
  elif asset["kind"] == "model":
    entry |= {key: measured[key] for key in ("size", "triangles", "skinned", "materials", "problem") if key in measured}
    entry["placements"] = sum(facts.get("placements", 0) for facts in asset["sources"].values())
  elif asset["kind"] == "light":
    entry |= {key: measured[key] for key in ("zone", "count", "colors", "radii", "names", "flickerFrames")}
  elif asset["kind"] == "emitter":
    entry["zones"] = {zone: {"count": facts["count"], "names": facts["names"]} for zone, facts in measured["zones"].items()}
  else:
    entry |= {key: measured[key] for key in ("zone", "layers", "problem") if key in measured}
  return entry
