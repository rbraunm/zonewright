"""Server side of the Blender bridge: owns the headless Blender process and its socket."""
import collections
import logging
import os
import secrets
import socket
import subprocess
import threading
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import bridgeProtocol
import toolingManifest
import toolingStatus

logger = logging.getLogger(__name__)
bridgeMainPath = Path(__file__).resolve().parent / "bridgeMain.py"
startupTimeoutSeconds = 180
shutdownTimeoutSeconds = 30
crashWaitSeconds = 30
outputTailLines = 40


class BlenderBridge:
  def __init__(self, toolingRoot):
    self.toolingRoot = toolingRoot
    self.lock = threading.Lock()
    self.process = None
    self.connection = None
    self.token = None
    self.outputTail = collections.deque(maxlen=outputTailLines)
    self.lastCrash = None

  def isRunning(self):
    return self.process is not None and self.process.poll() is None and self.connection is not None

  def readOutput(self, process, portFound, portHolder):
    for line in process.stdout:
      line = line.rstrip()
      logger.info("blender: %s", line)
      self.outputTail.append(line)
      if line.startswith(bridgeProtocol.portMarker + " "):
        portHolder.append(int(line.split()[1]))
        portFound.set()
    portFound.set()

  def start(self, reportProgress):
    manifest = toolingManifest.loadManifest()
    blenderVersion = manifest["blender"]["version"]
    blenderStatus = toolingStatus.getBlenderStatus(self.toolingRoot, blenderVersion)
    if blenderStatus["state"] != "installed":
      raise ToolError(f"Blender {blenderVersion} is {blenderStatus['state']}; run syncTooling first")
    reportProgress(0, None, "starting Blender")
    self.token = secrets.token_urlsafe(32)
    environment = dict(os.environ) | {
      bridgeProtocol.tokenVariable: self.token,
      bridgeProtocol.extensionsVariable: ",".join(sorted(manifest["extensions"])),
    }
    self.outputTail.clear()
    command = [blenderStatus["executablePath"], "--background", "--factory-startup", "--python", str(bridgeMainPath)]
    logger.info("starting bridge: %s", command)
    self.process = subprocess.Popen(
      command,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      encoding="utf-8",
      errors="replace",
      env=environment,
      creationflags=subprocess.CREATE_NO_WINDOW,
    )
    portFound = threading.Event()
    portHolder = []
    threading.Thread(target=self.readOutput, args=(self.process, portFound, portHolder), daemon=True).start()
    if not portFound.wait(startupTimeoutSeconds) or not portHolder:
      exitCode = self.process.poll()
      self.process.kill()
      self.process = None
      raise ToolError(f"Blender did not start the bridge (exit code {exitCode}). Last output:\n" + "\n".join(self.outputTail))
    self.connection = socket.create_connection(("127.0.0.1", portHolder[0]))
    self.lastCrash = None
    logger.info("bridge running: pid %s port %s", self.process.pid, portHolder[0])

  def handleCrash(self, command):
    try:
      exitCode = self.process.wait(crashWaitSeconds)
    except subprocess.TimeoutExpired:
      self.process.kill()
      exitCode = self.process.wait(crashWaitSeconds)
    self.lastCrash = {"exitCode": exitCode, "command": command, "lastOutput": list(self.outputTail)}
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

  def call(self, command, arguments, reportProgress):
    with self.lock:
      if self.process is not None and self.process.poll() is not None:
        self.handleCrash(command)
      if not self.isRunning():
        self.start(reportProgress)
      response = self.exchange(command, arguments)
    if not response["ok"]:
      raise ToolError(f"{response['error']}\n\n{response['traceback']}")
    return response["result"]

  def stop(self):
    with self.lock:
      if not self.isRunning():
        return
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

  def stopForSync(self):
    if not self.isRunning():
      return
    bridgeStatus = self.call("getStatus", {}, lambda *arguments: None)
    if bridgeStatus["unsavedChanges"]:
      raise ToolError(f"The bridge's open file ({bridgeStatus['filePath']}) has unsaved changes; save it or open another file with discardUnsavedChanges before syncing")
    self.stop()

  def status(self):
    if self.isRunning():
      return {"state": "running", "pid": self.process.pid} | self.call("getStatus", {}, lambda *arguments: None)
    if self.lastCrash is not None:
      return {"state": "crashed"} | self.lastCrash
    return {"state": "notStarted"}
