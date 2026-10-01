"""Blender-side entry point: a blocking, single-threaded command loop on Blender's main thread. Run with blender --python."""
import hmac
import os
import socket
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import addon_utils

import bridgeCommands
import bridgeProtocol


def enableExtensions():
  extensionIDs = [extensionID for extensionID in os.environ[bridgeProtocol.extensionsVariable].split(",") if extensionID]
  for extensionID in extensionIDs:
    moduleName = f"bl_ext.user_default.{extensionID}"

    def raiseError(error):
      raise RuntimeError(f"extension {extensionID} failed to enable") from error

    addon_utils.enable(moduleName, default_set=True, handle_error=raiseError)
    if not addon_utils.check(moduleName)[1]:
      raise RuntimeError(f"extension {extensionID} did not enable")


def serve(connection, token):
  while True:
    try:
      request = bridgeProtocol.receiveFrame(connection)
    except bridgeProtocol.ConnectionClosed:
      return
    if not hmac.compare_digest(request.get("token", ""), token):
      raise RuntimeError("request with a wrong token")
    if request["command"] == "shutdown":
      bridgeProtocol.sendFrame(connection, {"ok": True, "result": None})
      return
    try:
      result = bridgeCommands.dispatch(request["command"], request["arguments"])
      response = {"ok": True, "result": result}
    except Exception as error:
      response = {"ok": False, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    bridgeProtocol.sendFrame(connection, response)


def main():
  token = os.environ[bridgeProtocol.tokenVariable]
  enableExtensions()
  listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  listener.bind(("127.0.0.1", 0))
  listener.listen(1)
  print(f"{bridgeProtocol.portMarker} {listener.getsockname()[1]}", flush=True)
  connection, _ = listener.accept()
  listener.close()
  with connection:
    serve(connection, token)


main()
