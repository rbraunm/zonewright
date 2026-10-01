"""Frames shared by the server and the Blender bridge: a 4-byte big-endian length, then UTF-8 JSON. Runs under both Pythons."""
import json
import struct

lengthHeader = struct.Struct(">I")
maximumFrameBytes = 64 * 1024 * 1024
portMarker = "ZONEWRIGHT_BRIDGE_PORT"
tokenVariable = "ZONEWRIGHT_BRIDGE_TOKEN"
extensionsVariable = "ZONEWRIGHT_EXTENSIONS"


class ConnectionClosed(Exception):
  pass


def receiveExactly(connection, byteCount):
  received = bytearray()
  while len(received) < byteCount:
    chunk = connection.recv(byteCount - len(received))
    if not chunk:
      raise ConnectionClosed(f"connection closed after {len(received)} of {byteCount} bytes")
    received.extend(chunk)
  return bytes(received)


def sendFrame(connection, message):
  payload = json.dumps(message).encode("utf-8")
  if len(payload) > maximumFrameBytes:
    raise ValueError(f"frame of {len(payload)} bytes exceeds {maximumFrameBytes}")
  connection.sendall(lengthHeader.pack(len(payload)) + payload)


def receiveFrame(connection):
  (payloadLength,) = lengthHeader.unpack(receiveExactly(connection, lengthHeader.size))
  if payloadLength > maximumFrameBytes:
    raise ValueError(f"frame of {payloadLength} bytes exceeds {maximumFrameBytes}")
  return json.loads(receiveExactly(connection, payloadLength).decode("utf-8"))
