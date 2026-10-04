"""EQ preview rendering and picking in a temporary scene that links the open scene's objects, lit and fogged as the client lights them
(bridgeClientLight). Runs under Blender's Python."""
import math
import os
import time

import bpy
import mathutils
import numpy

import bridgeClientLight
import bridgeMeshAccess
import bridgeModels
import skyDrawing
from playerScale import swimEyeAboveSurface

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
viewShadings = ("client", "layout")
# The sky is soft everywhere, so an equirectangular image at about a fifth of a degree a pixel draws it.
skyImageHeight = 1024
# Layout shading lights from the northwest, as relief maps do, so slopes read the same whatever the zone's sun.
layoutLightDirection = (-0.5, 0.5, 0.7071)
layoutAmbient = 0.3
layoutHeightColors = ((0.0, (0.22, 0.36, 0.26)), (0.35, (0.58, 0.56, 0.36)), (0.7, (0.62, 0.45, 0.32)), (1.0, (0.92, 0.9, 0.87)))



def requireZone(zone):
  missing = [key for key in requiredZoneKeys if key not in zone]
  if missing:
    raise ValueError(f"Zone properties missing: {missing}; set them with setZoneProperties (a sky supplies the light and the fog color)")


class PreviewScene:
  """A scene holding links to the open scene's renderable objects (guides, such as plot outlines, only when asked for) plus the
  preview's own camera and world, the client's lighting set from the zone. Where nothing is drawn shows the fog color, or the zone's
  sky (eqSky's state) once drawSky places it for the camera."""

  def __init__(self, sourceScene, zone, guides=True, sky=None):
    requireZone(zone)
    self.zone = zone
    self.sky = sky
    self.skyImage = None
    self.createdObjects = []
    self.scene = bpy.data.scenes.new(previewName)
    for sourceObject in sourceScene.objects:
      if sourceObject.type not in ("LIGHT", "CAMERA") and not sourceObject.hide_render and (guides or bridgeMeshAccess.guideProperty not in sourceObject):
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

  def drawSky(self):
    """The zone's sky behind everything, as the client draws it for the camera's height, which moves its horizon band."""
    if self.sky is None:
      return
    textures = {texture["path"]: numpy.load(texture["path"]) for satellite in self.sky["satellites"] for texture in satellite["textures"]}
    width = 2 * skyImageHeight
    colors = skyDrawing.skyColors(skyDrawing.equirectangularDirections(width, skyImageHeight), self.sky, self.camera.location.z, textures)
    pixels = numpy.ones((colors.shape[0], 4), dtype=numpy.float32)
    pixels[:, :3] = colors
    self.skyImage = bpy.data.images.new(previewName + "Sky", width, skyImageHeight, alpha=False, float_buffer=True)
    self.skyImage.colorspace_settings.name = "Non-Color"
    self.skyImage.pixels.foreach_set(pixels.ravel())
    nodes = self.scene.world.node_tree.nodes
    environment = nodes.new("ShaderNodeTexEnvironment")
    environment.image = self.skyImage
    environment.interpolation = "Linear"
    self.scene.world.node_tree.links.new(environment.outputs["Color"], nodes["Background"].inputs["Color"])

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
    override = self.scene.view_layers[0].material_override
    bpy.data.worlds.remove(self.scene.world)
    if self.skyImage is not None:
      bpy.data.images.remove(self.skyImage)
    bpy.data.scenes.remove(self.scene)
    if override is not None:
      bpy.data.materials.remove(override)


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
    standAt = view["standAt"]
    surfaces = bridgeMeshAccess.PlayerSurfaces()
    if len(standAt) == 2:
      bottom, top = sceneHeightRange(preview)
      ground = surfaces.footingBelow(mathutils.Vector((*standAt, top + mapClearance)), top - bottom + 2 * mapClearance)
      if ground is None:
        raise ValueError(f"No ground below {list(standAt)}")
    elif len(standAt) == 3:
      ground = surfaces.footingBelow(mathutils.Vector(standAt) + mathutils.Vector((0, 0, 1)), groundSearchDistance)
      if ground is None:
        raise ValueError(f"No ground within {groundSearchDistance} units below {list(standAt)}")
    else:
      raise ValueError(f"standAt is [x, y] or [x, y, z], got {standAt!r}")
    # Where the water stands over the eye, the player swims, eye at the surface.
    waterDepth = bridgeMeshAccess.waterDepthAt(bridgeMeshAccess.swimSurfaces(), ground)
    swimming = waterDepth is not None and waterDepth > eyeHeight - swimEyeAboveSurface
    eye = ground + mathutils.Vector((0, 0, waterDepth + swimEyeAboveSurface if swimming else eyeHeight))
    forward = headingPitchForward(view["headingDegrees"], view["pitchDegrees"])
    camera.location, camera.rotation_quaternion = eye, lookRotation(forward)
    description = {
      "eye": list(eye), "forward": list(forward), "ground": list(ground), "waterDepth": None if waterDepth is None else round(waterDepth, 2),
      "swimming": swimming, "figure": None,
    }
    if figureModel is not None:
      description["figure"] = list(placeScaleFigure(preview, surfaces, ground, view["headingDegrees"], figureModel))
    return description
  raise ValueError(f"A view is {{camera}}, {{eye, target}}, {{map}}, or {{standAt, headingDegrees, pitchDegrees}}; got keys {sorted(viewKeys)}")


