"""The client's particle emitter definitions (.edd files), read as EQGraphicsDX9.dll reads them: "EDD\\0", the version "110\\0", then
416-byte records, record i being definition index i (docs/clientRendering.md, Particle emitters). An environment emitter's
EmitterDefIdx indexes EnvironmentEmittersNew.edd. Only the fields the client's particle code was traced reading are named."""
import struct
from pathlib import Path

magic = b"EDD\x00"
version = b"110\x00"
headerBytes = 8
recordBytes = 0x1A0
environmentFileName = "environmentemittersnew.edd"
# The DLL composes a definition's texture path with each folder in turn (0x10071d18).
textureFolders = ("SpellEffects", "EnvEmitterEffects", "ActorEffects")
nameBytes = 0x40
textureOffset, textureBytes = 0x40, 0x20

fields = (
  ("noDepthWrite", 0x64, "i"), ("additive", 0x68, "i"), ("billboard", 0x70, "i"), ("followsEmitter", 0x74, "i"),
  ("duration", 0x78, "f"), ("particleLife", 0x7C, "f"), ("burst", 0x80, "i"), ("perEvent", 0x84, "i"), ("eventRate", 0x88, "f"),
  ("startDelay", 0x8C, "f"), ("alphaFadeIn", 0x90, "f"), ("alphaFadeOut", 0x94, "f"), ("sizeFadeIn", 0x98, "f"),
  ("sizeFadeOut", 0x9C, "f"), ("lodDistance", 0xA0, "f"), ("maximumAlpha", 0xA4, "f"), ("shape", 0xA8, "i"),
  ("shapeRadius", 0xAC, "f"), ("shapeRadiusY", 0xB0, "f"), ("shapeHeight", 0xB4, "f"), ("offsetA", 0xB8, "f"), ("offsetB", 0xBC, "f"),
  ("offsetC", 0xC0, "f"), ("tiltB", 0xC4, "f"), ("tiltC", 0xC8, "f"), ("widthMinimum", 0xCC, "f"), ("depthBias", 0xD0, "f"),
  ("startRed", 0xD4, "i"), ("startGreen", 0xD8, "i"), ("startBlue", 0xDC, "i"), ("endRed", 0xE0, "i"), ("endGreen", 0xE4, "i"),
  ("endBlue", 0xE8, "i"),
  ("velocityAMinimum", 0xEC, "f"), ("velocityAMaximum", 0xF0, "f"), ("accelerationA", 0xF4, "f"),
  ("velocityBMinimum", 0xF8, "f"), ("velocityBMaximum", 0xFC, "f"), ("accelerationB", 0x100, "f"),
  ("velocityCMinimum", 0x104, "f"), ("velocityCMaximum", 0x108, "f"), ("accelerationC", 0x10C, "f"),
  ("velocityOutMinimum", 0x110, "f"), ("velocityOutMaximum", 0x114, "f"), ("accelerationOut", 0x118, "f"),
  ("swirlMinimum", 0x11C, "f"), ("swirlMaximum", 0x120, "f"), ("swirlAcceleration", 0x124, "f"),
  ("gravity", 0x128, "f"), ("driftX", 0x12C, "f"), ("frames", 0x130, "i"), ("framesPerSecond", 0x134, "f"), ("spinMinimum", 0x138, "f"),
  ("capacity", 0x144, "i"), ("sizeScale", 0x16C, "f"), ("randomRotation", 0x174, "i"),
  ("velocityAligned", 0x178, "i"), ("heightMinimum", 0x17C, "f"), ("heightMaximum", 0x180, "f"), ("widthMaximum", 0x184, "f"),
  ("spinMaximum", 0x188, "f"), ("linkedSize", 0x18C, "i"), ("heightFadeOut", 0x190, "f"), ("widthFadeOut", 0x194, "f"),
  ("crossesAxis", 0x198, "i"), ("emitterScaled", 0x19C, "i"),
)


def cString(raw):
  return raw.split(b"\x00", 1)[0].decode("latin-1")


def parseDefinitions(data, sourceName):
  """Every record of an .edd file as {index, name, texture, and the named fields}; refuses a file the client would not read as
  version 110."""
  if data[:4] != magic:
    raise ValueError(f"{sourceName}: not an emitter definition file (no EDD magic)")
  if data[4:8] != version:
    raise ValueError(f"{sourceName}: version {cString(data[4:8])!r}; only version 110 is read, the version this client's files are")
  if (len(data) - headerBytes) % recordBytes:
    raise ValueError(f"{sourceName}: {len(data) - headerBytes} bytes after the header are not whole {recordBytes}-byte records")
  definitions = []
  for index in range((len(data) - headerBytes) // recordBytes):
    record = data[headerBytes + index * recordBytes:headerBytes + (index + 1) * recordBytes]
    definition = {"index": index, "name": cString(record[:nameBytes]), "texture": cString(record[textureOffset:textureOffset + textureBytes])}
    for name, offset, kind in fields:
      definition[name] = struct.unpack_from("<i" if kind == "i" else "<f", record, offset)[0]
    definitions.append(definition)
  return definitions


def findFile(folder, name):
  """The file in folder whose name matches ignoring case, as the client's file system finds it, or None."""
  if not folder.is_dir():
    return None
  matches = [path for path in folder.iterdir() if path.name.lower() == name.lower()]
  if len(matches) > 1:
    raise ValueError(f"{folder} holds {len(matches)} files named {name} in different cases")
  return matches[0] if matches else None


def environmentDefinitionsPath(clientRoot):
  path = findFile(Path(clientRoot), environmentFileName)
  if path is None:
    raise ValueError(f"The client at {clientRoot} has no {environmentFileName}")
  return path


def texturePath(clientRoot, textureName):
  """Where the client finds a definition's texture, searching its effect folders in order, or None."""
  for folder in textureFolders:
    found = findFile(Path(clientRoot) / folder, textureName)
    if found is not None:
      return found
  return None
