"""What the player's lane is computed from, collected in the open scene: the collision the zone ships (bridgeExport.collisionTriangles),
the swim boxes and zone lines as they stand with each line's target, every entry stored and derived (bridgeEntries) with the footing
under each place players arrive, the regions with their access, the underworld, and the pools and rivers whose swimming is undecided;
which sights pass clear of that collision, what that collision holds where a trace stops, the ground under a lane view's eyes, and points
on an object's surface for a lane view to look at. The lane itself is computed by the server (playerReach), never here. Runs under
Blender's Python."""
import json

import bpy
import mathutils
import numpy

import bridgeAuthoring
import bridgeBoundaries
import bridgeCommands
import bridgeEntries
import bridgeExport
import bridgeMeshAccess
import bridgeSwim
import bridgeViews

seedKinds = ("safePoint", "zoneIn", "landing")


def footingRecord(ground, point):
  """The footing found under a point as standAt finds it: its point and the z of its face's normal, or None."""
  footing = ground.footing(list(point))
  if footing is None:
    return None
  return {"point": [float(value) for value in footing.point], "normalZ": abs(float(footing.normal.z))}


def zoneLineRecords(shortName):
  """The zone's own zone lines as boxes, each with its target and, for one leading back into the zone at a whole point, that point."""
  errors = bridgeBoundaries.zoneLineErrors()
  if errors:
    raise ValueError("Zone lines no lane can place: " + "; ".join(error["message"] for error in errors))
  records = []
  for line in bridgeBoundaries.zoneLineObjects():
    if not bridgeBoundaries.isAuthored(line):
      continue
    center, halfExtents = bridgeBoundaries.boxBounds(line)
    target = bridgeBoundaries.readSpec(line, bridgeMeshAccess.zoneLineProperty)
    landing = None
    if target is not None and shortName is not None and target["zone"] == shortName and all(target[axis] != bridgeBoundaries.keep for axis in bridgeBoundaries.targetCoordinates):
      landing = [float(target[axis]) for axis in bridgeBoundaries.targetCoordinates]
    records.append({"name": line.name, "center": center, "halfExtents": halfExtents, "target": target, "landing": landing})
  return records


def laneInput(outputPath):
  """Write the lane's input to outputPath (.npz): the collision triangles (float32 [n, 3, 3], zone axes), which of them are a boundary's,
  each undecided body's triangles, and a description of the rest as JSON."""
  bpy.context.view_layer.update()
  collision = bridgeExport.collisionTriangles()
  ground = bridgeEntries.ShippedGround(collision)
  zone = bridgeCommands.readZoneProperties(bpy.context.scene)
  listed = bridgeEntries.listEntries(ground)
  entries = []
  for entry in listed["entries"]:
    record = dict(entry)
    if entry["kind"] in seedKinds:
      record["seedFooting"] = footingRecord(ground, entry["at"])
    entries.append(record)
  bodies = bridgeSwim.swimDecisions()["undecided"]
  boundaryOwners = numpy.array([bridgeMeshAccess.boundaryProperty in bpy.data.objects[name] for name in collision["ownerNames"]] + [False])
  arrays = {
    "collision": collision["positions"][collision["triangles"]].astype(numpy.float32),
    "boundary": boundaryOwners[collision["owners"]],
  }
  for index, body in enumerate(bodies):
    positions, triangles = bridgeMeshAccess.worldTriangles(bridgeMeshAccess.splitParts(body))
    arrays[f"undecided{index}"] = positions[triangles].astype(numpy.float32)
  description = {
    "swimBoxes": [region | {key: bridgeSwim.readBox(bpy.data.objects[region["name"]])[key] for key in ("liquid", "body")} for region in bridgeSwim.swimRegions()],
    "zoneLines": zoneLineRecords(zone.get("shortName")),
    "entries": entries,
    "notFollowed": listed["notFollowed"],
    "regions": bridgeAuthoring.getRegions()["regions"],
    "builtFor": zone.get("builtFor"),
    "underworld": zone.get("underworld"),
    "undecidedBodies": [body.name for body in bodies],
  }
  arrays["description"] = numpy.array(json.dumps(description))
  numpy.savez(outputPath, **arrays)
  return {"outputPath": outputPath, "triangles": len(arrays["collision"])}


def clearSights(sights, short):
  """Which sights ([eye, target] pairs, zone axes) pass clear of what the zone ships (bridgeExport.collisionTriangles) up to `short`
  before their target: a list of booleans."""
  surfaces = bridgeExport.collisionSurfaces(bridgeExport.collisionTriangles())
  clear = []
  for eye, target in sights:
    start, end = mathutils.Vector(eye), mathutils.Vector(target)
    reach = (end - start).length - short
    clear.append(surfaces is None or reach <= 0 or surfaces.cast(start, (end - start).normalized(), reach) is None)
  return {"clear": clear}