def sceneCorners(preview):
  """Bounding box corners of the meshes as evaluated: an object's own bound_box lags behind a shaping pass changed since the last evaluation."""
  depsgraph = preview.depsgraph()
  corners = [sceneObject.matrix_world @ mathutils.Vector(corner) for sceneObject in preview.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.evaluated_get(depsgraph).bound_box]
  if not corners:
    raise ValueError("The scene has no meshes")
  return corners


def sceneHeightRange(preview):
  heights = [corner.z for corner in sceneCorners(preview)]
  return min(heights), max(heights)


def applyLayoutShading(preview, bandHeight):
  """Draw every surface unlit in a color for its height across the scene's height range, banded every bandHeight units so the band
  edges read as contours, and darker facing away from the layout light so slopes read; returns that height range."""
  if bandHeight <= 0:
    raise ValueError(f"bandHeight must be positive, got {bandHeight}")
  bottom, top = sceneHeightRange(preview)
  material = bpy.data.materials.new(previewName + "Layout")
  material.use_nodes = True
  nodes, links = material.node_tree.nodes, material.node_tree.links
  nodes.clear()
  geometry = nodes.new("ShaderNodeNewGeometry")
  height = nodes.new("ShaderNodeSeparateXYZ")
  links.new(geometry.outputs["Position"], height.inputs["Vector"])
  # Snapping to the nearest band rather than the band below keeps flat ground at a round height (a multiple of bandHeight) from
  # flickering between two bands with the GPU's rounding.
  halfBandUp = nodes.new("ShaderNodeMath")
  halfBandUp.operation = "ADD"
  links.new(height.outputs["Z"], halfBandUp.inputs[0])
  halfBandUp.inputs[1].default_value = bandHeight / 2
  banded = nodes.new("ShaderNodeMath")
  banded.operation = "SNAP"
  links.new(halfBandUp.outputs["Value"], banded.inputs[0])
  banded.inputs[1].default_value = bandHeight
  fraction = nodes.new("ShaderNodeMapRange")
  links.new(banded.outputs["Value"], fraction.inputs["Value"])
  fraction.inputs["From Min"].default_value = bottom
  fraction.inputs["From Max"].default_value = max(top, bottom + bandHeight)
  ramp = nodes.new("ShaderNodeValToRGB")
  elements = ramp.color_ramp.elements
  while len(elements) < len(layoutHeightColors):
    elements.new(0.5)
  for element, (position, color) in zip(elements, layoutHeightColors):
    element.position, element.color = position, (*color, 1.0)
  links.new(fraction.outputs["Result"], ramp.inputs["Fac"])
  facing = nodes.new("ShaderNodeVectorMath")
  facing.operation = "DOT_PRODUCT"
  links.new(geometry.outputs["Normal"], facing.inputs[0])
  facing.inputs[1].default_value = layoutLightDirection
  lit = nodes.new("ShaderNodeMath")
  lit.operation = "MAXIMUM"
  links.new(facing.outputs["Value"], lit.inputs[0])
  lit.inputs[1].default_value = 0.0
  shade = nodes.new("ShaderNodeMath")
  shade.operation = "MULTIPLY_ADD"
  links.new(lit.outputs["Value"], shade.inputs[0])
  shade.inputs[1].default_value = 1 - layoutAmbient
  shade.inputs[2].default_value = layoutAmbient
  shaded = nodes.new("ShaderNodeVectorMath")
  shaded.operation = "SCALE"
  links.new(ramp.outputs["Color"], shaded.inputs[0])
  links.new(shade.outputs["Value"], shaded.inputs["Scale"])
  emission = nodes.new("ShaderNodeEmission")
  links.new(shaded.outputs["Vector"], emission.inputs["Color"])
  output = nodes.new("ShaderNodeOutputMaterial")
  links.new(emission.outputs["Emission"], output.inputs["Surface"])
  preview.scene.view_layers[0].material_override = material
  return bottom, top


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


