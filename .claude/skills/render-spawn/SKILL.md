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
| `animation`, `animationVariant`, `animationFrame` | Pose: an animation code (`L01`, `S03`) or the client's label (`WALK`, `WAVE`), a variant letter for a Luclin model (`A`, `B`, ...), and a frame; default `P01` (STAND STILL), first variant, frame 0 |
| `snapToGround` | Drop the feet to the surface below (default true; the server's z sits a few units above the ground) |
| `source` | `"archive"` or `"archive:entry"` to take a definition other than the first the client loads |
| `name`, `collection` | Object name and collection |

The result's `source` names the archive and the link that chose it (`linkedBy`: `eqgame.exe startup`, `GlobalLoad.txt`, the zone load order, `<zone>_chr.txt`, or `OnDemandResources.txt`), the pieces drawn, how many materials the texture set swapped, and any `missingTextures`. Its `pose` names the animation resource drawn (such as `S03AELF`), the archive it came from, `borrowedFrom` when the model borrowed another code's animation, the variants available, and the frame, `frameCount`, and `millisecondsPerFrame`, so a render can pick a frame by time.

## Animations

The client's animations, with its labels where it has them:

| Codes | Kind |
|---|---|
| `C01`-`C11` | Combat: KICK, STAB, IMPALE ATK, OVRHAND ATK, LEFT HND ATK, BASH, PUNCH, BOW, SWIM ATK, MONK RND KICK |
| `D01`-`D05` | Damage: NORMAL DMG, FALL DMG, DEATH SHUDDER, FALL DOWN |
| `L01`-`L12` | Movement: WALK, RUN, JUMP ACROSS, JUMP, FREE FALL, CROUCH WALK, CROUCH, TREAD WATER |
| `O01`-`O03`, `P01`-`P09` | Idle and standing: IDLE, STAND STILL, TURN RIGHT, SWIM FORWD |
| `S01`-`S29` | Social and emotes: OH YAH!, AGONY, WAVE, UP YOURS, and more without labels |
| `T01`-`T09` | Instruments, spells, and kicks: PLAY DRUM, PLAY LUTE, PLAY HORN, DEFENSE SPELL, GENERAL SPELL, MISSILE SPELL, FLYING KICK, MONK HND ATK 2 |

A model without an animation of its own borrows one, as the client does: a dark elf plays the wood elf's, a Luclin erudite the human's, a kobold the werewolf's. Not every model has every animation; asking for one it lacks is an error naming why.

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

- **EQG characters.** EQG characters (Drakkin and later races) stand in their bind pose: their animations (`.ani`) are not read yet, so asking for one is an error.
- **Not drawn.** Equipment, faces (`faceStyle`), and particle effects are not drawn. Legacy 0x2C meshes (IVM and a few others) are not read and fail as unsupported.
- **Heading direction.** The heading's turn direction for `eqHeading` comes from the server's heading formula and has not yet been checked against a client screenshot.
