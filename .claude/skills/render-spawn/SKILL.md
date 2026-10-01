---
name: render-spawn
description: Place and render EverQuest characters (NPCs, players, pets, the scale figure) in a zonewright Blender scene from the client's own models. Use whenever a scene or render needs EQ spawns, whether one character or a whole zone's population from a live dump.
---

# Render spawns

`placeSpawn` puts an EverQuest character into the open scene, drawn at the scale the client draws it, with the appearance the client would give it. The model is found the way the client finds it (see "Client models" in the README; `findModel` shows the search), built once, and cached under the tooling root (`models\built\<model>@<archive>@<appearance>`).

## placeSpawn

| Argument | Meaning |
|---|---|
| `zone` | The zone whose archives to search first (`poknowledge`, `neighborhood`, ...); `null` searches only what every zone loads |
| `model` | The actorDef, with or without `_ACTORDEF`: `DAF`, `HUF`, `KOB`, `GBN`, `PMA`, ... |
| `height` | The spawn's height (EQEmu's size); the client draws the model at a scale from it (see Sizes) |
| `location` + `headingDegrees` | Blender position and facing (0 = +Y, clockwise from above) |
| `x`, `y`, `z` + `heading` | Or EQ's values as the server and the dumps give them; give these or the Blender pair, not both |
| `variation`, `headType`, `textureSet` | Appearance: body piece, head piece, texture set |
| `hairStyle` | The hair piece `<code>_HAIR_<nn>` the client attaches where it defines one (Drakkin) |
| `animation`, `animationVariant`, `animationFrame` | Pose: an animation code (`L01`, `S03`), the client's label (`WALK`, `WAVE`), or an EQG name (`STND`, `NRUN`); a variant letter for a Luclin model (`A`, `B`, ...); and a frame. The default is `P01` (STAND STILL), which EQG models play as `STND`, at the first variant and frame 0 |
| `snapToGround` | Stand the model origin `avatarHeight` above the surface below, as the client does (default true); false puts the origin at the position |
| `source` | `"archive"` or `"archive:entry"` to take a definition other than the first the client loads |
| `name`, `collection` | Object name and collection |

The result gives `height`, `scale`, and `avatarHeight` in the client's terms, so they compare with a dump row. Its `source` names the archive and the link that chose it (`linkedBy`: `eqgame.exe startup`, `GlobalLoad.txt`, the zone load order, `<zone>_chr.txt`, or `OnDemandResources.txt`), the pieces drawn, how many materials the texture set swapped, and any `missingTextures`. Its `pose` names the animation resource drawn (such as `S03AELF` or `WAVE_BA_1_DKF`) and the archive it came from. For a WLD model it also gives `borrowedFrom` (when the model borrowed another code's animation), the variants available, and `millisecondsPerFrame`. For an EQG model it gives `wldAnimation` and the frame's `frameMilliseconds` with the animation's `durationMilliseconds`. Both give the frame and `frameCount`, so a render can pick a frame by time.

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

EQG characters (Drakkin, the gnoll `GBN`, and later races) play the client's EQG animations, `<name>_BA_1_<code>`. The client maps each animation id to an EQG name and a WLD code: `/wave` is `WAVE` on an EQG model and `S03` on a WLD one; `STND` stands. Give a code or label and an EQG model plays the first EQG name mapped to it; give an EQG name and a WLD model plays its code. EQG animations have no lettered variants.

## From a live dump

`master\spawns.tsv` columns map to arguments:

| Column | Argument |
|---|---|
| `actorDef` | `model` (a value like `A | B` merges several captures; it is not a model name, so pick one or skip it) |
| `height` | `height` (the dump's `avatarHeight` should match the result's) |
| `positions` | `x,y,z,heading`, the first entry of a `|` list: `x`, `y`, `z`, `heading` |
| `textureType` | `textureSet` (`-1` means no override, so 0) |
| `headType`, `variation`, `hairStyle` | `headType`, `variation`, `hairStyle` |

The dumps come from live servers and our client is a modified RoF2, so many spawns name models this client lacks or does not link to that zone; those fail with an error listing any unlinked archives that define the model. Skip them; do not borrow a model from an unlinked archive.

## Appearance rules

These are the client's rules, read from `eqgame.exe` and `EQGraphicsDX9.dll`:

- **Pieces.** A reset shows body `<code>00` and head `<code>HE00` (EQG), or `<code>_DMSPRITEDEF` and `<code>HE00_DMSPRITEDEF` (WLD). `variation` swaps in body `<code><nn>`; `headType` swaps in head `<code>HE<nn>`. A swap to a piece the model lacks keeps the current one. EQG pieces outside those two groups are always drawn.
- **Texture sets.**
  - EQG models swap each material `i` to the layer `C_<code>_S<set>_M<i+1>` in their `.lay` file.
  - WLD models swap material `<code><part>00<nn>_MDF` for `<code><part><set><nn>_MDF` when the file has it.
- **Unknown default head.** An EQG model with neither the requested head nor `HE00` (PMA, SCC, DVL, I25) fails: which head the client shows then is not known.

## Sizes

The client draws a spawn at a scale from its height (`eqgame.exe` `0x5a3f40`):

| Model | Zone without `NewEngineZone` | Zone with `NewEngineZone` |
|---|---|---|
| WLD | height / 5 | height / 6.5 |
| EQG | height * 1.3 / 6 | height / 6 |

EQG models are those whose race the client registers with its EQG flag. `NewEngineZone` comes from the zone header, so set it with `setZoneProperties` before placing spawns: the dumps' `fields\zoneHeaders.tsv` gives it per zone (the bazaar, guild lobby, guild hall, and Plane of Knowledge have it off; the neighborhood and housing interiors on), and EQEmu sends false for every zone. A dark elf female of the race-default height 5 stands about 6.5 units tall with it off, 5 with it on.

`avatarHeight` is how high the client stands the model origin above the ground: `Resources\moddat.ini`'s `ROffset` for the model (3.125 when it has none) times the scale.

Common race defaults in the dumps: DAF 5, ELF 5, HUF 6, HUM 6, IKM 6.5, DKF 5.7.

## Scale figure

Every eye-level `renderView` (`standAt`) stands a dark elf female of height 5, drawn at the client's scale for the zone's `newEngineZone`, about 15 units ahead, walked along the ground like a player so walls, drops, and climbs stop her, facing the camera.

## Limits

- **Not drawn.** Equipment, faces (`faceStyle`), hair on Luclin WLD models, and particle effects are not drawn. Legacy 0x2C meshes (IVM and a few others) are not read and fail as unsupported.
- **Drakkin pieces.** A Drakkin draws its base model (`dkf.mod`, `dkm.mod`) and its hair. Its armor pieces (`dkf_<material>_<variation>_<bone>`), facial attachments, tattoos, and face and texture-set layers are not drawn. How the client picks armor pieces for an unequipped spawn is not yet read, so `variation`, `headType`, and `textureSet` are errors on Drakkin.
