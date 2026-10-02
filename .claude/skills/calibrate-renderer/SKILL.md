---
name: calibrate-renderer
description: Check and tune zonewright's EQ preview against live client screenshots, so renders look like the client. Use when a new calibration screenshot arrives, after changing the renderer (bridgeClientLight, bridgeViews, zone or model reading), or when a render and the client disagree.
---

# Calibrate the renderer

The preview draws what the client draws (`docs\clientRendering.md`): unlit surfaces emitting texture times the light the client's vertex shader computes, fogged exponentially squared, through a camera measured against screenshots. `calibrateShot` measures how far a render is from a real screenshot and tracks it.

## Screenshots

The owner keeps live client screenshots in `C:\Games\Steam\steamapps\common\Everquest F2P\Screenshots`, named `<zone>,<loc y>,<loc x>,<loc z>,<heading>,<pitch>.jpg`:

- `<zone>` is the owner's best name for the zone, not always the client's file name. Map it to the client zone: `ewastes` is `eastwastes`; `neighborhood` is the EQ terrain zone `neighborhood` (header fog 1500-6000, color 200, 200, 200, `NewEngineZone` 1); `pallatialguildhall` (server `guildhalllrg_int`) has no interior in this client, so it cannot be calibrated against zone geometry.
- `/loc` order is EQ y, x, z: the scene's x, y, z.
- `<heading>` is compass degrees clockwise from north (the scene's +X); `<pitch>` is in 512ths of a turn, positive up.
- A name without all six fields is not ready yet. Check the folder now and then, not often.

## calibrateShot

| Argument | Meaning |
|---|---|
| `screenshotPath` | The screenshot's full path |
| `zone` | The client zone file name (`poknowledge`, `eastwastes`) |
| `newEngineZone` | The zone header's `NewEngineZone` (the live dumps' `fields\zoneHeaders.tsv`) |
| `fogColor`, `fogStart`, `fogEnd`, `fogDensity` | The zone header's fog: the first of `zoneHeaders.tsv`'s four fog sets, `fogDensity` 0 when its `FogOnOff` is 0, else the client's 0.33. Give all four or none: with none (a zone the dumps lack, like Eastern Wastes), the fog is fitted with the light |
| `recordingPath`, `liveDumpsPath` | A live behavior recording of the zone that was running when the screenshot was taken (`mqLive\build\bin\release\Logs\peridotLiveBehavior_<server>_<zone>_<instance>_<character>_<start>.txt`) and the live dumps' `master` folder: NPCs, doors, ground items, and placed objects within 1000 units of the camera are placed as they stood at the screenshot's file time (see `placeRecording`). Give both or neither |
| `discardUnsavedChanges` | It opens a new file; this discards the open one's unsaved changes |

It imports the zone, renders the screenshot's view as passes, fits the scene light (ambient, sun, bounce, sun direction, and the fog when not given) that best explains the screenshot, renders with it, and returns the screenshot beside the render. The result gives the fit, its `residual` (the root mean square color error it could not explain, 0-1), and `meanPixelDifference` (0-255 over drawn pixels). Each run is kept under `%LOCALAPPDATA%\zonewright\calibration\<screenshot>\<time>` with its passes, comparison, and `result.json`; `getToolingStatus` lists every screenshot's latest run, so a renderer change shows as a better or worse difference.

## Reading a comparison

- **Shifted or scaled scene:** the camera. Its constants (`eyeAboveLoc`, `pitchOffsetDegrees` in `eqCalibration.py`, `verticalFieldOfViewDegrees` in `bridgeViews.py`) come from aligning renders to screenshots; re-measure them with several shots before changing them, and note an outlier shot rather than bending the constants to it.
- **Wrong overall light:** the time of day differs between shots; the fit measures it per shot. Shots of one zone a few minutes apart should fit alike.
- **Local glows, sky, distant haze:** the sky and point lights are not drawn yet, and fog without the zone header's values is a guess (see the to-do list in `docs\clientRendering.md`).
- **Missing props, people, and grass:** without a recording, doors, ground items, and spawns are not placed (the neighborhood's info board and teleport arch are doors). With one, NPCs still lack their equipment, live-only models and looks are listed in the result's `recorded.notPlaced`, and some live textures differ from this client's (the neighborhood's arched gate draws as bars). An EQ terrain zone's radial flora is not drawn. All of these count against the difference.
- **Magenta:** a texture no archive holds, as for the client.
