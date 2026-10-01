---
name: render-spawn
description: Place and render EverQuest characters (NPCs, players, the scale figure) in a zonewright Blender scene from the client's own models. Use whenever a scene or render needs EQ spawns, whether one character or a whole zone's population from a live dump.
---

# Render spawns

`placeSpawn` puts an EverQuest character model into the open scene, drawn the size the client draws it. Models come straight from the client (`EVERQUEST_CLIENT`); each is posed and cached once under the tooling root (`models\characters\<code>`), keyed by its archive's size and modification time.

## placeSpawn

| Argument | Meaning |
|---|---|
| `modelCode` | The actorDef code without `_ACTORDEF`: `DAF` (dark elf female), `HUF`, `HUM`, `ELF`, `IKM`, ... |
| `size` | EQ size. The client draws a model `size` units tall, so a size-5 dark elf female is 5 units tall |
| `location` | Where the feet go, in Blender units (1 unit = 1 EQ unit). With `snapToGround` the spawn drops to the surface below |
| `headingDegrees` | Facing: 0 = +Y, clockwise seen from above |
| `name`, `collection` | Object name and collection |

The result reports the drawn height, where the feet landed, and the pose.

## Sizes

The live dumps (`master\spawns.tsv`, column `height`) give each spawn's size. Common race defaults there: DAF 5, ELF 5, HUF 6, HUM 6, IKM 6.5, DKF 5.7.

## From a live dump

`master\spawns.tsv` rows map directly: `actorDef` minus `_ACTORDEF` is `modelCode`, `height` is `size`, and `positions` is `x,y,z,heading` (heading in 512 units per turn). The mapping from EQ world X/Y/heading to Blender axes and `headingDegrees` is settled by the zone render calibration; until it is recorded here, convert by hand and check a render against a client screenshot.

## Scale figure

Every eye-level `renderView` (`standAt`) stands a dark elf female at size 5 about 15 units ahead, walked along the ground like a player so walls, drops, and climbs stop her, facing the camera.

## Limits

- Models: Luclin-era WLD characters in `global<code>_chr.s3d`. EQG skinned models (Drakkin and later races) are not read yet; `placeSpawn` fails for them.
- Pose: the bind pose with the upper arms lowered 70 degrees (`pose: armsLowered`). The client's animations for these models are not stored as WLD tracks, so spawns do not animate.
- Appearance: base textures only; armor texture variants, heads, and equipment are not applied yet.
