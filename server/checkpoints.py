"""Recovery points of work files: copies of the saved .blend under the tooling root, one folder per work file."""
import datetime
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

recordName = "workFile.json"
maximumLabelLength = 60
forbiddenLabelCharacters = set('<>:"/\\|?*')
stemPattern = re.compile(r"^(\d{8}T\d{6}\.\d{3}Z)_(.+)$")


def checkpointsRoot(toolingRoot):
  return toolingRoot / "checkpoints"


def comparablePath(path):
  return os.path.normcase(os.path.abspath(path))


def samePath(first, second):
  return comparablePath(first) == comparablePath(second)


def requireWorkFilePath(workFile):
  if not os.path.isabs(workFile) or not workFile.lower().endswith(".blend"):
    raise ValueError(f"'{workFile}' is not an absolute path to a .blend file")


def validateLabel(label):
  if not 1 <= len(label) <= maximumLabelLength:
    raise ValueError(f"A checkpoint label takes 1 to {maximumLabelLength} characters, got {len(label)}")
  badCharacters = sorted({character for character in label if not " " <= character <= "~" or character in forbiddenLabelCharacters})
  if badCharacters:
    raise ValueError(f"A checkpoint label is part of a file name: printable ASCII without {''.join(sorted(forbiddenLabelCharacters))}; '{label}' has {badCharacters}")
  if label != label.strip(" ."):
    raise ValueError(f"A checkpoint label is part of a file name and cannot start or end with a space or a dot: '{label}'")


def workFileKey(workFile):
  """The work file's name and a short hash of its full path, so work files of one name in different folders never share checkpoints."""
  return f"{Path(workFile).stem}-{hashlib.sha256(comparablePath(workFile).encode('utf-8')).hexdigest()[:8]}"


def recordedWorkFile(folder):
  return json.loads((folder / recordName).read_text(encoding="utf-8"))["workFile"]


def workFileFolder(toolingRoot, workFile):
  requireWorkFilePath(workFile)
  folder = checkpointsRoot(toolingRoot) / workFileKey(workFile)
  if folder.is_dir() and not samePath(recordedWorkFile(folder), workFile):
    raise ValueError(f"Checkpoint folder '{folder}' belongs to '{recordedWorkFile(folder)}', not '{workFile}'")
  return folder


def checkpointNameParts(checkpointPath):
  match = stemPattern.match(checkpointPath.stem)
  if match is None:
    raise ValueError(f"'{checkpointPath}' is not named as a checkpoint (<UTC time>_<label>.blend)")
  moment = datetime.datetime.strptime(match[1], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=datetime.UTC)
  return match[1], moment, match[2]


def describe(checkpointPath):
  _, moment, label = checkpointNameParts(checkpointPath)
  return {
    "name": f"{checkpointPath.parent.name}/{checkpointPath.stem}", "label": label,
    "time": moment.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "sizeBytes": checkpointPath.stat().st_size,
  }


def saveCheckpoint(toolingRoot, workFile, label):
  """Copy the saved work file to a new checkpoint named for the UTC time and the label."""
  validateLabel(label)
  folder = workFileFolder(toolingRoot, workFile)
  if not folder.is_dir():
    folder.mkdir(parents=True)
    (folder / recordName).write_text(json.dumps({"workFile": workFile}), encoding="utf-8")
  moment = datetime.datetime.now(datetime.UTC)
  stamp = moment.strftime("%Y%m%dT%H%M%S.") + f"{moment.microsecond // 1000:03d}Z"
  if any(folder.glob(f"{stamp}_*.blend")):
    raise ValueError(f"A checkpoint of '{workFile}' already has the time {stamp}; save the checkpoint again")
  checkpointPath = folder / f"{stamp}_{label}.blend"
  shutil.copyfile(workFile, checkpointPath)
  return describe(checkpointPath) | {"workFile": workFile, "path": str(checkpointPath)}


def listCheckpoints(toolingRoot, workFile):
  folder = workFileFolder(toolingRoot, workFile)
  checkpoints = [describe(path) for path in sorted(folder.glob("*.blend"))]
  return {"workFile": workFile, "folder": str(folder), "checkpoints": checkpoints, "totalSizeBytes": sum(checkpoint["sizeBytes"] for checkpoint in checkpoints)}


def findCheckpoint(toolingRoot, name):
  """A checkpoint's file and the work file it was saved from, by the name listCheckpoints gives it."""
  checkpointPaths = {f"{path.parent.name}/{path.stem}": path for path in checkpointsRoot(toolingRoot).glob("*/*.blend")}
  if name not in checkpointPaths:
    raise ValueError(f"No checkpoint is named '{name}'; listCheckpoints gives the names")
  return checkpointPaths[name], recordedWorkFile(checkpointPaths[name].parent)


def restoreOver(checkpointPath, workFile):
  shutil.copyfile(checkpointPath, workFile)


def deleteCheckpoints(toolingRoot, names):
  if not names:
    raise ValueError("Name the checkpoints to delete")
  if len(set(names)) != len(names):
    raise ValueError(f"A checkpoint is named twice: {sorted(name for name in set(names) if names.count(name) > 1)}")
  checkpointPaths = [findCheckpoint(toolingRoot, name)[0] for name in names]
  for checkpointPath in checkpointPaths:
    checkpointPath.unlink()
  for folder in {checkpointPath.parent for checkpointPath in checkpointPaths}:
    if not any(folder.glob("*.blend")):
      (folder / recordName).unlink()
      folder.rmdir()
  return {"deleted": names}
