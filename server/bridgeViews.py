"""EQ preview rendering and picking in a temporary scene that links the open scene's objects, lit and fogged as the client lights them
(bridgeClientLight). Runs under Blender's Python."""
import math
import os
import time

import bpy
import mathutils
import numpy

import bridgeClientLight
import bridgeModels

requiredZoneKeys = (
  "ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "sunAzimuthDegrees", "sunElevationDegrees", "fogColor", "fogStart", "fogEnd",
  "fogDensity", "newEngineZone",
)
previewName = "zonewrightPreview"
renderWidth = 960
renderHeight = 540
renderSamples = 4
# Measured by aligning renders to Plane of Knowledge and Eastern Wastes screenshots (16:9 crops of the live client's first-person view).
verticalFieldOfViewDegrees = 46.5
eyeHeight = 5.5
cameraClipStart = 0.5
groundSearchDistance = 50.0
figureDistance = 15.0
figureStep = 1.0
figureClearance = 1.5
figureStepClimb = 2.0
figureStepDrop = 4.0
figureMinimumDistance = 3.0
figureSideOffset = 1.5
mapClearance = 100.0



def requireZone(zone):
  missing = [key for key in requiredZoneKeys if key not in zone]
  if missing:
    raise ValueError(f"Zone properties missing: {missing}; set them with setZoneProperties")


class PreviewScene:
  """A scene holding links to the open scene's renderable objects plus the preview's own camera and world, the client's lighting set
  from the zone. Where nothing is drawn shows the fog color; the client's sky is not drawn yet."""

  def __init__(self, sourceScene, zone):
    requireZone(zone)
    self.zone = zone
    self.createdObjects = []
    self.scene = bpy.data.scenes.new(previewName)
    for sourceObject in sourceScene.objects:
      if sourceObject.type not in ("LIGHT", "CAMERA") and not sourceObject.hide_render:
        self.scene.collection.objects.link(sourceObject)
    self.camera = self.addObject(bpy.data.objects.new(previewName + "Camera", bpy.data.cameras.new(previewName + "Camera")))
    self.camera.data.sensor_fit = "VERTICAL"
    self.camera.data.angle = math.radians(verticalFieldOfViewDegrees)
    self.camera.data.clip_start = cameraClipStart
    self.camera.data.clip_end = zone["fogEnd"]
    self.scene.camera = self.camera
    self.configureRender()
    self.configureWorld()
    bridgeClientLight.applyEnvironment(zone)

  def addObject(self, newObject):
    self.scene.collection.objects.link(newObject)
    self.createdObjects.append(newObject)
    return newObject

  def configureRender(self):
    render = self.scene.render
    render.engine = "BLENDER_EEVEE"
    render.resolution_x = renderWidth
    render.resolution_y = renderHeight
    render.resolution_percentage = 100
    render.film_transparent = False
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.color_depth = "8"
    for stampProperty in bpy.types.RenderSettings.bl_rna.properties:
      if stampProperty.identifier.startswith("use_stamp"):
        setattr(render, stampProperty.identifier, False)
    eevee = self.scene.eevee
    eevee.taa_render_samples = renderSamples
    eevee.use_raytracing = False
    eevee.use_fast_gi = False
    eevee.use_shadows = False
    # Raw: output values are written as computed, as the client writes its texture-times-light bytes.
    self.scene.view_settings.view_transform = "Raw"
    self.scene.view_settings.look = "None"
    self.scene.view_settings.exposure = 0.0
    self.scene.view_settings.gamma = 1.0

  def configureWorld(self):
    world = bpy.data.worlds.new(previewName + "World")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (*self.zone["fogColor"], 1.0)
    background.inputs["Strength"].default_value = 1.0
    self.scene.world = world

  def depsgraph(self):
    with bpy.context.temp_override(scene=self.scene, view_layer=self.scene.view_layers[0]):
      return bpy.context.evaluated_depsgraph_get()

  def rayCast(self, origin, direction, distance):
    hit, location, normal, faceIndex, hitObject, _ = self.scene.ray_cast(self.depsgraph(), origin, direction, distance=distance)
    return (location, normal, faceIndex, hitObject) if hit else None

  def addFigure(self, groundPoint, figureModel, facingHeadingDegrees):
    """The figure model faces +X and stands its origin avatarHeight above the ground; facingHeadingDegrees is 0 = +Y, clockwise."""
    origin = groundPoint + mathutils.Vector((0, 0, figureModel["avatarHeight"]))
    figure = bridgeModels.modelObject(figureModel["folder"], previewName + "Figure", figureModel["scale"], origin, 90 - facingHeadingDegrees)
    return self.addObject(figure)

  def remove(self):
    for createdObject in self.createdObjects:
      data = createdObject.data
      bpy.data.objects.remove(createdObject)
      if isinstance(data, bpy.types.Camera):
        bpy.data.cameras.remove(data)
      elif isinstance(data, bpy.types.Light):
        bpy.data.lights.remove(data)
      elif isinstance(data, bpy.types.Mesh):
        bpy.data.meshes.remove(data)
    bpy.data.worlds.remove(self.scene.world)
    bpy.data.scenes.remove(self.scene)


