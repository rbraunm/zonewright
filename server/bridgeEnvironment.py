"""A zone's lights and particle emitters as Blender objects, so they are placed, moved, and exported like everything else. A light is a
point light whose eqRadius property holds the EQ radius (its reach in world units) and whose color is the EQ RGB, 0-1. An emitter is an
empty whose eqEmitterDefinition and eqEmitterLifespan properties hold the client emitter definition it shows and the list's lifespan
field, and eqEmitterAlwaysVisible the field some lists add. A client-shaded preview draws both, as the client does: the lights' light on
what they reach (bridgePointLights) and the emitters' particles (bridgeEmitterDrawing). Runs under Blender's Python."""
import json

import bpy

import bridgeCaveLight
import bridgeCommands
import bridgeMeshAccess
import bridgeObjects
import bridgeViews

radiusProperty = "eqRadius"
definitionProperty = "eqEmitterDefinition"
lifespanProperty = "eqEmitterLifespan"
alwaysVisibleProperty = "eqEmitterAlwaysVisible"
emitterDisplaySize = 2.0


def validatedLight(light):
  if light["radius"] <= 0 or len(light["color"]) != 3 or not all(0 <= component <= 1 for component in light["color"]):
    raise ValueError(f"Light '{light['name']}' needs a positive radius and an RGB color in 0-1, got {light['radius']} and {list(light['color'])}")
  return light


def requireClientContent(clientContent):
  if clientContent is not None and clientContent not in bridgeMeshAccess.clientContentKinds:
    raise ValueError(f"clientContent must be None or one of {list(bridgeMeshAccess.clientContentKinds)}, got {clientContent!r}")


def placeLights(lights, collection, clientContent, viewSky=None):
  """Point lights named, placed, colored, and reaching as given: [{name, position or onCave, color, radius}], a light anchored on a
  cave's lining (onCave, bridgeCaveLight) standing where its anchor finds the lining and keeping the anchor; clientContent marks those an
  imported zone brings (bridgeMeshAccess.clientContentKinds). Anchored lights are looked at in a client-shaded view, so the zone must draw
  one (viewSky: its sky's state, as a view takes it) before any is placed."""
  requireClientContent(clientContent)
  if any(light.get("onCave") is not None for light in lights):
    bridgeViews.requireZone(bridgeCommands.previewZone(viewSky))
  anchors = []
  for light in lights:
    if ("position" in light and light["position"] is not None) == ("onCave" in light and light["onCave"] is not None):
      raise ValueError(f"Light '{light['name']}' is placed at a position or anchored on a cave's lining (onCave), one of them")
    anchor = bridgeCaveLight.anchorDefinition(light["onCave"]) if light.get("onCave") is not None else None
    anchors.append((anchor, None if anchor is None else bridgeCaveLight.anchoredPosition(anchor)))
  destination = bridgeObjects.targetCollection(collection)
  placed, anchored = [], []
  for light, (anchor, found) in zip(lights, anchors):
    light = validatedLight(light | ({"position": found[0]} if found is not None else {}))
    data = bpy.data.lights.new(light["name"], "POINT")
    data.color = light["color"]
    data[radiusProperty] = float(light["radius"])
    lightObject = bpy.data.objects.new(light["name"], data)
    lightObject.location = light["position"]
    if anchor is not None:
      lightObject[bridgeCaveLight.anchorProperty] = json.dumps(anchor)
    if clientContent is not None:
      lightObject[bridgeMeshAccess.clientContentProperty] = clientContent
    destination.objects.link(lightObject)
    placed.append(lightObject.name)
    if anchor is not None:
      anchored.append(bridgeCaveLight.anchoredReport(lightObject, found[1]))
  return {"lights": len(placed), "collection": destination.name, "names": placed} | ({"anchored": anchored} if anchored else {})


def placeEmitters(emitters, collection, clientContent):
  """Emitter empties named, placed, and showing the given definitions: [{name, position, definition, lifespan, alwaysVisible (or None)}];
  clientContent marks those an imported zone brings."""
  requireClientContent(clientContent)
  destination = bridgeObjects.targetCollection(collection)
  placed = []
  for emitter in emitters:
    if not isinstance(emitter["definition"], int) or emitter["definition"] < 0:
      raise ValueError(f"Emitter '{emitter['name']}' needs a non-negative definition index, got {emitter['definition']!r}")
    if not isinstance(emitter["lifespan"], int):
      raise ValueError(f"Emitter '{emitter['name']}' needs an integer lifespan, got {emitter['lifespan']!r}")
    emitterObject = bpy.data.objects.new(emitter["name"], None)
    emitterObject.empty_display_type = "SPHERE"
    emitterObject.empty_display_size = emitterDisplaySize
    emitterObject.location = emitter["position"]
    emitterObject[definitionProperty] = emitter["definition"]
    emitterObject[lifespanProperty] = emitter["lifespan"]
    if emitter.get("alwaysVisible") is not None:
      emitterObject[alwaysVisibleProperty] = emitter["alwaysVisible"]
    if clientContent is not None:
      emitterObject[bridgeMeshAccess.clientContentProperty] = clientContent
    destination.objects.link(emitterObject)
    placed.append(emitterObject.name)
  return {"emitters": len(placed), "collection": destination.name, "names": placed}


def lightRecord(lightObject):
  """A rendered light as the zone file stores it; only point lights carrying an EQ radius export."""
  data = lightObject.data
  if data.type != "POINT" or radiusProperty not in data:
    raise ValueError(f"Light '{lightObject.name}' is a {data.type} light{'' if radiusProperty in data else ' without ' + radiusProperty}; a zone exports point lights placed with placeLight")
  return validatedLight({
    "name": lightObject.name, "position": list(lightObject.matrix_world.translation), "color": list(data.color), "radius": float(data[radiusProperty]),
  })


def isEmitter(sceneObject):
  return sceneObject.type == "EMPTY" and definitionProperty in sceneObject


def sceneDepsgraph(scene):
  """The scene evaluated, so a light or emitter placed or moved since its last evaluation reads where it now stands."""
  with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
    return bpy.context.evaluated_depsgraph_get()


def emitterRecord(emitterObject):
  lifespan = emitterObject.get(lifespanProperty)
  if not isinstance(lifespan, int):
    raise ValueError(f"Emitter '{emitterObject.name}' needs an integer {lifespanProperty}, got {lifespan!r}")
  return {
    "name": emitterObject.name, "definition": int(emitterObject[definitionProperty]),
    "position": list(emitterObject.matrix_world.translation), "lifespan": lifespan, "alwaysVisible": emitterObject.get(alwaysVisibleProperty),
  }


commands = {
  "placeLights": (placeLights, True),
  "placeEmitters": (placeEmitters, True),
}
