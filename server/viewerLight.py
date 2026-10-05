"""The scene light the RoF2 client adds for the character a view belongs to (eqgame.exe 0x4942b0): the special ambient, the floor of
the character's vision times one less the character's own share of scene light. The client takes that share from the player's actor
(0x494555-0x494579, its vtable 0xf0), which finds it by the same drop as a placed object's (CActor 0x1003a4c0, loadTimeLight), and
moves toward it by 0.01 a frame (0x9c4be4); each channel is truncated to a byte (0x494725-0x4947c6) before the scene graph takes it
(vtable 0xcc). MQPeridotEmu's dumps hold the player's share: 0.1992 on Grimling Forest's cave floor, 0.9961 on the Plane of Knowledge's
cobbles."""
import math
import struct

import numpy

import eqArchive
import eqModels
import eqRaces
import eqSkeletons
import eqSky

# A view's character is drawn as the calibration character: a human (HUM, globalhum_chr.s3d) of height 6.
viewerModel = "HUM"
viewerHeight = 6


def viewerBody(clientRoot, cacheRoot, newEngineZone):
  """The view's character's model as the client stands it: its skeleton's bounding sphere and how high its origin stands over its feet."""
  definition = eqModels.resolveModel(clientRoot, cacheRoot, viewerModel, None)
  worldFile, actor = eqModels.wldActor(eqArchive.EQArchive(clientRoot / definition["archive"]), definition)
  skeleton = worldFile.fragment(struct.unpack_from("<i", eqModels.actorReferences(worldFile, actor)[0].body, 4)[0], 0x10)
  scale = eqRaces.spawnScale(viewerModel, viewerHeight, newEngineZone)
  return {"sphere": eqSkeletons.boundingSphere(skeleton), "avatarHeight": eqRaces.avatarHeight(clientRoot, viewerModel, scale)}


def viewerShare(floors, feet, body):
  """The share of scene light the drop under the character standing on feet finds."""
  position = numpy.asarray(feet, dtype=numpy.float64) + numpy.array((0.0, 0.0, body["avatarHeight"]))
  return float(floors.share(position + body["sphere"]["center"], body["sphere"]["radius"]))


def specialAmbient(share):
  """The special ambient for a character of share, each channel the vision floor times one less the share, truncated to a byte."""
  level = math.trunc(255 * eqSky.ambientFloor * (1 - share)) / 255
  return [level, level, level]
