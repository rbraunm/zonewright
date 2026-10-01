# zonewright

MCP server through which Claude manages and works in Blender to build EverQuest zones.

## Install (Windows)

Requires Python 3.14 on PATH as `python`, git, and Claude Code.

From the root of a clone of this repository:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
claude
```

Run Claude Code from the repository root, since `.mcp.json` launches the server with paths relative to it. On first launch, accept the workspace trust prompt and approve the `zonewright` server. `claude mcp list` then shows `zonewright` as connected.

## Tooling

`toolingManifest.json` pins Blender and every extension: version, download URL, and SHA-256. `syncTooling` makes `%LOCALAPPDATA%\zonewright` match it. Blender runs in portable mode (a `portable` folder next to `blender.exe`), so it never reads or writes the config of any Blender installed for personal use. Logs rotate daily under `%LOCALAPPDATA%\zonewright\logs` and are kept 90 days.

To upgrade Blender, update the version, URL, and SHA-256 (from the release's published `.sha256` file) and run `syncTooling`.

### Machine profile

Performance settings are discovered per machine, never configured by hand. `syncTooling` ends by profiling the machine whenever `machineProfile.json` (under the tooling root) is missing or stale: it benchmarks a multi-material EEVEE render on each Blender GPU backend (Vulkan, OpenGL), rejects software renderers, and keeps the fastest hardware backend. Parallel CPU work uses 80% of the logical processors. The profile records the Blender version and a hardware fingerprint; when either changes, Blender tools fail until `syncTooling` re-profiles. `profileMachine` re-measures on demand. On the development machine (RTX 2080 Ti) Vulkan won: warm renders take about 0.1 s, and its shader cache survives Blender restarts, so a known scene's first render takes under a second instead of 18 s on OpenGL.

| Tool | Does |
|---|---|
| `getToolingStatus` | Reports Blender (`missing`, `broken`, `versionMismatch`, `installed`), each pinned extension (`missing`, `versionMismatch`, `installed`), and installed extensions that are not pinned |
| `syncTooling` | Installs the pinned Blender after verifying its SHA-256, removes other Blender versions, and installs, upgrades, or removes extensions to match the manifest. Fails if Blender is running from the tooling root, or if the pinned install is `broken` or `versionMismatch` (clear it by hand) |
| `addExtension` | Pins the newest extensions.blender.org release of an extension compatible with the pinned Blender, then syncs. Fails if a different `version` is requested |
| `removeExtension` | Unpins an extension, then syncs to uninstall it |
| `profileMachine` | Re-measures the machine: CPU workers and the fastest hardware GPU backend for Blender |

## Scale

Blender scenes are authored at **1 Blender unit = 1 EQ unit**, Z up, in the zone files' own axes, so client models and zone files load unchanged. The server, and so the live dumps, give positions as (x, y, z) with x and y swapped against the zone files: Blender = (y, x, z), measured by every dumped door, ground object, and spawn landing on its zone's geometry only that way. `/loc` prints the server's y, x, z, which is Blender's x, y, z in order. Spawns draw at the client's scale for their height (see "Spawn size" under Client models): a dark elf female of the race-default height 5 stands about 6.5 units tall in a zone without `NewEngineZone` and about 5 in one with it. Eye-level views put the eye 5.5 units above the ground.

EQGZI's Blender exporter (`xackery/eqgzi` `out/convert.py`) works at 1 Blender unit = 2 EQ units and writes placements as EQ = (-Blender.y, Blender.x, Blender.z) x 2. Phase 2 export applies that conversion; nothing in Phase 1 does.

## Blender bridge

The first tool that needs Blender starts the pinned Blender headless (`--background --factory-startup`, portable config) running `server/bridgeMain.py`, which enables the pinned extensions and serves commands over 127.0.0.1 on a random port with a per-session token. Commands run one at a time on Blender's main thread; errors come back with their traceback. If Blender exits, the next call reports the exit code and Blender's last output, and the call after that starts a fresh Blender. Blender launches with the profiled GPU backend and 80% of the CPU for its threads and shader compilation. `syncTooling`, `addExtension`, `removeExtension`, and `profileMachine` stop an idle bridge first and refuse while the open file has unsaved changes.

Nothing runs stale code. Every tool checks the server's own loaded source files and fails with "reconnect with /mcp" once any has changed on disk. Every bridge call checks the Blender-side source files: when they have changed and the open file is saved, Blender restarts on the new code and reopens the file; with unsaved changes, only `saveFile`, `newFile`, `openFile`, and status calls run until the work is saved or discarded. Renders rebuild their preview scene each time, so they always reflect the current scene.

| Tool | Does |
|---|---|
| `runPython` | Fallback only: runs code in a persistent namespace (`bpy`, `bmesh`, `mathutils`, `math`) and returns printed output and `result` if the code sets it. Every call is logged to `logs\runPython.log`; `getToolingStatus` counts them |
| `newFile` / `openFile` | Start an empty scene or open a .blend by absolute path; refused while the open file has unsaved changes unless `discardUnsavedChanges` |
| `saveFile` | Saves, or saves as an absolute path; textures and linked libraries become paths relative to the .blend; packed, generated, missing, or other-drive images are refused |
| `getSceneSummary` | File status, zone properties, objects (type, location, dimensions, triangles, materials), collections, cameras, materials with their textures, images |
| `setZoneProperties` | Stores the zone's EQ properties in the .blend, all required to render: fog color, fog start and end (the end is also the far clip), sun azimuth and elevation, sun color and strength, ambient color, and `newEngineZone` (the zone header's `NewEngineZone`, which sets the scale spawns draw at) |
| `renderView` | Renders the EQ preview of a view and returns the PNG inline; the file is kept under `%LOCALAPPDATA%\zonewright\renders` |
| `pick` | For a pixel of a view, the object hit, world position, surface normal, material, and distance |

### Artist toolkit

Edits address parts of a mesh with selectors instead of an interactive selection: `{"all": true}`, `{"sphere": {center, radius}}`, `{"box": {minimum, maximum}}`, `{"cylinder": {center: [x, y], radius, bottom, top}}`, `{"facing": {direction, withinDegrees}}`, `{"material": name}`, `{"vertexGroup": name}`, `{"insideObject": closedMeshName}`, and `{"and": [...]}`, `{"or": [...]}`, `{"not": selector}`. Shapes test vertex positions in world units, or face centers for face operations. A selector that matches nothing is an error.

| Activity | Tool | Does |
|---|---|---|
| Blocking out | `createPrimitive` | Plane, grid, cube, cylinder, cone, or sphere built to an exact bounding size, origin at its base center (center for flat shapes) |
| | `createTerrainGrid` | Flat grid with a vertex every `spacing` units, ready to sculpt |
| | `transformObjects` | Relative or absolute location, rotation, and scale |
| | `duplicateObjects` / `deleteObjects` | Copies (optionally sharing the mesh) and deletions |
| | `organize` | Renames, parents (world transform kept), collections |
| | `joinObjects` | Merges meshes into one object, such as a trunk and canopy into one tree |
| Shaping | `moveVertices` | Moves selected vertices, optionally fading with distance from a center: the fine-detail edit |
| | `sculptAtPoint` / `sculptAlongPath` | Raise, lower, crease, smooth, flatten around a point or along a path; carve cuts down to a path's heights through a cross-section profile, sliding the vertices just outside the cut onto the rim contour (`conformRim`) |
| | `deleteFaces` | Removes selected faces, such as the terrain inside a rock that forms its own cave floor |
| | `extrudeFaces`, `insetFaces`, `bevelEdges`, `subdivide` | Topology edits on selected faces or edges |
| | `booleanCut` | Cuts openings with a cutter mesh; refuses a cut that would erase the mesh |
| | `decimate` / `cleanupMesh` | Triangle reduction; merging, degenerate removal, and consistent normals |
| Surfacing | `createMaterial` | Diffuse texture, optional normal map, no shine; optional alpha-tested cutout |
| | `assignMaterial` | Material on selected faces |
| | `projectUVs` | Planar or box projection at a set number of world units per texture repeat |
| Dressing | `placeOnSurface` | Drops objects onto the surface below, optionally aligned to its normal |
| | `scatterInRegion` | Spaced, linked copies over a circle or polygon by density, with yaw and scale ranges, a slope limit, and objects to keep clear of; deterministic per seed |
| | `placeSpawn` | An EverQuest character drawn at the client's scale for its height in the zone with the client's appearance rules, posed at any frame of the animations the client gives it, its origin `avatarHeight` above the ground; see the `render-spawn` skill |
| | `placeDoor` | An EverQuest door (any server-placed model: doors, lifts, teleport pads, books, furniture) at its position and scale; see the `render-door` skill |
| | `placeObject` | An EverQuest ground object (kilns, looms, dropped items, housing pieces) at its position and scale; see the `render-object` skill |
| | `markAsset` / `linkKitAsset` | Marks kit collections as assets; links and places them from a kit .blend |
| Inspecting | `getObjectDetail` | Transform, bounds, counts, faces per material, UV density, modifiers, vertex groups |
| | `measure` | Surface heights, distances, height changes, and slopes between points |

A view is `{"camera": name}`, `{"eye": [x,y,z], "target": [x,y,z]}`, or `{"standAt": [x,y,z], "headingDegrees": h, "pitchDegrees": p}`. Heading 0 looks along +Y and turns clockwise seen from above; positive pitch looks up. `standAt` finds the ground by casting down from just above the point, so it works inside caves, puts the eye 5.5 units above it, and stands a scale figure ahead: the client's own dark elf female at the race-default height 5, drawn at the client's scale for the zone's `newEngineZone`, walked up to 15 units along the ground like a player (walls, drops, and climbs stop her) and facing the camera.

The EQ preview renders the open scene's objects (its own lights and cameras excluded) in a temporary scene: EEVEE without ray tracing, GI, or bloom; one shadowed sun and uniform ambient, both with no specular; linear distance fog composited from the mist pass, with the far clip at the fog end; 52 degree vertical field of view; 960 x 540. Output is byte-identical for identical input. These are starting values to calibrate against client screenshots.

## EverQuest reference

`EVERQUEST_CLIENT` in `.mcp.json` points at an EverQuest client install. The `zone-survey` skill (`.claude/skills/zone-survey`) describes how to answer zone questions from the survey. Scale and layout come only from the actual zone files: classic WLD (`.s3d`), EQGZ (`.zon` with `.ter`/`.mod` models, including archives named in `<zone>_assets.txt`), and EQTZP terrain (`.zon`/`.dat`). Brewall map files (`maps\Brewall`) are not authoritative geometry; they serve only as place names for design notes.

| Tool | Does |
|---|---|
| `surveyZones` | Technical lane of the zone survey: measured groups (`dimensions`, `surfaces`, `verticality`, `content`, `regions`) for the named zones or all of them, sorted by any numeric field. Cached per variant by source-file SHA-256 and per-group version, so changing one group's method recomputes only that group. As in git, a file is re-hashed only when its size or modification time changes (`verifyHashes` forces it), and zone discovery is reused until the client folder's listing changes. Stale zones are measured in parallel worker processes: a full cold survey takes about 80 s, a warm one 0.2 s. A variant whose files cannot be parsed is reported with its error |
| `getZoneSurvey` | Every survey group for one zone, both lanes (measured and interpreted) |
| `getZoneNotes` | Lists a zone's Brewall labels: text, map position, and layer file |
| `findModel` | Where the client finds a model in a zone: every definition by link tier, unlinked archives that also define it, and the one placement uses (see Client models) |

### Client models

Spawns, doors, objects, and the scale figure come from the client's own models and animations, found the way the client finds them: only in archives the client loads in the zone, never by matching a name in an unrelated archive. The client keeps the first definition of any name it registers (`EQGraphicsDX9.dll` skips a name already present), so the order in which it loads archives decides between archives that define the same name. `findModel` shows the whole search for one model. The order, read from `eqgame.exe`:

1. **At startup** (`0x491c20`):
   - `Resources\GlobalLoad.txt` phases 1 and 2.
   - The Luclin player models, `global<code>_chr2` then `global<code>_chr`, for races 1-12, 128, and 130, male then female, when `eqclient.ini` turns that race's Luclin model on. The human and wood elf models also load when a race that borrows their animations (erudite; dark, half, and high elf) has its Luclin model on.
   - `Global5_chr2`, `Global5_chr`, and `frog_mount_chr` while `UseLuclinElementals` is on.
   - The Luclin equipment (`LGEquip_amr2`, `LGEquip_amr`, `LGEquip2`, `LGEquip`) once any Luclin model loaded.
   - `VEquip` with the Luclin Vah Shir, or `GEquip6` and `Global7_chr` unless both Vah Shir settings are on.
   - `GlobalLoad.txt` phases 3 and 4.
   - `Resources\GlobalLoad_chr.txt`.
2. **On entering the zone** (`0x49b200`):
   - `<zone>_pre_chr.txt`, and `poknowledge_obj3.eqg` for the Plane of Knowledge only.
   - For classic zones, `<zone>_obj2`, `<zone>_obj`, the zone, `<zone>_2_obj`, `<zone>_chr2`, `<zone>2_chr` (only for the zones `eqgame.exe` names), and `<zone>_chr`.
   - `<zone>_chr.txt`. Each line is `code,source`: a source that starts with the code loads `source.eqg` (else `source.s3d`); any other source loads only that code's actor from `source.s3d`.
   - The `.eqg` archives in `<zone>_assets.txt`, and, for EQG zones, the zone `.eqg`.
3. **When first needed:** `Resources\OnDemandResources.txt` EQG models (`EQGM`) and skinned models (`EQGS`).

The first definition in that order wins: IT67 is in both `equipment-01.eqg` and `gequip.s3d`, and the client uses `equipment-01.eqg`, which `GlobalLoad.txt` loads first. `source` picks a definition by archive instead. Of two `OnDemandResources.txt` lines naming one resource, the first registers: `EQGraphicsDX9.dll` skips a name already registered with the same type (`0x100c74f0`). One archive defining a model twice is an error, since which entry the client takes is not known. Textures come from the model's own archive or, for a zone's EQG model, from the zone's other EQG archives. A texture none of them holds is missing for the client too: its faces draw magenta and every placement lists it in `missingTextures`.

Supported models:

- EQG models (`.mod`): static, or skinned when the file stores bones (Drakkin and many later creatures).
- EQG skinned piece models (`.mds`).
- WLD static actors.
- WLD skeletal actors: skinned meshes and bone-attached meshes. Particle clouds are counted, not drawn.

Characters take the client's appearance rules (from `EQGraphicsDX9.dll`):

- **Pieces:** the body piece `<code><nn>` for `variation` and the head piece `<code>HE<nn>` for `headType`, keeping the default when the model lacks the piece.
- **Texture sets:** `.lay` layers `C_<code>_S<set>_M<n>` for `.mds` models, and `<code><part><set><nn>_MDF` materials for WLD ones.
- **Hair:** `hairStyle` attaches `<code>_HAIR_<nn>` (`eqgame.exe` `0x40ac80`, called for the head slot when no helm is worn) where the client defines one, as for Drakkin.

A WLD character plays the client's animations (`eqAnimations.py`, transcribed from `eqgame.exe`):

- **The animations:** the 78 the client loads per model (`C01`-`C11` combat, `D01`-`D05` damage, `L01`-`L12` movement, `O01`-`O03` idle, `P01`-`P09`, `S01`-`S29` social, `T01`-`T09`), with the client's own labels such as `WALK` and `WAVE`.
- **Borrowing:** a model without an animation borrows another code's (`0x406a60`): dark, high, and half elves the wood elf's, Luclin erudites the human's, kobolds the werewolf's, and about a hundred more rules. The model's own animation comes first (`0x407800`).
- **Variants:** a Luclin model (one with a `<code>TUNIC_POINT_DAG` bone) takes lettered variants such as `L01A` and `L01B`; it takes an unlettered animation only if it has at least 50 tracks.
- **Frames:** each frame decodes as the client does (`0x1001b190`): rotation, translation over 256, and a uniform scale over 256. Bones without a track in the animation keep their bind transform.
- **Duplicate tracks:** of two tracks with one name in a file, the first plays. `EQGraphicsDX9.dll` registers tracks in file order, and `d3dx9_30.dll`'s `RegisterAnimationSRTKeys` refuses a name already registered (the DLL logs the failure and goes on).
- **Stand:** a spawn stands at frame 0 of `P01` (STAND STILL) unless another animation is chosen. A model with no stand animation stands in its bind pose.

An EQG character plays the client's EQG animations:

- **Animation ids:** `eqgame.exe` keeps one table of 104 animation ids (`0xaae3e8`). Each id has an EQG name and the WLD animation it plays on a WLD model (`0xaaf428`): `/wave` is id 75, `WAVE` on an EQG model and `S03` on a WLD one.
- **Requests:** a request may give an EQG name (`STND`, `NRUN`), a WLD code, or a label. A code plays the first id mapped to it, so the default `P01` plays `STND`; an EQG name on a WLD model plays its id's code.
- **Lookup:** an EQG model plays `<name>_BA_1_<code>` (`EQGraphicsDX9.dll` `PlayAnimation`, `"%s_BA_1_%s"`), from the first archive in the load order that defines it, else from the first `OnDemandResources.txt` line naming it.
- **Files:** `.ani` animations hold, per bone, keys of time, translation, rotation, and scale. Skinned `.mod` and `.mds` files hold 56-byte bones (parent links and bind transform) and one 36-byte weight record per vertex (up to four bones). Every such file in the client parses exactly to its size.
- **Bones:** the DLL transposes each quaternion's matrix (`0x1003efc8`), a convention under which weighted vertices sit beside their bones. A vertex moves by its weighted bones from bind to pose.
- **Footing:** the DLL lowers `ROOT_BONE`'s keys by the model's `ROffset` (`0x1003cd5b`, every animation but `_MT_` ones). An animated EQG model's feet therefore sit `ROffset` below its origin, like a WLD model's.
- **Attached pieces:** a piece such as hair carries a subset of the skeleton's bones. It takes the skeleton's pose by bone name; a weighted bone that binds elsewhere than the skeleton's is an error.

### Spawn size

Spawns take the dumps' and the server's terms: `height` (EQEmu's size), `avatarHeight`, and `heading`. `eqgame.exe` `0x5a3f40` turns a spawn's height into the scale it draws the model at:

| Model | Zone without `NewEngineZone` | Zone with `NewEngineZone` |
|---|---|---|
| WLD | height / 5 | height / 6.5 |
| EQG | height * 1.3 / 6 | height / 6 |

- **EQG models:** the codes whose race `eqgame.exe` registers with flag 8. `eqRaces.py` holds all 859 of its race registrations (`0x50a440`).
- **`NewEngineZone`:** the zone header's flag (offset 692 of the RoF2 zone packet). The live dumps' `zoneHeaders` give it per zone; EQEmu sends false for every zone. `setZoneProperties` stores it with the zone.
- **`avatarHeight`:** how high the client stands the model origin above the ground: `Resources\moddat.ini`'s `ROffset` for the model (3.125 when it has none) times the scale. Luclin models' standing feet sit about 3 units below their origin.

The formula reproduces the `avatarHeight` of 16,926 of the 17,291 live-dump spawns whose model this client registers. The other 365 are races RoF2 registers as EQG models and live draws at WLD scale (`CAT`, `HLG`, `I25`, ...).

All four placement paths (`placeSpawn`, `placeDoor`, `placeObject`, and the scale figure) build through the same code. A model is indexed and built once:

- **Index:** `models\modelIndex.json` lists every archive's models and animations, rebuilt when the client listing changes.
- **Built models:** `models\built\<model>@<archive>[@appearance][@pose]`, rebuilt when the client's files change.

Placements take Blender values (`location`, `headingDegrees`) or EQ's (`x`, `y`, `z`, and `heading` in 512ths of a turn, as the server and the dumps give them). `eqgame.exe`'s heading toward a point (`0x4ef250`, which `/face` stores as the player's heading) is 0 toward +y and 128 toward +x. Through the axis swap, that is a turn of +heading about Z for a model whose front is +X. Measured doors confirm there is no quarter-turn offset. A client screenshot confirms the rest:
- **Layout:** +y is north and +x is west.
- **Christine:** the neighborhood's Housing Information NPC stands where the dump puts her relative to the camera. At the dump's height 6 she draws 6.0 units tall, about what the screenshot shows. Her heading 246 faces her square to the camera, as the client draws her; a turn of -heading would rotate her 17 degrees away.
- **Palatial Guild Hall:** seven characters, rendered from the dump of the moment the screenshot was taken, land where the screenshot shows them and face the same ways. Among them are a Drakkin (EQG), barbarian, Vah Shir, dark elf, Iksar, and gnome. The Drakkin faces as the screenshot shows, so EQG models take no quarter turn either.
