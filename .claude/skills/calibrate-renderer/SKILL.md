---
name: calibrate-renderer
description: Check zonewright's EQ preview against shots of Claude's spare RoF2 client taken through claudelink, so renders look like the client. Use when a calibration shot is wanted or arrives, after changing the renderer (bridgeClientLight, bridgeViews, zone or model reading), or when a render and the client disagree.
---

# Calibrate the renderer

The preview draws what the client draws (`docs\clientRendering.md`): unlit surfaces emitting texture times the light the client's vertex shader computes, fogged exponentially squared, through the RoF2 client's own camera. `calibrateShot` predicts a shot of the client from the dump taken with it and measures how far the prediction is from the shot; `compareShot` does the same for a shot of a zone only the spare holds, or of an export, from the open work file.

Calibration takes only claudelink shots of the spare RoF2 client. Older shots (the owner's live Steam screenshots, the Peridot client's `EQ0000xx` shots taken through EQBCS) are not used.

## Taking a shot

The spare client runs through the claudelink MCP (`C:\Users\Toshiro\git\github\PhoenixCampfire\claudelink`, its README). Keep the spare's viewport as its UI file sets it; `calibrateShot` crops to it.

1. `launchClient` with the spare's character, then `state` to see where it stands.
2. `zone` to the zone, `teleport` to the spot (`/loc` order, y x z) with the heading, and `face` for the pitch.
3. `command` `#set weather 0`: weather is not read from the client, so every calibration shot is taken clear, and runs record that condition.
4. `time` to the hour (and minute) the shot is for.
5. `screenshot` to a path under `%LOCALAPPDATA%\claudelink\screenshots`, with the UI hidden. It writes the PNG, its sidecar `<shot>.json`, and the zone dump read in the same frame, at 100 fps with MacroQuest's frame limiter suspended.
6. `calibrateShot` with the PNG's path; `compareShot` for a zone the client lacks, or an export.

Plan shots as a set before tuning anything: several zones, times, and spots per question, each settled by more than one shot.

## calibrateShot

| Argument | Meaning |
|---|---|
| `screenshotPath` | The shot's PNG; its sidecar sits beside it and names its dump |
| `fitLight` | Also fit the ambient, sun, and bounce colors and sun direction that best explain the shot, beside the sky's prediction (a diagnostic) |
| `discardUnsavedChanges` | It opens a new file; this discards the open one's unsaved changes |

It refuses a shot whose sidecar and dump do not pair (paths and capture time), one taken under 100 fps or with MacroQuest's frame limiter throttling, and one whose PNG is not the client area the sidecar records.

From the dump it takes, with nothing fitted:

- the zone, imported as `importZone` does (lights, emitters, zone lines);
- the camera exactly: eye, heading, and pitch, and a frame at the viewport's aspect whose field of view the camera's half view angle and `viewAngleRatio` give, checked against the projection scales the client computed (a rolled camera is refused);
- the sky at the dump's game time, and the zone header's first fog set, density, fog switch, clips, `newEngineZone`, short name, and id (a zone without a sky, a zone type whose ambient is a fixed gray, or a clip slider short of its maximum is refused: previews do not draw those yet);
- the light the viewer carries, its type's color and reach at the position the client put it;
- the viewer's special ambient, from its actor's share of scene light (a race with infravision or ultravision is refused);
- every spawn but the viewer, visible door, ground item, and placed object that could show in the frame, at its actor's position, heading, and scale (`placeDump`).

It returns a sheet: the shot cropped to the viewport beside the render, over their difference amplified 4 times and a map of the difference per 32-pixel block (black 0, through red and yellow, to white at 64 of 255). The result gives the mean difference (of 255) over the zone's drawn pixels (`meanPixelDifference`), over the sky, and over all, the block map's median and largest, what was placed and why anything was not, and with `fitLight` the fitted light and the prediction's, each with the residual it leaves. Each run is kept under `%LOCALAPPDATA%\zonewright\calibration\<shot>\<run>` with the shot crop, render, difference, block map (`blocks.npy` and `.png`), sheet, and `result.json`, which records the conditions (game time, weather, the carried light, the viewer's share, settings) and the renderer's source hash. `getToolingStatus` lists every shot's latest run.

## compareShot

For a zone the client lacks (one only the spare holds, such as a zone deployed through the ledger) or an exported archive, which `calibrateShot` cannot import. The subject is the open work file, saved with no unsaved changes (refused otherwise, naming `saveFile`), whose `shortName` must be the dump's zone; or with `archivePath` an exported `<zone>.eqg` whose stem is the dump's zone, brought into a new file as `importZoneFile` brings it.

| Argument | Meaning |
|---|---|
| `screenshotPath` | The shot's PNG, as `calibrateShot` takes it, with the same refusals |
| `archivePath` | An exported `.eqg` to draw instead of the open scene (absolute, existing, its stem the dump's zone) |
| `fitLight` | As `calibrateShot`'s |

The work-file route hides everything a zone export leaves out (reference zones, `importZoneFile` references, plot borders, placed spawns, guides), as the client never draws them, and keeps what the dump places (in `<zone> dump`, its models from the zone's own archives when the client has them, else from its startup archives); the archive route draws the archive as `verifyExport`'s archive views do. The dump's values drive the render, as in `calibrateShot`: the clock, the header's fog and clips, the sky type the client resolved, the viewer's special ambient and carried light. The work file's stored values are never applied and never rewritten; where its sky type, `fogStart`, `fogEnd`, `fogDensity`, `fogOn`, `minClip`, `maxClip`, `newEngineZone`, or `zoneId` differ from the dump's, the result's `source.zoneDrift` lists each as `{stored, dump}` (empty when none differ), a report to read, never a refusal: a drift says the file and the deployed zone row disagree, not that the measurement is wrong. The run is kept beside `calibrateShot`'s under `calibration\<shot>\<run>`, its `result.json` carrying `source` (`kind` `workFile` or `archive`, `path`, `zoneDrift`), and `getToolingStatus` names each shot's latest run's source. When the run ends, whatever happened, the saved work file is opened again, so no placement, property change, or unsaved flag survives.

## Reading a comparison

Look at the sheet first; the numbers only say how much.

- **Shifted or scaled scene:** the camera or the frame. The dump's camera is the client's own; a shift points at how the view is built from it (`eqCalibration.cameraView`, `clientFrame`), never at a constant to nudge.
- **Wrong overall light:** the sky at the game time, the special ambient, or the zone type's ambient rule. `fitLight` shows which way the light differs; noon light clamps on most sunlit faces, which leaves the sun and bounce colors unmeasured there.
- **Warm light near the camera:** the viewer's carried light, drawn where the dump puts it.
- **Local glows, sky, distant haze:** point lights and environment emitters draw with the rules `docs\clientRendering.md` lists as not settled; the sky's clouds, stars, and backdrop ring do not.
- **Missing props and people:** spawns draw without equipment; anything the client data cannot draw is listed in `notPlaced`. An EQ terrain zone's radial flora is not drawn.
- **Black faces on a model:** a texture no archive holds, which previews draw black.

Record what a planned set of shots shows in `docs\renderingWorklist.md` (Standing) and in `docs\clientRendering.md` with the shots that show it. Do not tune a constant to one shot.