def traceProbes(crossings, columns):
  """What the zone ships (bridgeExport.collisionTriangles) holds where a lane trace stops: for each crossing [start, end] (zone points, or
  None) the first face a cast from start toward end meets before end, with its object and whether that is a boundary (None where it
  meets none); for each column [x, y, bottom, top] (or None) every face a cast up the vertical line from bottom to top meets, lowest
  first, with its height, its normal's z, and its object."""
  surfaces = bridgeExport.collisionSurfaces(bridgeExport.collisionTriangles())
  if surfaces is None:
    raise ValueError("The zone ships no collision to probe")
  met = []
  for crossing in crossings:
    if crossing is None:
      met.append(None)
      continue
    start, end = mathutils.Vector(crossing[0]), mathutils.Vector(crossing[1])
    hit = surfaces.castOn(start, (end - start).normalized(), (end - start).length)
    met.append(None if hit is None else {
      "point": list(hit[0]), "object": hit[2], "boundary": bridgeMeshAccess.boundaryProperty in bpy.data.objects[hit[2]],
    })
  profiles = []
  for column in columns:
    if column is None:
      profiles.append(None)
      continue
    x, y, bottom, top = column
    faces, origin = [], mathutils.Vector((x, y, bottom))
    while origin.z < top:
      hit = surfaces.castOn(origin, bridgeMeshAccess.up, top - origin.z)
      if hit is None:
        break
      faces.append({"z": hit[0].z, "normalZ": hit[1].z, "object": hit[2]})
      origin = hit[0] + bridgeMeshAccess.up * bridgeMeshAccess.castNudge
    profiles.append(faces)
  return {"crossings": met, "columns": profiles}


def footings(points):
  """The ground on what the zone ships under each [x, y, z] (zone axes), found as standAt finds it: its height and the z of its face's
  normal, or None where there is none."""
  ground = bridgeEntries.ShippedGround(bridgeExport.collisionTriangles())
  found = [footingRecord(ground, point) for point in points]
  return {"footings": [None if footing is None else {"height": footing["point"][2], "normalZ": footing["normalZ"]} for footing in found]}


# The R2 sequence's steps: points placed by it spread evenly over a triangle without a random generator, so an object always gives the
# same points.
spreadSteps = (0.7548776662466927, 0.5698402909980532)


def surfacePoints(name, count):
  """count points spread over an object's surface by area (its evaluated meshes, a collection instance's parts as placed), each with its
  face's normal as the mesh winds it, and the surface's bounds, in zone axes: {points, normals, minimum, maximum}."""
  positions, triangles = bridgeMeshAccess.partTriangles(bridgeMeshAccess.objectParts(bridgeMeshAccess.requireObject(name)))
  corners = positions[triangles]
  crossed = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
  areas = numpy.linalg.norm(crossed, axis=1) / 2
  if not len(areas) or areas.sum() <= 0:
    raise ValueError(f"'{name}' has no faces to look at")
  chosen = numpy.minimum(numpy.searchsorted(numpy.cumsum(areas) / areas.sum(), (numpy.arange(count) + 0.5) / count), len(areas) - 1)
  across = numpy.arange(1, count + 1)[:, None] * numpy.array(spreadSteps) % 1.0
  folded = across.sum(axis=1) > 1
  across[folded] = 1 - across[folded]
  start = corners[chosen, 0]
  points = start + across[:, :1] * (corners[chosen, 1] - start) + across[:, 1:] * (corners[chosen, 2] - start)
  normals = crossed[chosen] / (2 * areas[chosen])[:, None]
  return {
    "points": points.tolist(), "normals": normals.tolist(), "minimum": corners.reshape(-1, 3).min(axis=0).tolist(),
    "maximum": corners.reshape(-1, 3).max(axis=0).tolist(),
  }


def requireViewZone(sky):
  """Refuse, naming them, the zone properties a client-shaded view needs that the zone and its sky (its state, as a view takes it) lack."""
  bridgeViews.requireZone(bridgeCommands.previewZone(sky))
  return {}


commands = {
  "laneInput": (laneInput, False),
  "clearSights": (clearSights, False),
  "traceProbes": (traceProbes, False),
  "footings": (footings, False),
  "surfacePoints": (surfacePoints, False),
  "requireViewZone": (requireViewZone, False),
}
