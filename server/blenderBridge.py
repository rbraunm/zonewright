"""Server side of the Blender bridge: holds the headless Blender process the front started for this server (launcher), or the one the
server before this one handed over (reloadZonewright), and its socket, so a server restart keeps the open scene."""
import ctypes
import ctypes.wintypes
import datetime
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import bridgeProtocol
import launcher
import machineProfile
import toolingLog
import toolingManifest
import toolingStatus

logger = logging.getLogger(__name__)
serverDirectory = Path(__file__).resolve().parent
bridgeMainPath = serverDirectory / "bridgeMain.py"
startupTimeoutSeconds = 180
shutdownTimeoutSeconds = 30
crashWaitSeconds = 30
adoptConnectTimeoutSeconds = 10
outputTailLines = 40
blenderLogStem = "blender"
# Commands that reach a clean, saved state still run on outdated bridge code, so unsaved work can be kept or dropped.
cleanStateCommands = ("getStatus", "saveFile", "newFile", "openFile")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
kernel32.OpenProcess.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
kernel32.GetExitCodeProcess.argtypes = (ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD))
kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD)
kernel32.TerminateProcess.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.UINT)
processSynchronize = 0x00100000
processQueryLimitedInformation = 0x1000
processTerminate = 0x0001
stillActive = 259
waitTimeout = 0x102
waitInfinite = 0xFFFFFFFF


def bridgeCodeFingerprint():
  """Size and modification time of every Blender-side source file; a change means the running Blender has stale code."""
  return {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in sorted(serverDirectory.glob("bridge*.py"))}


class AdoptedProcess:
  """A Blender process another process started (the front, or through it the server before this one), held through its pid as
  subprocess.Popen holds a child: pid, poll, wait, kill."""

  def __init__(self, pid):
    self.pid = pid
    self.handle = kernel32.OpenProcess(processSynchronize | processQueryLimitedInformation | processTerminate, False, pid)
    if not self.handle:
      raise OSError(f"process {pid} cannot be opened: {ctypes.FormatError(ctypes.get_last_error())}")

  def poll(self):
    exitCode = ctypes.wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(self.handle, ctypes.byref(exitCode)):
      raise OSError(f"process {self.pid} gives no exit code: {ctypes.FormatError(ctypes.get_last_error())}")
    return None if exitCode.value == stillActive else exitCode.value

  def wait(self, timeout=None):
    if kernel32.WaitForSingleObject(self.handle, waitInfinite if timeout is None else int(timeout * 1000)) == waitTimeout:
      raise subprocess.TimeoutExpired(f"blender pid {self.pid}", timeout)
    return self.poll()

  def kill(self):
    kernel32.TerminateProcess(self.handle, 1)


