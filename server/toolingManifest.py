import json
import re
from pathlib import Path

manifestPath = Path(__file__).resolve().parent.parent / "toolingManifest.json"
versionPattern = re.compile(r"\d+\.\d+\.\d+")
sha256Pattern = re.compile(r"[0-9a-f]{64}")
commitPattern = re.compile(r"[0-9a-f]{40}")
extensionIDPattern = re.compile(r"[A-Za-z0-9_]+")
pinKeys = {"version", "url", "sha256"}
recastKeys = {"repository", "commit", "treeSha256"}
githubPrefix = "https://github.com/"
manifestKeys = {"blender", "extensions", "recast"}


def validatePin(pinName, pin):
  if not isinstance(pin, dict) or set(pin) != pinKeys:
    raise ValueError(f"{manifestPath.name}: {pinName} must have exactly the keys {sorted(pinKeys)}")
  if not versionPattern.fullmatch(pin["version"]):
    raise ValueError(f"{manifestPath.name}: {pinName}.version '{pin['version']}' is not MAJOR.MINOR.PATCH")
  if not isinstance(pin["url"], str) or not pin["url"]:
    raise ValueError(f"{manifestPath.name}: {pinName}.url must be a non-empty string")
  if not sha256Pattern.fullmatch(pin["sha256"]):
    raise ValueError(f"{manifestPath.name}: {pinName}.sha256 '{pin['sha256']}' is not 64 lowercase hex digits")


def validateRecastPin(pin):
  if not isinstance(pin, dict) or set(pin) != recastKeys:
    raise ValueError(f"{manifestPath.name}: recast must have exactly the keys {sorted(recastKeys)}")
  if not isinstance(pin["repository"], str) or not pin["repository"].startswith(githubPrefix) or pin["repository"].count("/") != 4:
    raise ValueError(f"{manifestPath.name}: recast.repository '{pin['repository']}' is not {githubPrefix}<owner>/<name>, which codeload serves as a zip")
  if not isinstance(pin["commit"], str) or not commitPattern.fullmatch(pin["commit"]):
    raise ValueError(f"{manifestPath.name}: recast.commit '{pin['commit']}' is not a 40-digit lowercase hex commit")
  if not isinstance(pin["treeSha256"], str) or not sha256Pattern.fullmatch(pin["treeSha256"]):
    raise ValueError(f"{manifestPath.name}: recast.treeSha256 '{pin['treeSha256']}' is not 64 lowercase hex digits")


def loadManifest():
  manifest = json.loads(manifestPath.read_text(encoding="ascii"))
  if set(manifest) != manifestKeys:
    raise ValueError(f"{manifestPath.name}: top level must have exactly the keys {sorted(manifestKeys)}")
  validatePin("blender", manifest["blender"])
  if not isinstance(manifest["extensions"], dict):
    raise ValueError(f"{manifestPath.name}: extensions must be an object keyed by extension ID")
  for extensionID, pin in manifest["extensions"].items():
    if not extensionIDPattern.fullmatch(extensionID):
      raise ValueError(f"{manifestPath.name}: extension ID '{extensionID}' is not alphanumeric/underscore")
    validatePin(f"extensions.{extensionID}", pin)
  validateRecastPin(manifest["recast"])
  return manifest


def saveManifest(manifest):
  manifestPath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii")
  loadManifest()
