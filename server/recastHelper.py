"""The Recast helper: recastHelper.exe built from the repository's recastHelper/ sources against the pinned recastnavigation tree with
this machine's Visual Studio, kept per fingerprint under the tooling root; its status, its input files, and its runs."""
import functools
import hashlib
import json
import logging
import struct
import subprocess
import tempfile
import time
import winreg
from pathlib import Path

import numpy
from mcp.server.mcpserver.exceptions import ToolError

import toolingManifest

logger = logging.getLogger(__name__)
sourcesPath = Path(__file__).resolve().parent.parent / "recastHelper"
treeParts = ("Recast", "Detour", "RecastDemo/Include/ChunkyTriMesh.h", "RecastDemo/Source/ChunkyTriMesh.cpp")
visualStudioComponents = ("Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "Microsoft.VisualStudio.Component.VC.CMake.Project")
visualStudioNeeds = 'Visual Studio 2022 with the "Desktop development with C++" workload and its "C++ CMake tools for Windows" component'
configureArguments = ("-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release")
executableName = "recastHelper.exe"
buildRecordName = "build.json"
fingerprintFolderLength = 16
navMagic = b"ZWNAV001"
inspectMagic = b"ZWINSP01"
detourAreaCount = 64
navSettingFloats = (
  "cellSize", "cellHeight", "agentHeight", "agentRadius", "agentClimb", "maxSlope", "regionMinSize", "regionMergeSize", "edgeMaxLength",
  "edgeMaxError", "detailSampleDistance", "detailSampleMaxError",
)
navSettingIntegers = ("verticesPerPolygon", "tileSize", "borderSize")


def sha256Of(data):
  return hashlib.sha256(data).hexdigest()


def treePath(toolingRoot, pin):
  return toolingRoot / "recast" / pin["commit"][:12]


def treeFiles(root):
  """Every file of the tree's parts, as (path relative to the tree root in / form, absolute path), sorted by relative path."""
  files = []
  for part in treeParts:
    path = root / part
    files += [path] if path.is_file() else [entry for entry in path.rglob("*") if entry.is_file()]
  return sorted((file.relative_to(root).as_posix(), file) for file in files)


def treeDigest(root):
  """SHA-256 over the sorted lines '<path> <sha256 of contents>\\n' of the tree's files; GitHub does not promise stable zip bytes, so
  the extracted tree is what the pin verifies."""
  lines = "".join(f"{relative} {sha256Of(path.read_bytes())}\n" for relative, path in treeFiles(root))
  return sha256Of(lines.encode("ascii"))


def programFilesX86():
  # The MCP client leaves %ProgramFiles(x86)% out of the server's environment; the registry holds the folder it is set from.
  with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion") as key:
    return Path(winreg.QueryValueEx(key, "ProgramFilesDir (x86)")[0])


def runQuietly(arguments):
  return subprocess.run(arguments, capture_output=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)


def visualStudio():
  """The Visual Studio install the helper builds with: its folder, vcvars64.bat, the bundled cmake and ninja, and the default toolset's
  cl.exe (the one vcvars64 puts first on PATH)."""
  vswhere = programFilesX86() / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
  if not vswhere.is_file():
    raise ToolError(f"The Recast helper needs {visualStudioNeeds}; there is no {vswhere}")
  query = [str(vswhere), "-latest", "-products", "*", "-requires", *visualStudioComponents]

  def answer(*arguments):
    completed = runQuietly(query + list(arguments))
    if completed.returncode != 0:
      raise ToolError(f"vswhere {' '.join(arguments)} failed with exit {completed.returncode}: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

  installations = answer("-property", "installationPath")
  if len(installations) != 1:
    raise ToolError(f"The Recast helper needs {visualStudioNeeds}; vswhere finds no install with both")
  installation = Path(installations[0])
  found = {}
  for tool in ("cmake.exe", "ninja.exe"):
    paths = answer("-find", rf"**\{tool}")
    if len(paths) != 1:
      raise ToolError(f"The Recast helper needs the {tool} that {visualStudioNeeds} bundles; vswhere finds {paths or 'none'} in {installation}")
    found[tool] = Path(paths[0])
  buildFolder = installation / "VC" / "Auxiliary" / "Build"
  versionFile = buildFolder / "Microsoft.VCToolsVersion.default.txt"
  if not versionFile.is_file():
    raise ToolError(f"The Recast helper needs {visualStudioNeeds}; {versionFile} is missing")
  toolset = versionFile.read_text(encoding="utf-8").strip()
  compiler = installation / "VC" / "Tools" / "MSVC" / toolset / "bin" / "Hostx64" / "x64" / "cl.exe"
  vcvars = buildFolder / "vcvars64.bat"
  for path in (compiler, vcvars):
    if not path.is_file():
      raise ToolError(f"The Recast helper needs {visualStudioNeeds}; {path} is missing")
  return {"installation": installation, "vcvars": vcvars, "cmake": found["cmake.exe"], "ninja": found["ninja.exe"], "compiler": compiler}


def compilerLine(compiler):
  """cl.exe's version line, which it writes first when run with no arguments."""
  completed = runQuietly([str(compiler)])
  lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
  if not lines or "Compiler Version" not in lines[0]:
    raise ToolError(f"{compiler} did not report its version: {completed.stderr.strip()[:300]}")
  return lines[0]


@functools.cache
def toolchain():
  """visualStudio() and its compiler's version line, found once per process: vswhere and cl take over a second and a half, and every
  helper run checks its build's fingerprint. A Visual Studio update shows as stale once the server restarts."""
  studio = visualStudio()
  return studio, compilerLine(studio["compiler"])


def helperFingerprint(pin, compiler):
  """SHA-256 of the helper sources (every file in recastHelper/, CMakeLists.txt among them), the tree digest, cl's version line, and the
  configure arguments; a change to any of them makes a new build."""
  sources = sorted(path for path in sourcesPath.iterdir() if path.is_file())
  lines = [f"source {path.name} {sha256Of(path.read_bytes())}" for path in sources]
  lines += [f"tree {pin['treeSha256']}", f"compiler {compiler}", f"configure {' '.join(configureArguments)}"]
  return sha256Of(("\n".join(lines) + "\n").encode("utf-8"))


def helpersRoot(toolingRoot):
  return toolingRoot / "recastHelper"


def installPath(toolingRoot, fingerprint):
  return helpersRoot(toolingRoot) / fingerprint[:fingerprintFolderLength]


def readBuildRecord(folder):
  recordPath = folder / buildRecordName
  if not recordPath.is_file() or not (folder / executableName).is_file():
    return None
  return json.loads(recordPath.read_text(encoding="utf-8"))


def helperStatus(toolingRoot, pin):
  """missing (never built here, or no Visual Studio to tell), stale (built only from other sources, tree, compiler, or arguments than now),
  or built; with the fingerprint, the commit, and the compiler."""
  status = {"commit": pin["commit"]}
  try:
    _, compiler = toolchain()
  except ToolError as error:
    return status | {"state": "missing", "problem": str(error)}
  fingerprint = helperFingerprint(pin, compiler)
  folder = installPath(toolingRoot, fingerprint)
  status |= {"fingerprint": fingerprint, "compiler": compiler, "executablePath": str(folder / executableName)}
  record = readBuildRecord(folder)
  if record is not None and record["fingerprint"] == fingerprint:
    return status | {"state": "built", "compileSeconds": record["compileSeconds"]}
  root = helpersRoot(toolingRoot)
  otherBuilds = [entry for entry in root.iterdir() if entry.is_dir() and readBuildRecord(entry) is not None] if root.is_dir() else []
  return status | {"state": "stale" if otherBuilds else "missing"}


def requireHelper(toolingRoot):
  status = helperStatus(toolingRoot, toolingManifest.loadManifest()["recast"])
  if status["state"] != "built":
    problem = f": {status['problem']}" if "problem" in status else ""
    raise ToolError(f"The Recast helper is {status['state']}{problem}; run syncTooling to build it")
  return Path(status["executablePath"])


def runHelper(toolingRoot, mode, inputBytes, reportProgress):
  """Run the built helper on one input; returns (its output file's bytes, or None for inspect, and its report). Its stderr progress
  lines are relayed; its error line, or anything else it says, becomes a ToolError."""
  executable = requireHelper(toolingRoot)
  helpersRoot(toolingRoot).mkdir(parents=True, exist_ok=True)
  started = time.perf_counter()
  with tempfile.TemporaryDirectory(dir=helpersRoot(toolingRoot), prefix="run-") as runFolder:
    folder = Path(runFolder)
    inputPath, outputPath, reportPath, stdoutPath = (folder / name for name in ("input.bin", "output.bin", "report.json", "stdout.txt"))
    inputPath.write_bytes(inputBytes)
    arguments = [str(executable), mode, str(inputPath), str(outputPath) if mode == "nav" else "-", str(reportPath)]
    errorLine = None
    unexpected = []
    with stdoutPath.open("wb") as stdout:
      process = subprocess.Popen(arguments, stdout=stdout, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
      for rawLine in process.stderr:
        line = rawLine.decode("utf-8", errors="replace").strip()
        try:
          message = json.loads(line)
        except json.JSONDecodeError:
          unexpected.append(line)
          continue
        if "error" in message:
          errorLine = message["error"]
        elif "step" in message:
          reportProgress(message["done"], message["of"], message["step"])
        else:
          unexpected.append(line)
      exitCode = process.wait()
    if exitCode != 0:
      raise ToolError(f"recastHelper {mode}: {errorLine if errorLine is not None else f'exit {exitCode}'}" + (f" ({'; '.join(unexpected)})" if unexpected else ""))
    said = stdoutPath.read_bytes()
    if errorLine is not None or unexpected or said:
      raise ToolError(f"recastHelper {mode} exited 0 but said {errorLine!r}, {unexpected}, and {said[:300]!r} on stdout")
    report = json.loads(reportPath.read_text(encoding="utf-8"))
    output = outputPath.read_bytes() if mode == "nav" else None
  logger.info("recastHelper %s ran in %.2f s", mode, time.perf_counter() - started)
  return output, report


def navInput(recastTriangles, bounds, volumes, settings, threads):
  """The nav mode's input: map_edit's settings, the bounds (minimum and maximum corners, Recast axes), the thread count, the collidable
  triangles (Recast axes, float32 [n, 3, 3]), and the volumes (four corners in Recast axes, their height range, and their nav area), in
  .wtr order."""
  triangles = numpy.ascontiguousarray(recastTriangles, dtype="<f4")
  if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
    raise ValueError(f"collidable triangles must be [n, 3, 3], got {list(triangles.shape)}")
  parts = [navMagic, struct.pack("<12f", *(settings[name] for name in navSettingFloats)), struct.pack("<3i", *(settings[name] for name in navSettingIntegers))]
  parts += [struct.pack("<6f", *bounds[0], *bounds[1])]
  parts += [struct.pack("<I", threads), struct.pack("<I", len(triangles)), triangles.tobytes(), struct.pack("<I", len(volumes))]
  for volume in volumes:
    parts.append(struct.pack("<12f2fI", *numpy.asarray(volume["corners"], dtype=numpy.float32).ravel(), volume["low"], volume["high"], volume["area"]))
  return b"".join(parts)


def inspectInput(payload, safePoint, targets, pathing):
  """The inspect mode's input: the nav payload as the .nav holds it inflated, the server's search limits and ground filter, the safe
  point (Recast axes, or None), and the probe targets (Recast axes)."""
  costs = [1.0] * detourAreaCount
  for area, cost in pathing["areaCosts"].items():
    costs[area] = cost
  excluded = 0
  for area in pathing["excludedAreas"]:
    excluded |= 1 << area
  parts = [inspectMagic, struct.pack("<I", len(payload)), payload, struct.pack("<2I", pathing["searchNodes"], pathing["pathPolygons"])]
  parts += [struct.pack("<3f", *pathing["nearestHalfExtents"]), struct.pack("<3f", *pathing["pathHalfExtents"]), struct.pack("<f", pathing["snapHeight"])]
  parts += [struct.pack("<2I", 0xFFFF ^ excluded, 0), struct.pack(f"<{detourAreaCount}f", *costs)]
  parts += [struct.pack("<I3f", safePoint is not None, *(safePoint if safePoint is not None else (0.0, 0.0, 0.0))), struct.pack("<I", len(targets))]
  parts += [struct.pack("<3f", *target) for target in targets]
  return b"".join(parts)
