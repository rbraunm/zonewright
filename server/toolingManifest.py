import json
import re
from pathlib import Path

manifestPath = Path(__file__).resolve().parent.parent / "toolingManifest.json"
versionPattern = re.compile(r"\d+\.\d+\.\d+")
sha256Pattern = re.compile(r"[0-9a-f]{64}")
extensionIDPattern = re.compile(r"[A-Za-z0-9_]+")
pinKeys = {"version", "url", "sha256"}


def validatePin(pinName, pin):
  if not isinstance(pin, dict) or set(pin) != pinKeys:
    raise ValueError(f"{manifestPath.name}: {pinName} must have exactly the keys {sorted(pinKeys)}")
  if not versionPattern.fullmatch(pin["version"]):
    raise ValueError(f"{manifestPath.name}: {pinName}.version '{pin['version']}' is not MAJOR.MINOR.PATCH")
  if not isinstance(pin["url"], str) or not pin["url"]:
    raise ValueError(f"{manifestPath.name}: {pinName}.url must be a non-empty string")
  if not sha256Pattern.fullmatch(pin["sha256"]):
    raise ValueError(f"{manifestPath.name}: {pinName}.sha256 '{pin['sha256']}' is not 64 lowercase hex digits")


def loadManifest():
  manifest = json.loads(manifestPath.read_text(encoding="ascii"))
  if set(manifest) != {"blender", "extensions"}:
    raise ValueError(f"{manifestPath.name}: top level must have exactly the keys ['blender', 'extensions']")
  validatePin("blender", manifest["blender"])
  if not isinstance(manifest["extensions"], dict):
    raise ValueError(f"{manifestPath.name}: extensions must be an object keyed by extension ID")
  for extensionID, pin in manifest["extensions"].items():
    if not extensionIDPattern.fullmatch(extensionID):
      raise ValueError(f"{manifestPath.name}: extension ID '{extensionID}' is not alphanumeric/underscore")
    validatePin(f"extensions.{extensionID}", pin)
  return manifest


def saveManifest(manifest):
  manifestPath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii")
  loadManifest()