def placeScaleFigure(preview, surfaces, ground, headingDegrees, figureModel):
  """Walk ahead along what players stand on (surfaces, bridgeMeshAccess.PlayerSurfaces), as a player would, until figureDistance or a
  wall, drop, or climb stops the walk; then stand the figure there."""
  heading = math.radians(headingDegrees)
  ahead = mathutils.Vector((math.sin(heading), math.cos(heading), 0))
  side = mathutils.Vector((math.cos(heading), -math.sin(heading), 0))
  down = mathutils.Vector((0, 0, -1))
  position = ground.copy()
  sideHit = surfaces.cast(position + side * figureSideOffset + mathutils.Vector((0, 0, figureStepClimb)), down, figureStepClimb + figureStepDrop)
  if sideHit is not None:
    position = sideHit
  walked = 0.0
  while walked < figureDistance:
    chest = position + mathutils.Vector((0, 0, figureModel["avatarHeight"]))
    if surfaces.cast(chest, ahead, figureStep + figureClearance) is not None:
      break
    nextGround = surfaces.cast(position + ahead * figureStep + mathutils.Vector((0, 0, figureStepClimb)), down, figureStepClimb + figureStepDrop)
    if nextGround is None:
      break
    position = nextGround
    walked += figureStep
  if walked < figureMinimumDistance:
    raise ValueError(f"No room for the scale figure: the ground ahead stops after {walked:.0f} units")
  preview.addFigure(position, figureModel, headingDegrees + 180)
  return position


def roundVector(vector, digits=3):
  return [round(float(component), digits) for component in vector]


def renderView(sourceScene, zone, sky, view, outputPath, figureModel, shading, bandHeight, guides):
  if shading not in viewShadings:
    raise ValueError(f"shading must be one of {list(viewShadings)}, got '{shading}'")
  # A map or a layout drawing is for reading the shape, so neither is fogged nor has a sky.
  shapeOnly = "map" in view or shading == "layout"
  preview = PreviewScene(sourceScene, zone | {"fogDensity": 0.0} if shapeOnly else zone, guides, None if shapeOnly else sky)
  try:
    description = placeCamera(preview, view, figureModel)
    preview.drawSky()
    if shading == "layout":
      description["heightRange"] = list(applyLayoutShading(preview, bandHeight))
      description["bandHeight"] = bandHeight
      if preview.camera.data.type != "ORTHO":
        preview.camera.data.clip_end = max((corner - preview.camera.location).length for corner in sceneCorners(preview)) + mapClearance
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


thumbnailSide = 256
# Neutral daylight for looking at a model on its own: a grey ambient and a white sun from the front right, no fog.
thumbnailZone = {
  "ambientColor": [0.5, 0.5, 0.5], "specialAmbientColor": [0.0, 0.0, 0.0], "bounceColor": [0.0, 0.0, 0.0], "sunColor": [0.55, 0.55, 0.55],
  "sunAzimuthDegrees": 135.0, "sunElevationDegrees": 45.0, "fogColor": [0.3, 0.33, 0.37], "fogStart": 0.0, "fogEnd": 1000000.0, "fogDensity": 0.0,
  "newEngineZone": False,
}
# EQ models face +X: seen from in front, to the right, and above.
thumbnailViewDirection = mathutils.Vector((1.0, -0.8, 0.6)).normalized()


def renderModelThumbnails(models, outputFolder):
  """One square render per model ([{folder}]), drawn as the client draws it in neutral daylight and framed to fit from three-quarters
  above; returns the files in order."""
  scratch = bpy.data.scenes.new(previewName + "Models")
  written = []
  try:
    for index, model in enumerate(models):
      modelObject = bridgeModels.modelObject(model["folder"], f"{previewName}Model{index}", 1, (0, 0, 0), 0)
      scratch.collection.objects.link(modelObject)
      preview = PreviewScene(scratch, thumbnailZone)
      try:
        preview.scene.render.resolution_x = preview.scene.render.resolution_y = thumbnailSide
        corners = [modelObject.matrix_world @ mathutils.Vector(corner) for corner in modelObject.evaluated_get(preview.depsgraph()).bound_box]
        center = sum(corners, mathutils.Vector()) / len(corners)
        radius = max(max((corner - center).length for corner in corners), 0.01)
        distance = radius / math.sin(math.radians(verticalFieldOfViewDegrees / 2)) * 1.05
        preview.camera.location = center + thumbnailViewDirection * distance
        preview.camera.rotation_mode = "QUATERNION"
        preview.camera.rotation_quaternion = lookRotation(-thumbnailViewDirection)
        preview.camera.data.clip_start = distance / 100
        preview.camera.data.clip_end = distance + 2 * radius
        outputPath = os.path.join(outputFolder, f"model{index}.png")
        preview.scene.render.filepath = outputPath
        bpy.ops.render.render(write_still=True, scene=preview.scene.name)
        written.append({"file": outputPath, "radius": round(radius, 2)})
      finally:
        preview.remove()
        mesh = modelObject.data
        bpy.data.objects.remove(modelObject)
        bpy.data.meshes.remove(mesh)
  finally:
    bpy.data.scenes.remove(scratch)
  return {"thumbnails": written}
