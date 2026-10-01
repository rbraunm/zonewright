---
name: render-object
description: Place and render EverQuest ground objects in a zonewright Blender scene from the client's own models: tradeskill containers (kilns, looms, forges, ovens), ground spawns, dropped items, and housing pieces. Use whenever a scene or render needs a zone's objects, one or all of them from a live dump.
---

# Render objects

`placeObject` puts a ground object's model into the open scene, with its model origin at the object's position, as the server places it. The model is found the way the client finds it (see "Client models" in the README; `findModel` shows the search), built once, and cached under the tooling root.

## placeObject

| Argument | Meaning |
|---|---|
| `zone` | The zone the object is in; its archives are searched first |
| `model` | The object's actor: `IT10800_ACTORDEF` (with or without `_ACTORDEF`), ... |
| `location` + `headingDegrees` | Blender position of the model origin and turn (0 = +Y, clockwise from above) |
| `eqLocation` + `eqHeading` | Or the server's values as the dumps give them; give one pair, not both |
| `scale` | The object's scale (default 1) |
| `source` | `"archive"` or `"archive:entry"` when one link tier defines the model twice |
| `name`, `collection` | Object name and collection |

Objects are not snapped to the ground: the server's z is the model origin.

## From a live dump

`master\ground.tsv` columns map to arguments:

| Column | Argument |
|---|---|
| `name` | `model` |
| `x`, `y`, `z` | `eqLocation` = `[x, y, z]` |
| `heading` | `eqHeading` |
| `scale` | `scale` |

Pitch and roll are 0 in every dumped row, so they are not taken. Housing items placed by players are in `placedObjects\<server>.tsv`.

## Where object models come from

Item models (`IT<number>`) resolve as follows:

- **Zone archives first:** the neighborhood's kilns and looms come from `tradeskill_objects.eqg`, which `neighborhood_assets.txt` names.
- **Then global archives:** the `GEquip*` archives that `GlobalLoad.txt` loads.
- **Then on-demand:** `OnDemandResources.txt` entries such as `phexterior1a.eqg^IT20026.MOD`.

## Limits

- **Missing textures.** A texture missing from every linked archive draws magenta and is listed in `missingTextures`. The neighborhood's `OBJ_TREEM` bark is one: only its normal map ships.
- **Particles.** Particle effects (flames, smoke) are not drawn.
- **Heading direction.** The heading's turn direction for `eqHeading` is still to be checked against a client screenshot (see `render-door`).
