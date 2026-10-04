"""Per-machine performance profile: CPU worker count and the fastest hardware GPU backend for Blender, discovered during setup."""
import json
import logging
import math
import os
import platform
import subprocess
import time
import winreg
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import bridgeProtocol
import toolingManifest
import toolingStatus

logger = logging.getLogger(__name__)
profileFormat = 1
cpuShare = 0.8
gpuBackends = ("vulkan", "opengl")
benchmarkScriptPath = Path(__file__).resolve().parent / "machineBenchmark.py"
benchmarkMarker = "ZONEWRIGHT_BENCHMARK"
benchmarkTimeoutSeconds = 600
softwareRendererMarkers = ("llvmpipe", "softpipe", "swiftshader", "basic render")


def workerCount():
  """Workers for parallel CPU work: 80% of the logical processors."""
  return max(1, math.floor(os.cpu_count() * cpuShare))


def profilePath(toolingRoot):
  return toolingRoot / "machineProfile.json"


def processorIdentity():
  # platform.processor() asks WMI, which times out under load, and then falls back to PROCESSOR_IDENTIFIER, a variable the MCP client
  # leaves out of the server's environment: the fingerprint would change with the machine's load.
  with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
    return f"{winreg.QueryValueEx(key, 'Identifier')[0]}, {winreg.QueryValueEx(key, 'VendorIdentifier')[0]}"


def hardwareFingerprint():
  return {"logicalProcessors": os.cpu_count(), "processor": processorIdentity(), "machine": platform.machine()}


def benchmarkBackend(executablePath, backend):
  command = [str(executablePath), "--background", "--factory-startup", "--gpu-backend", backend, "--threads", str(workerCount()), "--python", str(benchmarkScriptPath)]
  environment = dict(os.environ) | {bridgeProtocol.shaderWorkersVariable: str(workerCount())}
  start = time.perf_counter()
  completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", env=environment, timeout=benchmarkTimeoutSeconds, creationflags=subprocess.CREATE_NO_WINDOW)
  processSeconds = round(time.perf_counter() - start, 2)
  logger.info("benchmark %s exit %s output: %s", backend, completed.returncode, completed.stdout.strip()[-2000:])
  reportLines = [line for line in completed.stdout.splitlines() if line.startswith(benchmarkMarker + " ")]
  if completed.returncode != 0 or len(reportLines) != 1:
    return {"backend": backend, "usable": False, "reason": f"exit {completed.returncode}: {completed.stdout.strip()[-300:]} {completed.stderr.strip()[-300:]}", "processSeconds": processSeconds}
  report = json.loads(reportLines[0][len(benchmarkMarker) + 1:])
  isSoftware = report["deviceType"] == "SOFTWARE" or any(marker in report["renderer"].lower() for marker in softwareRendererMarkers)
  return {
    "backend": backend,
    "usable": not isSoftware,
    "reason": f"software renderer {report['renderer']}" if isSoftware else None,
    "renderer": report["renderer"],
    "vendor": report["vendor"],
    "deviceType": report["deviceType"],
    "renderSeconds": report["renderSeconds"],
    "totalRenderSeconds": round(sum(report["renderSeconds"]), 3),
    "processSeconds": processSeconds,
  }


def profileMachine(toolingRoot, reportProgress):
  blenderVersion = toolingManifest.loadManifest()["blender"]["version"]
  blenderStatus = toolingStatus.getBlenderStatus(toolingRoot, blenderVersion)
  if blenderStatus["state"] != "installed":
    raise ToolError(f"Blender {blenderVersion} is {blenderStatus['state']}; profiling needs it installed")
  benchmarks = []
  for index, backend in enumerate(gpuBackends):
    reportProgress(index, len(gpuBackends), f"benchmarking the {backend} GPU backend")
    benchmarks.append(benchmarkBackend(blenderStatus["executablePath"], backend))
  usable = [benchmark for benchmark in benchmarks if benchmark["usable"]]
  if not usable:
    raise ToolError(f"No hardware GPU backend works with Blender {blenderVersion} on this machine: {benchmarks}")
  fastest = min(usable, key=lambda benchmark: benchmark["totalRenderSeconds"])
  profile = {
    "profileFormat": profileFormat,
    "blenderVersion": blenderVersion,
    "hardware": hardwareFingerprint(),
    "gpuBackend": fastest["backend"],
    "gpu": fastest["renderer"],
    "benchmarks": benchmarks,
  }
  profilePath(toolingRoot).write_text(json.dumps(profile, indent=2), encoding="utf-8")
  logger.info("machine profile: %s on %s, %s workers", profile["gpuBackend"], profile["gpu"], workerCount())
  return profile


def profileProblems(toolingRoot):
  path = profilePath(toolingRoot)
  if not path.is_file():
    return ["no machine profile"]
  profile = json.loads(path.read_text(encoding="utf-8"))
  problems = []
  if profile.get("profileFormat") != profileFormat:
    problems.append(f"profile format {profile.get('profileFormat')} is not {profileFormat}")
  pinnedVersion = toolingManifest.loadManifest()["blender"]["version"]
  if profile.get("blenderVersion") != pinnedVersion:
    problems.append(f"profiled Blender {profile.get('blenderVersion')}, pinned {pinnedVersion}")
  if profile.get("hardware") != hardwareFingerprint():
    problems.append("hardware changed since profiling")
  return problems


def loadMachineProfile(toolingRoot):
  problems = profileProblems(toolingRoot)
  if problems:
    raise ToolError(f"Machine profile is missing or stale ({'; '.join(problems)}); run syncTooling")
  return json.loads(profilePath(toolingRoot).read_text(encoding="utf-8"))


def profileStatus(toolingRoot):
  problems = profileProblems(toolingRoot)
  if problems:
    return {"state": "stale" if profilePath(toolingRoot).is_file() else "missing", "problems": problems, "workers": workerCount()}
  profile = loadMachineProfile(toolingRoot)
  return {"state": "current", "gpuBackend": profile["gpuBackend"], "gpu": profile["gpu"], "workers": workerCount()}
