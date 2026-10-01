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

Blender scenes are authored at **1 Blender unit = 1 EQ unit**, Z up, so values in Blender match `/loc`, client models, and zone files directly. A player is about 6 units tall: the client's Drakkin male mesh (`dkm.mod`) stands 5.96 units, and EQEmu gives human males a default size of 6.0. Eye-level views put the eye 5.5 units above the ground.

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
| `setZoneProperties` | Stores the zone's EQ preview properties in the .blend: fog color, fog start and end (the end is also the far clip), sun azimuth and elevation, sun color and strength, ambient color |
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
| | `placeSpawn` | An EverQuest character model drawn at its EQ size, feet on the ground; see the `render-spawn` skill |
| | `markAsset` / `linkKitAsset` | Marks kit collections as assets; links and places them from a kit .blend |
| Inspecting | `getObjectDetail` | Transform, bounds, counts, faces per material, UV density, modifiers, vertex groups |
| | `measure` | Surface heights, distances, height changes, and slopes between points |

A view is `{"camera": name}`, `{"eye": [x,y,z], "target": [x,y,z]}`, or `{"standAt": [x,y,z], "headingDegrees": h, "pitchDegrees": p}`. Heading 0 looks along +Y and turns clockwise seen from above; positive pitch looks up. `standAt` finds the ground by casting down from just above the point, so it works inside caves, puts the eye 5.5 units above it, and stands a scale figure ahead: the client's own dark elf female at her normal size of 5 units, walked up to 15 units along the ground like a player (walls, drops, and climbs stop her) and facing the camera.

The EQ preview renders the open scene's objects (its own lights and cameras excluded) in a temporary scene: EEVEE without ray tracing, GI, or bloom; one shadowed sun and uniform ambient, both with no specular; linear distance fog composited from the mist pass, with the far clip at the fog end; 52 degree vertical field of view; 960 x 540. Output is byte-identical for identical input. These are starting values to calibrate against client screenshots.

## EverQuest reference

`EVERQUEST_CLIENT` in `.mcp.json` points at an EverQuest client install. The `zone-survey` skill (`.claude/skills/zone-survey`) describes how to answer zone questions from the survey. Scale and layout come only from the actual zone files: classic WLD (`.s3d`), EQGZ (`.zon` with `.ter`/`.mod` models, including archives named in `<zone>_assets.txt`), and EQTZP terrain (`.zon`/`.dat`). Brewall map files (`maps\Brewall`) are not authoritative geometry; they serve only as place names for design notes.

| Tool | Does |
|---|---|
| `surveyZones` | Technical lane of the zone survey: measured groups (`dimensions`, `surfaces`, `verticality`, `content`, `regions`) for the named zones or all of them, sorted by any numeric field. Cached per variant by source-file SHA-256 and per-group version, so changing one group's method recomputes only that group. As in git, a file is re-hashed only when its size or modification time changes (`verifyHashes` forces it), and zone discovery is reused until the client folder's listing changes. Stale zones are measured in parallel worker processes: a full cold survey takes about 80 s, a warm one 0.2 s. A variant whose files cannot be parsed is reported with its error |
| `getZoneSurvey` | Every survey group for one zone, both lanes (measured and interpreted) |
| `getZoneNotes` | Lists a zone's Brewall labels: text, map position, and layer file |