def lookRotation(forward):
  if forward.length == 0:
    raise ValueError("The view direction has zero length")
  return forward.normalized().to_track_quat("-Z", "Y")


def headingPitchForward(headingDegrees, pitchDegrees):
  heading = math.radians(headingDegrees)
  pitch = math.radians(pitchDegrees)
  return mathutils.Vector((math.sin(heading) * math.cos(pitch), math.cos(heading) * math.cos(pitch), math.sin(pitch)))


def placeCamera(preview, view, figureModel):
  """Position the preview camera from a view; returns a description, including the scale figure when one is placed."""
  viewKeys = set(view)
  camera = preview.camera
  camera.rotation_mode = "QUATERNION"
  if viewKeys == {"camera"}:
    source = bpy.data.objects.get(view["camera"])
    if source is None or source.type != "CAMERA":
      raise ValueError(f"No camera object named '{view['camera']}'")
    location, rotation, _ = source.matrix_world.decompose()
    camera.location, camera.rotation_quaternion = location, rotation
    return {"eye": list(location), "forward": list(rotation @ mathutils.Vector((0, 0, -1))), "figure": None}
  if viewKeys == {"eye", "target"}:
    eye, target = mathutils.Vector(view["eye"]), mathutils.Vector(view["target"])
    camera.location, camera.rotation_quaternion = eye, lookRotation(target - eye)
    return {"eye": list(eye), "forward": list((target - eye).normalized()), "figure": None}
  if viewKeys == {"map"}:
    return placeMapCamera(preview, view["map"])
  if viewKeys == {"standAt", "headingDegrees", "pitchDegrees"}:
    standAt = mathutils.Vector(view["standAt"])
    groundHit = preview.rayCast(standAt + mathutils.Vector((0, 0, 1)), mathutils.Vector((0, 0, -1)), groundSearchDistance)
    if groundHit is None:
      raise ValueError(f"No ground within {groundSearchDistance} units below {list(standAt)}")
    eye = groundHit[0] + mathutils.Vector((0, 0, eyeHeight))
    forward = headingPitchForward(view["headingDegrees"], view["pitchDegrees"])
    camera.location, camera.rotation_quaternion = eye, lookRotation(forward)
    description = {"eye": list(eye), "forward": list(forward), "ground": list(groundHit[0]), "figure": None}
    if figureModel is not None:
      description["figure"] = list(placeScaleFigure(preview, groundHit[0], view["headingDegrees"], figureModel))
    return description
  raise ValueError(f"A view is {{camera}}, {{eye, target}}, {{map}}, or {{standAt, headingDegrees, pitchDegrees}}; got keys {sorted(viewKeys)}")


def sceneHeightRange(preview):
  heights = [(sceneObject.matrix_world @ mathutils.Vector(corner)).z for sceneObject in preview.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.bound_box]
  if not heights:
    raise ValueError("The scene has no meshes to map")
  return min(heights), max(heights)


def placeMapCamera(preview, mapView):
  """Straight down from above everything, orthographic, north (+Y) up and east (+X) right."""
  if not isinstance(mapView, dict) or set(mapView) != {"center", "width"} or len(mapView["center"]) != 2 or mapView["width"] <= 0:
    raise ValueError(f"A map view is {{\"map\": {{\"center\": [x, y], \"width\": w}}}} with a positive width, got {mapView!r}")
  bottom, top = sceneHeightRange(preview)
  camera = preview.camera
  camera.data.type = "ORTHO"
  camera.data.sensor_fit = "HORIZONTAL"
  camera.data.ortho_scale = mapView["width"]
  camera.location = mathutils.Vector((*mapView["center"], top + mapClearance))
  camera.rotation_quaternion = mathutils.Quaternion()
  camera.data.clip_end = top - bottom + 2 * mapClearance
  return {"mapCenter": list(mapView["center"]), "mapWidth": mapView["width"], "mapHeight": mapView["width"] * renderHeight / renderWidth, "unitsPerPixel": mapView["width"] / renderWidth, "figure": None}


def placeScaleFigure(preview, ground, headingDegrees, figureModel):
  """Walk ahead along the ground, as a player would, until figureDistance or a wall, drop, or climb stops the walk; then stand the figure there."""
  heading = math.radians(headingDegrees)
  ahead = mathutils.Vector((math.sin(heading), math.cos(heading), 0))
  side = mathutils.Vector((math.cos(heading), -math.sin(heading), 0))
  position = ground.copy()
  sideHit = preview.rayCast(position + side * figureSideOffset + mathutils.Vector((0, 0, figureStepClimb)), mathutils.Vector((0, 0, -1)), figureStepClimb + figureStepDrop)
  if sideHit is not None:
    position = sideHit[0]
  walked = 0.0
  while walked < figureDistance:
    chest = position + mathutils.Vector((0, 0, figureModel["avatarHeight"]))
    if preview.rayCast(chest, ahead, figureStep + figureClearance) is not None:
      break
    nextGround = preview.rayCast(position + ahead * figureStep + mathutils.Vector((0, 0, figureStepClimb)), mathutils.Vector((0, 0, -1)), figureStepClimb + figureStepDrop)
    if nextGround is None:
      break
    position = nextGround[0]
    walked += figureStep
  if walked < figureMinimumDistance:
    raise ValueError(f"No room for the scale figure: the ground ahead stops after {walked:.0f} units")
  preview.addFigure(position, figureModel, headingDegrees + 180)
  return position


