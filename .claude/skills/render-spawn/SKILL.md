---
name: render-spawn
description: Place and render EverQuest characters (NPCs, players, pets, the scale figure) in a zonewright Blender scene from the client's own models. Use whenever a scene or render needs EQ spawns, whether one character or a whole zone's population from a live dump.
---

# Render spawns

`placeSpawn` puts an EverQuest character into the open scene, drawn the size the client draws it, with the appearance the client would give it. The model is found the way the client finds it (see "Client models" in the README; `findModel` shows the search), built once, and cached under the tooling root (`models\built\<model>@<archive>@<appearance>`).

## placeSpawn

| Argument | Meaning |
|---|---|
| `zone` | The zone whose archives to search first (`poknowledge`, `neighborhood`, ...); `null` searches only what every zone loads |
| `model` | The actorDef, with or without `_ACTORDEF`: `DAF`, `HUF`, `KOB`, `GBN`, `PMA`, ... |
| `size` | EQ size. The client draws a model `size` units tall, so a size-5 dark elf female is 5 units tall |
| `location` + `headingDegrees` | Blender position of the feet and facing (0 = +Y, clockwise from above) |
| `eqLocation` + `eqHeading` | Or the server's values as the dumps give them; give one pair, not both |
| `variation`, `headType`, `textureSet` | Appearance: body piece, head piece, texture set |
| `snapToGround` | Drop the feet to the surface below (default true; the server's z sits a few units above the ground) |
| `source` | `"archive"` or `"archive:entry"` when one link tier defines the model twice |
| `name`, `collection` | Object name and collection |

The result's `source` names the archive and the link that chose it (`linkedBy`: the zone load order, `<zone>_chr.txt`, `eqclient.ini`, `GlobalLoad.txt`, or `OnDemandResources.txt`), the pieces drawn, how many materials the texture set swapped, the pose, and any `missingTextures`.

## From a live dump

`master\spawns.tsv` columns map to arguments:

| Column | Argument |
|---|---|
| `actorDef` | `model` (a value like `A | B` merges several captures; it is not a model name, so pick one or skip it) |
| `height` | `size` |
| `positions` | `x,y,z,heading`, so `eqLocation` = `[x, y, z]` and `eqHeading` = `heading` (the first entry of a `|` list) |
| `textureType` | `textureSet` (`-1` means no override, so 0) |
| `headType`, `variation` | `headType`, `variation` |

The dumps come from live servers and our client is a modified RoF2, so many spawns name models this client lacks or does not link to that zone; those fail with an error listing any unlinked archives that define the model. Skip them; do not borrow a model from an unlinked archive.

## Appearance rules

These are the client's rules, read from `eqgame.exe` and `EQGraphicsDX9.dll`:

- **Pieces.** A reset shows body `<code>00` and head `<code>HE00` (EQG), or `<code>_DMSPRITEDEF` and `<code>HE00_DMSPRITEDEF` (WLD). `variation` swaps in body `<code><nn>`; `headType` swaps in head `<code>HE<nn>`. A swap to a piece the model lacks keeps the current one. EQG pieces outside those two groups are always drawn.
- **Texture sets.**
  - EQG models swap each material `i` to the layer `C_<code>_S<set>_M<i+1>` in their `.lay` file.
  - WLD models swap material `<code><part>00<nn>_MDF` for `<code><part><set><nn>_MDF` when the file has it.
- **Unknown default head.** An EQG model with neither the requested head nor `HE00` (PMA, SCC, DVL, I25) fails: which head the client shows then is not known.

## Sizes

Common race defaults in the dumps: DAF 5, ELF 5, HUF 6, HUM 6, IKM 6.5, DKF 5.7.

## Scale figure

Every eye-level `renderView` (`standAt`) stands a dark elf female at size 5 about 15 units ahead, walked along the ground like a player so walls, drops, and climbs stop her, facing the camera.

## Limits

- **Pose.** WLD characters stand in their bind pose with the upper arms lowered 70 degrees (`pose: armsLowered`), when their arm bones carry the Luclin names. Others report `bind` (arms out). EQG characters stand in their bind pose. Animations are not applied yet.
- **Not drawn.** Equipment, faces (`faceStyle`), and particle effects are not drawn. Legacy 0x2C meshes (IVM and a few others) are not read and fail as unsupported.
- **Heading direction.** The heading's turn direction for `eqHeading` comes from the server's heading formula and has not yet been checked against a client screenshot.
