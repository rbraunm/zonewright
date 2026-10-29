"""How Blender is started: by the front, on the worker's request. The MCP SDK runs the worker in a Windows Job Object it terminates
with the worker, and the job forbids breakaway, so a Blender the worker spawned would die on every reload; the front outlives reloads,
spawns the command the worker asks for over a local socket, and the worker holds the process by pid (blenderBridge.AdoptedProcess)."""
import hmac
import os
import secrets
import socket
import subprocess

import anyio
from anyio.abc import SocketAttribute
from mcp.server.mcpserver.exceptions import ToolError

import bridgeProtocol

portVariable = "zonewrightLauncherPort"
tokenVariable = "zonewrightLauncherToken"
connectTimeoutSeconds = 30


async def receiveExactly(stream, byteCount):
  received = bytearray()
  while len(received) < byteCount:
    received.extend(await stream.receive(byteCount - len(received)))
  return bytes(received)


async def receiveFrame(stream):
  (payloadLength,) = bridgeProtocol.lengthHeader.unpack(await receiveExactly(stream, bridgeProtocol.lengthHeader.size))
  return bridgeProtocol.decodeFrame(await receiveExactly(stream, payloadLength))


async def sendFrame(stream, message):
  await stream.send(bridgeProtocol.encodeFrame(message))


class Launcher:
  """The front's side: spawns each command a worker asks for and keeps the process, which so outlives the worker."""

  def __init__(self):
    self.token = secrets.token_urlsafe(32)
    self.port = None
    self.processes = {}

  async def serve(self, taskGroup):
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    self.port = listener.extra(SocketAttribute.local_port)
    taskGroup.start_soon(listener.serve, self.handle)

  async def handle(self, stream):
    async with stream:
      request = await receiveFrame(stream)
      if not hmac.compare_digest(request.get("token", ""), self.token):
        raise RuntimeError("launch request with a wrong token")
      try:
        with open(request["outputPath"], "ab") as outputFile:
          process = subprocess.Popen(
            request["command"], stdin=subprocess.DEVNULL, stdout=outputFile, stderr=subprocess.STDOUT, env=request["environment"],
            creationflags=subprocess.CREATE_NO_WINDOW,
          )
      except OSError as error:
        await sendFrame(stream, {"ok": False, "error": f"{type(error).__name__}: {error}"})
        return
      self.processes[process.pid] = process
      await sendFrame(stream, {"ok": True, "pid": process.pid})

  def environment(self):
    return {portVariable: str(self.port), tokenVariable: self.token}

  def killSurvivors(self):
    """Processes still running once the worker is gone are orphans: nothing will shut them down."""
    return [process.pid for process in self.processes.values() if process.poll() is None and process.kill() is None]


def launch(command, environment, outputPath):
  """The worker's side: asks the front to start the command with its output appended to outputPath, and returns the pid."""
  port, token = os.environ.get(portVariable), os.environ.get(tokenVariable)
  if not port or not token:
    raise ToolError("zonewright runs without its front, which starts Blender for it: register server\\zonewrightFront.py (README), not zonewrightServer.py")
  try:
    with socket.create_connection(("127.0.0.1", int(port)), timeout=connectTimeoutSeconds) as connection:
      bridgeProtocol.sendFrame(connection, {"token": token, "command": command, "environment": environment, "outputPath": str(outputPath)})
      response = bridgeProtocol.receiveFrame(connection)
  except (OSError, bridgeProtocol.ConnectionClosed) as error:
    raise ToolError(f"zonewright's front did not answer the request to start Blender: {error}") from error
  if not response["ok"]:
    raise ToolError(f"zonewright's front could not start Blender: {response['error']}")
  return response["pid"]
