"""Review cameras: guides kept in the .blend for looking at the zone the same way again, in their own collection, never drawn in views
and never exported. A review camera holds the pose of a view (an eye and a target, standing where a player stands, or framing objects),
where the scale figure stood in it, and a note of what to judge there. Runs under Blender's Python."""
import json
import math

import bpy
import mathutils

import bridgeCommands
import bridgeMeshAccess
import bridgeObjects
import bridgeViews

cameraCollectionName = "reviewCameras"
reviewCameraProperty = "zonewrightReviewCamera"


def requireName(name, kind):
  if not isinstance(name, str) or not name.strip() or name != name.strip():
    raise ValueError(f"A {kind} needs a name without spaces at its ends, got {name!r}")


def roundVector(vector, digits=3):
  return [round(float(component), digits) for component in vector]


def removeIfEmpty(collectionName):
  collection = bpy.data.collections.get(collectionName)
  if collection is not None and not collection.all_objects:
    bpy.data.collections.remove(collection)


def reviewCameras():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if reviewCameraProperty in sceneObject), key=lambda sceneObject: sceneObject.name)


def reviewPose(cameraObject):
  """A review camera's location and rotation as stored, or None for any other camera or one parented or turned another way since it was
  saved."""
  if reviewCameraProperty not in cameraObject or cameraObject.parent is not None or cameraObject.rotation_mode != "QUATERNION":
    return None
  return cameraObject.location.copy(), cameraObject.rotation_quaternion.copy()


def savedFigure(cameraObject):
  """Where the scale figure stood in the view a review camera was saved from ({at, facingDegrees}), or None."""
  return json.loads(cameraObject[reviewCameraProperty])["figure"] if reviewCameraProperty in cameraObject else None


def describeCamera(cameraObject):
  saved = json.loads(cameraObject[reviewCameraProperty])
  location, rotation, _ = cameraObject.matrix_world.decompose()
  forward = rotation @ mathutils.Vector((0.0, 0.0, -1.0))
  return {
    "name": cameraObject.name, "note": saved["note"], "view": saved["view"], "eye": roundVector(location),
    "headingDegrees": round(math.degrees(math.atan2(forward.x, forward.y)) % 360, 2), "pitchDegrees": round(math.degrees(math.asin(max(-1.0, min(1.0, forward.z)))), 2),
    "figure": None if saved["figure"] is None else roundVector(saved["figure"]["at"]),
  }


def requireSavedView(view):
  keys = set(view) if isinstance(view, dict) else None
  if keys is None or not (keys == {"eye", "target"} or keys == {"frame"} or keys - {"figureAt"} == {"standAt", "headingDegrees", "pitchDegrees"}):
    raise ValueError(
      "A review camera is saved from an {eye, target} view, a {standAt, headingDegrees, pitchDegrees} view (figureAt optional), or a"
      f" {{frame}} view; got {view!r}"
    )


def saveReviewCamera(name, view, note, sky, figureModel):
  requireName(name, "review camera")
  requireSavedView(view)
  if not isinstance(note, str) or not note.strip():
    raise ValueError("A review camera needs its note: what to judge from it")
  existing = bpy.data.objects.get(name)
  if existing is not None and reviewCameraProperty not in existing:
    raise ValueError(f"An object named '{name}' already exists and is not a review camera")
  preview = bridgeViews.PreviewScene(bpy.context.scene, bridgeCommands.previewZone(sky), True, None)
  try:
    description = bridgeViews.placeCamera(preview, view, figureModel)
    location, rotation = preview.camera.location.copy(), preview.camera.rotation_quaternion.copy()
  finally:
    preview.remove()
  cameraObject = existing if existing is not None else newCamera(name)
  cameraObject.rotation_mode = "QUATERNION"
  cameraObject.location, cameraObject.rotation_quaternion = location, rotation
  figure = None if description["figure"] is None else {"at": description["figure"], "facingDegrees": description["figureFacingDegrees"]}
  cameraObject[reviewCameraProperty] = json.dumps({"note": note.strip(), "view": view, "figure": figure})
  bpy.context.view_layer.update()
  return describeCamera(cameraObject)


def newCamera(name):
  bridgeObjects.requireNewName(name)
  data = bpy.data.cameras.new(name)
  data.sensor_fit = "VERTICAL"
  data.angle = math.radians(bridgeViews.verticalFieldOfViewDegrees)
  data.clip_start = bridgeViews.cameraClipStart
  cameraObject = bpy.data.objects.new(name, data)
  cameraObject[bridgeMeshAccess.guideProperty] = "reviewCamera"
  bridgeObjects.targetCollection(cameraCollectionName).objects.link(cameraObject)
  return cameraObject


def getReviewCameras():
  return {"cameras": [describeCamera(cameraObject) for cameraObject in reviewCameras()]}


def requireKnown(names, byName, kind):
  if not names:
    raise ValueError(f"Name the review {kind} to delete")
  missing = sorted(set(names) - set(byName))
  if missing:
    raise ValueError(f"No review {kind} {missing} (saved: {sorted(byName)}); nothing deleted")


def deleteReviewCameras(names):
  byName = {cameraObject.name: cameraObject for cameraObject in reviewCameras()}
  requireKnown(names, byName, "cameras")
  for name in sorted(set(names)):
    data = byName[name].data
    bpy.data.objects.remove(byName[name])
    bpy.data.cameras.remove(data)
  removeIfEmpty(cameraCollectionName)
  return {"deleted": sorted(set(names)), "remaining": [cameraObject.name for cameraObject in reviewCameras()]}


commands = {
  "saveReviewCamera": (saveReviewCamera, True),
  "getReviewCameras": (getReviewCameras, False),
  "deleteReviewCameras": (deleteReviewCameras, True),
}
