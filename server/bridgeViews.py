"""EQ preview rendering and picking in a temporary scene that links the open scene's objects. Runs under Blender's Python."""
import math
import time

import bpy
import mathutils

import bridgeModels

requiredZoneKeys = ("fogColor", "fogStart", "fogEnd", "sunAzimuthDegrees", "sunElevationDegrees", "sunColor", "sunStrength", "ambientColor")
previewName = "zonewrightPreview"
renderWidth = 960
renderHeight = 540
renderSamples = 4
# Starting values, to be calibrated against client screenshots: the reference renderer matched a live view at 52 degrees vertical.
verticalFieldOfViewDegrees = 52.0
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



def requireZone(zone):
  missing = [key for key in requiredZoneKeys if key not in zone]
  if missing:
    raise ValueError(f"Zone properties missing: {missing}; set them with setZoneProperties")


class PreviewScene:
  """A scene holding links to the open scene's renderable objects plus the preview's own camera, sun, world, and fog."""

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
    self.addSun()
    self.configureRender()
    self.configureWorld()
    self.configureFog()

  def addObject(self, newObject):
    self.scene.collection.objects.link(newObject)
    self.createdObjects.append(newObject)
    return newObject

  def addSun(self):
    sun = self.addObject(bpy.data.objects.new(previewName + "Sun", bpy.data.lights.new(previewName + "Sun", "SUN")))
    azimuth = math.radians(self.zone["sunAzimuthDegrees"])
    elevation = math.radians(self.zone["sunElevationDegrees"])
    towardSun = mathutils.Vector((math.sin(azimuth) * math.cos(elevation), math.cos(azimuth) * math.cos(elevation), math.sin(elevation)))
    sun.rotation_mode = "QUATERNION"
    sun.rotation_quaternion = towardSun.to_track_quat("Z", "Y")
    sun.data.color = self.zone["sunColor"]
    sun.data.energy = self.zone["sunStrength"]
    sun.data.specular_factor = 0.0
    sun.data.angle = 0.0
    sun.data.use_shadow = True

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
    eevee.use_shadows = True
    self.scene.view_settings.view_transform = "Standard"
    self.scene.view_settings.look = "None"
    self.scene.view_settings.exposure = 0.0
    self.scene.view_settings.gamma = 1.0
    self.scene.view_layers[0].use_pass_mist = True

  def configureWorld(self):
    world = bpy.data.worlds.new(previewName + "World")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (*self.zone["ambientColor"], 1.0)
    background.inputs["Strength"].default_value = 1.0
    world.mist_settings.start = self.zone["fogStart"]
    world.mist_settings.depth = self.zone["fogEnd"] - self.zone["fogStart"]
    world.mist_settings.falloff = "LINEAR"
    self.scene.world = world

  def configureFog(self):
    tree = bpy.data.node_groups.new(previewName + "Fog", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    layers = tree.nodes.new("CompositorNodeRLayers")
    layers.scene = self.scene
    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.inputs["B"].default_value = (*self.zone["fogColor"], 1.0)
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(layers.outputs["Mist"], mix.inputs["Factor"])
    tree.links.new(layers.outputs["Image"], mix.inputs["A"])
    tree.links.new(mix.outputs["Result"], output.inputs["Image"])
    self.scene.compositing_node_group = tree

  def depsgraph(self):
    with bpy.context.temp_override(scene=self.scene, view_layer=self.scene.view_layers[0]):
      return bpy.context.evaluated_depsgraph_get()

  def rayCast(self, origin, direction, distance):
    hit, location, normal, faceIndex, hitObject, _ = self.scene.ray_cast(self.depsgraph(), origin, direction, distance=distance)
    return (location, normal, faceIndex, hitObject) if hit else None

  def addFigure(self, groundPoint, figureModel, facingHeadingDegrees):
    """The figure model faces +X; facingHeadingDegrees is 0 = +Y, clockwise."""
    feet = groundPoint - mathutils.Vector((0, 0, figureModel["footHeight"] * figureModel["scale"]))
    figure = bridgeModels.modelObject(figureModel["folder"], previewName + "Figure", figureModel["scale"], feet, 90 - facingHeadingDegrees)
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
    bpy.data.node_groups.remove(self.scene.compositing_node_group)
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
  raise ValueError(f"A view is {{camera}}, {{eye, target}}, or {{standAt, headingDegrees, pitchDegrees}}; got keys {sorted(viewKeys)}")


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
    chest = position + mathutils.Vector((0, 0, figureModel["size"] / 2))
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
  preview = PreviewScene(sourceScene, zone)
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


def pick(sourceScene, zone, view, pixel):
  if len(pixel) != 2 or not (0 <= pixel[0] < renderWidth and 0 <= pixel[1] < renderHeight):
    raise ValueError(f"pixel {pixel} is outside the {renderWidth}x{renderHeight} render")
  preview = PreviewScene(sourceScene, zone)
  try:
    placeCamera(preview, view, None)
    topRight, _, bottomLeft, topLeft = preview.camera.data.view_frame(scene=preview.scene)
    across = (pixel[0] + 0.5) / renderWidth
    down = (pixel[1] + 0.5) / renderHeight
    localDirection = topLeft + (topRight - topLeft) * across + (bottomLeft - topLeft) * down
    origin = preview.camera.location.copy()
    direction = (preview.camera.rotation_quaternion @ localDirection).normalized()
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
