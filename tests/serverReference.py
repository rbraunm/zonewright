"""Peridot's server files for the reference zones, as test fixtures: EQEmu's map pack at the commit Peridot's maps are byte-identical to,
each file fetched into the shared test root when missing and every one verified against its recorded size and SHA-256, failing (never
skipping) on a mismatch. They are server data, so they stay under the test root and out of git."""
import functools
import json
import os
import urllib.request
from pathlib import Path

from conftest import exclusiveLock, sha256OfFile

pin = json.loads(Path(__file__).with_name("serverReference.json").read_text(encoding="ascii"))
referenceRoot = Path(os.environ["LOCALAPPDATA"]) / "zonewrightTests" / "serverReference" / pin["commit"][:8]


@functools.cache
def verifiedReferenceRoot():
  referenceRoot.mkdir(parents=True, exist_ok=True)
  with exclusiveLock(referenceRoot / "install.lock"):
    for entry in pin["files"]:
      filePath = referenceRoot / entry["path"]
      if not filePath.is_file():
        filePath.parent.mkdir(parents=True, exist_ok=True)
        partialPath = filePath.with_name(filePath.name + ".partial")
        request = urllib.request.Request(f"{pin['repository']}/{pin['commit']}/{entry['path']}", headers={"User-Agent": "zonewright"})
        with urllib.request.urlopen(request) as response:
          partialPath.write_bytes(response.read())
        partialPath.replace(filePath)
  mismatches = [entry["path"] for entry in pin["files"] if (referenceRoot / entry["path"]).stat().st_size != entry["bytes"] or sha256OfFile(referenceRoot / entry["path"]) != entry["sha256"]]
  assert not mismatches, f"Reference files under {referenceRoot} differ from serverReference.json: {mismatches}"
  return referenceRoot


def referenceBytes(path):
  """One reference file's bytes, by its path in the map pack (base/highpasshold.map)."""
  if path not in {entry["path"] for entry in pin["files"]}:
    raise KeyError(f"{path} is not a recorded reference file")
  return (verifiedReferenceRoot() / path).read_bytes()
