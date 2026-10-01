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

| Tool | Does |
|---|---|
| `getToolingStatus` | Reports Blender (`missing`, `broken`, `versionMismatch`, `installed`), each pinned extension (`missing`, `versionMismatch`, `installed`), and installed extensions that are not pinned |
| `syncTooling` | Installs the pinned Blender after verifying its SHA-256, removes other Blender versions, and installs, upgrades, or removes extensions to match the manifest. Fails if Blender is running from the tooling root, or if the pinned install is `broken` or `versionMismatch` (clear it by hand) |
| `addExtension` | Pins the newest extensions.blender.org release of an extension compatible with the pinned Blender, then syncs. Fails if a different `version` is requested |
| `removeExtension` | Unpins an extension, then syncs to uninstall it |

## Scale

Blender scenes are authored at **1 Blender unit = 1 EQ unit**, Z up, so values in Blender match `/loc`, client models, and zone files directly. A player is about 6 units tall: the client's Drakkin male mesh (`dkm.mod`) stands 5.96 units, and EQEmu gives human males a default size of 6.0. Eye-level views put the eye 5.5 units above the ground.

EQGZI's Blender exporter (`xackery/eqgzi` `out/convert.py`) works at 1 Blender unit = 2 EQ units and writes placements as EQ = (-Blender.y, Blender.x, Blender.z) x 2. Phase 2 export applies that conversion; nothing in Phase 1 does.

## Blender bridge

The first tool that needs Blender starts the pinned Blender headless (`--background --factory-startup`, portable config) running `server/bridgeMain.py`, which enables the pinned extensions and serves commands over 127.0.0.1 on a random port with a per-session token. Commands run one at a time on Blender's main thread; errors come back with their traceback. If Blender exits, the next call reports the exit code and Blender's last output, and the call after that starts a fresh Blender. `syncTooling`, `addExtension`, and `removeExtension` stop an idle bridge first and refuse while the open file has unsaved changes.

| Tool | Does |
|---|---|
| `runPython` | Runs code in a persistent namespace (`bpy`, `bmesh`, `mathutils`, `math`); returns printed output and `result` if the code sets it |
| `newFile` / `openFile` | Start an empty scene or open a .blend by absolute path; refused while the open file has unsaved changes unless `discardUnsavedChanges` |
| `saveFile` | Saves, or saves as an absolute path; textures and linked libraries become paths relative to the .blend; packed, generated, missing, or other-drive images are refused |
| `getSceneSummary` | File status, zone properties, objects (type, location, dimensions, triangles, materials), collections, cameras, materials with their textures, images |
| `setZoneProperties` | Stores the zone's EQ preview properties in the .blend: fog color, fog start and end (the end is also the far clip), sun azimuth and elevation, sun color and strength, ambient color |
| `renderView` | Renders the EQ preview of a view and returns the PNG inline; the file is kept under `%LOCALAPPDATA%\zonewright\renders` |
| `pick` | For a pixel of a view, the object hit, world position, surface normal, material, and distance |

A view is `{"camera": name}`, `{"eye": [x,y,z], "target": [x,y,z]}`, or `{"standAt": [x,y,z], "headingDegrees": h, "pitchDegrees": p}`. Heading 0 looks along +Y and turns clockwise seen from above; positive pitch looks up. `standAt` finds the ground by casting down from just above the point, so it works inside caves, puts the eye 5.5 units above it, and renders a 6-unit scale figure about 15 units ahead.

The EQ preview renders the open scene's objects (its own lights and cameras excluded) in a temporary scene: EEVEE without ray tracing, GI, or bloom; one shadowed sun and uniform ambient, both with no specular; linear distance fog composited from the mist pass, with the far clip at the fog end; 52 degree vertical field of view; 960 x 540. Output is byte-identical for identical input. These are starting values to calibrate against client screenshots.

## EverQuest reference

`EVERQUEST_CLIENT` in `.mcp.json` points at an EverQuest client install. The `zone-survey` skill (`.claude/skills/zone-survey`) describes how to answer zone questions from the survey. Scale and layout come only from the actual zone files: classic WLD (`.s3d`), EQGZ (`.zon` with `.ter`/`.mod` models, including archives named in `<zone>_assets.txt`), and EQTZP terrain (`.zon`/`.dat`). Brewall map files (`maps\Brewall`) are not authoritative geometry; they serve only as place names for design notes.

| Tool | Does |
|---|---|
| `surveyZones` | Technical lane of the zone survey: measured groups (`dimensions`, `surfaces`, `verticality`, `content`, `regions`) for the named zones or all of them, sorted by any numeric field. Cached per variant by source-file SHA-256 and per-group version, so changing one group's method recomputes only that group. A variant whose files cannot be parsed is reported with its error |
| `getZoneSurvey` | Every survey group for one zone, both lanes (measured and interpreted) |
| `getZoneNotes` | Lists a zone's Brewall labels: text, map position, and layer file |