def roundVector(vector, digits=3):
  return [round(float(component), digits) for component in vector]


def renderView(sourceScene, zone, view, outputPath, figureModel):
  # A map is for reading the layout, so it is drawn without fog.
  preview = PreviewScene(sourceScene, zone | {"fogDensity": 0.0} if "map" in view else zone)
  try:
    description = placeCamera(preview, view, figureModel)
    preview.scene.render.filepath = outputPath
    start = time.perf_counter()
    bpy.ops.render.render(write_still=True, scene=preview.scene.name)
    renderSeconds = time.perf_counter() - start
  finally:
    preview.remove()
  return {key: roundVector(value) if isinstance(value, list) else value for key, value in description.items()} | {
    "outputPath": outputPath,
    "width": renderWidth,
    "height": renderHeight,
    "renderSeconds": round(renderSeconds, 2),
  }


def renderPasses(sourceScene, zone, view, outputFolder, passNames):
  """Render a view once per named bridgeClientLight pass as raw floats (numpy .npy, rows top to bottom, RGBA, alpha 0 where nothing
  is drawn) for calibration: the drawn color (lit), the base (texture) color, the normal, the baked light, and the share of scene
  light."""
  unknown = sorted(set(passNames) - set(bridgeClientLight.passes))
  if unknown:
    raise ValueError(f"Unknown passes {unknown}; known: {list(bridgeClientLight.passes)}")
  preview = PreviewScene(sourceScene, zone)
  written = {}
  try:
    description = placeCamera(preview, view, None)
    preview.scene.render.film_transparent = True
    preview.scene.render.image_settings.file_format = "OPEN_EXR"
    preview.scene.render.image_settings.color_mode = "RGBA"
    preview.scene.render.image_settings.color_depth = "32"
    for passName in passNames:
      bridgeClientLight.selectPass(passName)
      exrPath = os.path.join(outputFolder, f"{passName}.exr")
      preview.scene.render.filepath = exrPath
      bpy.ops.render.render(write_still=True, scene=preview.scene.name)
      image = bpy.data.images.load(exrPath)
      try:
        pixels = numpy.empty(image.size[0] * image.size[1] * 4, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
      finally:
        bpy.data.images.remove(image)
      arrayPath = os.path.join(outputFolder, f"{passName}.npy")
      numpy.save(arrayPath, pixels.reshape(renderHeight, renderWidth, 4)[::-1])
      os.remove(exrPath)
      written[passName] = arrayPath
  finally:
    bridgeClientLight.selectPass("lit")
    preview.remove()
  return {key: roundVector(value) if isinstance(value, list) else value for key, value in description.items()} | {"passes": written}


def pick(sourceScene, zone, view, pixel):
  if len(pixel) != 2 or not (0 <= pixel[0] < renderWidth and 0 <= pixel[1] < renderHeight):
    raise ValueError(f"pixel {pixel} is outside the {renderWidth}x{renderHeight} render")
  preview = PreviewScene(sourceScene, zone)
  try:
    placeCamera(preview, view, None)
    topRight, _, bottomLeft, topLeft = preview.camera.data.view_frame(scene=preview.scene)
    across = (pixel[0] + 0.5) / renderWidth
    down = (pixel[1] + 0.5) / renderHeight
    localPoint = topLeft + (topRight - topLeft) * across + (bottomLeft - topLeft) * down
    rotation = preview.camera.rotation_quaternion
    if preview.camera.data.type == "ORTHO":
      origin = preview.camera.location + rotation @ mathutils.Vector((localPoint.x, localPoint.y, 0))
      direction = rotation @ mathutils.Vector((0, 0, -1))
    else:
      origin = preview.camera.location.copy()
      direction = (rotation @ localPoint).normalized()
    hit = preview.rayCast(origin, direction, preview.camera.data.clip_end)
    if hit is None:
      return {"hit": False, "origin": roundVector(origin), "direction": roundVector(direction)}
    location, normal, faceIndex, hitObject = hit
    evaluated = hitObject.evaluated_get(preview.depsgraph())
    materialIndex = evaluated.data.polygons[faceIndex].material_index
    material = evaluated.material_slots[materialIndex].material if materialIndex < len(evaluated.material_slots) else None
    return {
      "hit": True,
      "object": hitObject.name,
      "position": roundVector(location),
      "normal": roundVector(normal),
      "distance": round((location - origin).length, 3),
      "material": material.name if material else None,
      "origin": roundVector(origin),
      "direction": roundVector(direction),
    }
  finally:
    preview.remove()
