---
name: render-door
description: Place and render EverQuest doors in a zonewright Blender scene from the client's own models. Doors are every server-placed model, not only doors: lifts, teleport pads, books, banners, furniture, and housing pieces. Use whenever a scene or render needs a zone's doors, one or all of them from a live dump.
---

# Render doors

`placeDoor` puts a door model into the open scene, closed, with its model origin at the door's position, as the server places it. The model is found the way the client finds it (see "Client models" in the README; `findModel` shows the search), built once, and cached under the tooling root.

## placeDoor

| Argument | Meaning |
|---|---|
| `zone` | The zone the door belongs to; its archives are searched first |
| `model` | The door's name: `POKDOOR500`, `OBJ_TELEPADA`, `IT11202`, ... |
| `location` + `headingDegrees` | Blender position of the model origin and turn (0 = +Y, clockwise from above) |
| `eqLocation` + `eqHeading` | Or the server's values as the dumps give them; give one pair, not both |
| `scalePercent` | The door's scale (default 100) |
| `source` | `"archive"` or `"archive:entry"` when one link tier defines the model twice |
| `name`, `collection` | Object name and collection |

Doors are not snapped to the ground: the server's z is the model origin, and measured doors sit on their floors at that z.

## From a live dump

`master\doors.tsv` columns map to arguments:

| Column | Argument |
|---|---|
| `name` | `model` |
| `x`, `y`, `z` | `eqLocation` = `[x, y, z]` |
| `heading` | `eqHeading` |
| `scaleFactor` | `scalePercent` |

Name each object after the zone and door `id` (for example `door_poknowledge_12`) so repeated doors of one model stay distinct. A row whose `visible` is 0 was hidden when captured; leave it out unless asked.

## Where door models come from

Door models resolve through the zone's links:

- **Classic zones:** `<zone>_obj.s3d`, `_obj2`, `_2_obj`.
- **EQG zones:** the zone `.eqg` and the archives in `<zone>_assets.txt`.
- **PoK:** `poknowledge_obj3.eqg`, which `eqgame.exe` loads for that zone only.

Failing those, a model may come from the global load list or `OnDemandResources.txt`. A name defined twice in one tier (IT67 is in both `equipment-01.eqg` and `gequip.s3d`) fails until `source` picks one.

## Limits

- **Closed only.** Doors are drawn closed; opening rotations are not applied.
- **Animated doors.** These are posed at frame 0 of their skeleton; their particle effects are not drawn (`particleCloudsNotDrawn` counts them).
- **Missing textures.** A texture missing from every linked archive draws magenta and is listed in `missingTextures`.
- **Heading direction.** The heading's turn direction for `eqHeading` comes from the server's heading formula; measured doors confirm the axis (no quarter-turn offset), but the turn direction is still to be checked against a client screenshot.