class BlenderBridge:
  def __init__(self, toolingRoot):
    self.toolingRoot = toolingRoot
    self.lock = threading.Lock()
    self.process = None
    self.connection = None
    self.port = None
    self.token = None
    self.codeFingerprint = None
    self.logPath = None
    self.logOffset = 0
    self.lastCrash = None

  def isRunning(self):
    return self.process is not None and self.process.poll() is None and self.connection is not None

  def blenderLogPath(self):
    """Blender's output goes to logs\\blender.<day>.log, appended, the old days' files pruned as the server's own logs are."""
    logDirectory = self.toolingRoot / "logs"
    logDirectory.mkdir(parents=True, exist_ok=True)
    toolingLog.pruneLogs(logDirectory, blenderLogStem, time.time() - toolingLog.logRetentionDays * 86400)
    return logDirectory / f"{blenderLogStem}.{datetime.date.today().isoformat()}.log"

  def output(self):
    """Every line this Blender wrote."""
    if self.logPath is None or not self.logPath.exists():
      return []
    with open(self.logPath, "rb") as logFile:
      logFile.seek(self.logOffset)
      return logFile.read().decode("utf-8", errors="replace").splitlines()

  def outputTail(self):
    return self.output()[-outputTailLines:]

  def waitForPort(self):
    deadline = time.monotonic() + startupTimeoutSeconds
    while True:
      for line in self.output():
        if line.startswith(bridgeProtocol.portMarker + " "):
          return int(line.split()[1])
      if self.process.poll() is not None or time.monotonic() >= deadline:
        return None
      time.sleep(0.1)

  def start(self, reportProgress):
    manifest = toolingManifest.loadManifest()
    blenderVersion = manifest["blender"]["version"]
    blenderStatus = toolingStatus.getBlenderStatus(self.toolingRoot, blenderVersion)
    if blenderStatus["state"] != "installed":
      raise ToolError(f"Blender {blenderVersion} is {blenderStatus['state']}; run syncTooling first")
    profile = machineProfile.loadMachineProfile(self.toolingRoot)
    reportProgress(0, None, "starting Blender")
    self.token = secrets.token_urlsafe(32)
    workers = machineProfile.workerCount()
    environment = dict(os.environ) | {
      bridgeProtocol.tokenVariable: self.token,
      bridgeProtocol.extensionsVariable: ",".join(sorted(manifest["extensions"])),
      bridgeProtocol.shaderWorkersVariable: str(workers),
    }
    self.codeFingerprint = bridgeCodeFingerprint()
    self.logPath = self.blenderLogPath()
    self.logOffset = self.logPath.stat().st_size if self.logPath.exists() else 0
    command = [
      blenderStatus["executablePath"], "--background", "--factory-startup",
      "--gpu-backend", profile["gpuBackend"], "--threads", str(workers),
      "--python", str(bridgeMainPath),
    ]
    logger.info("starting bridge through the front: %s", command)
    self.process = AdoptedProcess(launcher.launch(command, environment, self.logPath))
    port = self.waitForPort()
    if port is None:
      exitCode = self.process.poll()
      self.process.kill()
      self.process = None
      raise ToolError(f"Blender did not start the bridge (exit code {exitCode}). Last output:\n" + "\n".join(self.outputTail()))
    self.port = port
    self.connection = socket.create_connection(("127.0.0.1", port))
    self.lastCrash = None
    logger.info("bridge running: pid %s port %s backend %s threads %s", self.process.pid, port, profile["gpuBackend"], workers)

  def handOver(self):
    """Hands the running Blender to the next server: closes this server's connection, which the bridge outlives for handoverWaitSeconds
    (bridgeMain), and returns what that server's adopt needs; None when no Blender runs."""
    with self.lock:
      if not self.isRunning():
        return None
      record = {
        "pid": self.process.pid, "port": self.port, "token": self.token, "codeFingerprint": self.codeFingerprint,
        "logPath": str(self.logPath), "logOffset": self.logOffset,
      }
      self.connection.close()
      self.connection = None
      self.process = None
      logger.info("bridge handed over: pid %s port %s", record["pid"], record["port"])
      return record

  def adopt(self, record):
    """Takes over the Blender the server before this one handed over, and returns its status."""
    with self.lock:
      if self.isRunning():
        raise ToolError(f"this server already runs Blender (pid {self.process.pid}); nothing to adopt")
      pid = record["pid"]
      process = AdoptedProcess(pid)
      exitCode = process.poll()
      if exitCode is not None:
        raise ToolError(f"the handed-over Blender (pid {pid}) has exited with code {exitCode}; the next call starts a fresh one")
      try:
        connection = socket.create_connection(("127.0.0.1", record["port"]), timeout=adoptConnectTimeoutSeconds)
      except OSError as error:
        raise ToolError(f"the handed-over Blender (pid {pid}) does not answer on port {record['port']}: {error}; the next call starts a fresh one") from error
      connection.settimeout(None)
      self.process = process
      self.connection = connection
      self.port = record["port"]
      self.token = record["token"]
      self.codeFingerprint = {name: tuple(signature) for name, signature in record["codeFingerprint"].items()}
      self.logPath = Path(record["logPath"])
      self.logOffset = record["logOffset"]
      self.lastCrash = None
      status = self.exchangeOrRaise("getStatus", {})
      logger.info("bridge adopted: pid %s port %s file %s", pid, self.port, status["filePath"])
      return {"pid": pid} | status

  def handleCrash(self, command):
    try:
      exitCode = self.process.wait(crashWaitSeconds)
    except subprocess.TimeoutExpired:
      self.process.kill()
      exitCode = self.process.wait(crashWaitSeconds)
    self.lastCrash = {"exitCode": exitCode, "command": command, "lastOutput": self.outputTail()}
    self.connection.close()
    self.connection = None
    self.process = None
    logger.error("Blender exited with code %s during %s", exitCode, command)
    raise ToolError(f"Blender exited with code {exitCode} during {command}; the next call starts a fresh Blender. Last output:\n" + "\n".join(self.lastCrash["lastOutput"]))

  def exchange(self, command, arguments):
    try:
      bridgeProtocol.sendFrame(self.connection, {"token": self.token, "command": command, "arguments": arguments})
      return bridgeProtocol.receiveFrame(self.connection)
    except (bridgeProtocol.ConnectionClosed, ConnectionError):
      self.handleCrash(command)

  def exchangeOrRaise(self, command, arguments):
    response = self.exchange(command, arguments)
    if not response["ok"]:
      raise ToolError(response["error"])
    return response["result"]

  def shutdownProcess(self):
    self.exchange("shutdown", {})
    try:
      self.process.wait(shutdownTimeoutSeconds)
    except subprocess.TimeoutExpired:
      logger.warning("Blender did not exit within %s seconds of shutdown; killing it", shutdownTimeoutSeconds)
      self.process.kill()
      self.process.wait(shutdownTimeoutSeconds)
    self.connection.close()
    self.connection = None
    self.process = None

  def reloadForChangedCode(self, command, reportProgress):
    """Restart Blender on the new bridge code and reopen the open file, refusing while unsaved changes would be lost."""
    changed = sorted(name for name, signature in bridgeCodeFingerprint().items() if self.codeFingerprint.get(name) != signature)
    status = self.exchangeOrRaise("getStatus", {})
    if status["unsavedChanges"]:
      raise ToolError(f"Blender bridge code changed on disk ({', '.join(changed)}) and the open file has unsaved changes; save it (saveFile) or discard them (newFile or openFile with discardUnsavedChanges), then call {command} again to load the new code")
    logger.info("bridge code changed (%s); restarting Blender and reopening %s", changed, status["filePath"])
    self.shutdownProcess()
    self.start(reportProgress)
    if status["filePath"] is not None:
      self.exchangeOrRaise("openFile", {"path": status["filePath"], "discardUnsavedChanges": False})

  def call(self, command, arguments, reportProgress):
    with self.lock:
      if self.process is not None and self.process.poll() is not None:
        self.handleCrash(command)
      if self.isRunning() and bridgeCodeFingerprint() != self.codeFingerprint and command not in cleanStateCommands:
        self.reloadForChangedCode(command, reportProgress)
      # A Blender that died since the check above is reported by the exchange as a crash, never replaced in silence.
      if self.process is None:
        self.start(reportProgress)
      return self.exchangeOrRaise(command, arguments)

  def stop(self):
    with self.lock:
      if self.isRunning():
        self.shutdownProcess()

  def stopForSync(self):
    if not self.isRunning():
      return
    bridgeStatus = self.call("getStatus", {}, lambda *arguments: None)
    if bridgeStatus["unsavedChanges"]:
      raise ToolError(f"The bridge's open file ({bridgeStatus['filePath']}) has unsaved changes; save it or open another file with discardUnsavedChanges before syncing")
    self.stop()

  def status(self):
    if self.isRunning():
      return {"state": "running", "pid": self.process.pid, "codeCurrent": bridgeCodeFingerprint() == self.codeFingerprint} | self.call("getStatus", {}, lambda *arguments: None)
    if self.lastCrash is not None:
      return {"state": "crashed"} | self.lastCrash
    return {"state": "notStarted"}
