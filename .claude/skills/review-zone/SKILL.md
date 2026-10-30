---
name: review-zone
description: Review a zone pass in pictures against the zone checklist (one noise scale everywhere, repeating textures, grid-like scatter, same-size props, floating or sunk objects, wrong scale against the player, mushy silhouettes, no landmark, colors drifting from the art, too shiny, dense, or realistic for 2011), and have the zone-critic agent judge the renders with fresh eyes. Use after every authoring step and before showing zone work to the owner.
---

# Reviewing a zone

A review judges what a player would see. Render it, look at every picture, name what reads wrong and where, fix it, and look again. The checklist below is what to look for; the zone-critic agent applies it to the renders without seeing the code, the tools, or your reasoning, so it judges the pictures as they are.

## Render the review

1. **Review cameras.** Each area keeps saved review cameras (`saveReviewCamera`): its zone-in or approach at eye level, an oblique from above, each landmark, and each place the concept art shows (`compareToConcept` with `saveAs`, matched to the art). A camera keeps an hour (`sky`) where the light matters, such as a night camera at a landmark, a lamp-lit street, or a pool by moonlight, and renders at that hour wherever it renders. `renderReviewSet` renders them all on one sheet, each at its own hour, or every camera at one hour with `sky` (a lit area at the zone's hour and at night); give each its note, what to judge there. Once the zone is deployed, the same cameras are shot in the client (below).
2. **Routes.** The main routes are saved review routes (`saveReviewRoute`); `renderRouteStrip` walks each and frames it at eye level, with a frame at every problem the walk meets; a route lit for the night is walked at that hour too (`sky`).
3. **The plan.** A `renderView` map in layout shading, and `renderSketch` over it with the regions' names, for where things are; the same map in `slope` shading, or `renderSketch` with the `slope` layer, for what is level, what is a bank, and what is a wall.
4. **The concept.** `compareToConcept` with each concept camera: side by side, the overlay, the squinted masses, the edges, and the palettes, with numbers.
5. **Diagnostics, where a question needs them.** `renderView` shadings: `curvature` (broad planes, crisp lips, one noise scale everywhere), `texelDensity` and `triangleDensity` (against a client reference zone rendered the same way), `coverage` (export status), `slope` (ground reached only at the client's limit, banks over the design grade on routes; a `lane` view tints such floor climb, violet beside walk green), `objects` and `labels` (naming what a view shows).

## Shooting the review cameras in the client

Once the zone is deployed (`deployZone`) and the spare client holds the zone's files, every review camera is shot in the client from the same place and set beside its render, so the critic judges what the client draws, not only the preview. Which side holds what: the pose, the comparison, and the record are zonewright tools (`getReviewCameras` lists each camera's `clientPose`, `compareShot` compares a shot with its camera, `saveReviewCamera` keeps a shot as a camera); the client is driven through the claudelink MCP's own tools (`launchClient`, `zone`, `command`, `teleport`, `face`, `time`, `state`, `screenshot`), called from this procedure, since the two servers never call each other.

1. `launchClient` with the spare's character, then `zone` to the zone.
2. `command` `/keypress FIRST_PERSON_CAMERA` once, so the camera is the eye, and `command` `#set weather 0`, since the client's weather is not read.
3. `getReviewCameras`: each camera's `clientPose` is the spare's first-person pose that puts the client's camera where the review camera stands: `teleport` {y, x, z} in the client's /loc order, `face` {heading, pitch}, and `time` {hour, minute} (None for a zone drawing no sky). A camera with `clientPose` None cannot be shot; `clientPoseRefused` says why.
4. Per camera, from its `clientPose`: `teleport` (y, x, z, with `face.heading`), which flies, so a standAt camera's eye, lower than the client's eye on foot, is reached in the air; `face` (heading, pitch); `time` (hour, minute) when `time` is not None; `state`, and check the landing against the pose before the shot (the position within 2 of the teleport on each axis, the heading and look angle as sent); then `screenshot` with `hideUI` true. The PNG's path comes back in the result.
5. `compareShot` with `camera` and that path: the shot cropped to the viewport beside the render from the dump's own camera, the pose drift, and the numbers. A refusal naming an axis or angle off means the client put the character elsewhere (the ground, water, a wall): teleport and shoot again.
6. The critic gets each camera's shot beside its render.

A shot worth looking at again becomes a camera of its own: `saveReviewCamera` with `screenshotPath` keeps the dump's camera, renders at the shot's frame and hour, and lists the pose that took it.

## The checklist

Each item says what it looks like and where it shows. Severity: **blocking** breaks the illusion for any player (floating, holes, the void, wrong scale); **major** reads as generated or as wrong for the area's intent; **minor** is polish.

### Forms

| Problem | What it looks like | Where it shows |
|---|---|---|
| One noise scale everywhere | Bumps or ripples of one size across ground and rock alike; rock that wobbles instead of breaking into planes, ledges, and corners | Eye level and obliques against the light; `curvature` shading speckled evenly everywhere instead of broad planes with crisp warm lips and cool creases |
| Mushy silhouettes | Skylines, rims, and ledge lips soft and wavering where they should be crisp, stepped, and decided | Eye level looking up at rims against the sky; obliques along a cliff run |
| Forms left as made | Perfect circles, straight runs, symmetry, thin ledges at even offsets along a path, a terrace like a stamped shape | The plan in layout shading; obliques |
| Broken ground | Spikes, teeth along a ledge line, folds, slits, holes, the sky or the void seen through the ground, open seams at a cave mouth | Close eye-level views; `coverage` shading's blue where a face is seen from its back |

### Surfacing

| Problem | What it looks like | Where it shows |
|---|---|---|
| Visibly repeating textures | The same blotch or crack repeating in a grid across a wide surface; one material over a whole slope with nothing breaking it | Eye level across open ground and long walls; obliques from above |
| Grid edges and speckle | Material borders that zigzag along triangles or follow the grid, specks of one material inside another near a slope threshold | Obliques; the plan |
| Missing transitions | A hard seam where two grounds meet with no transition strip | Eye level at the border; `coverage` magenta |
| Stretched textures | Texture smeared down steep faces or squeezed into streaks | Eye level at cliffs; `texelDensity` streaks, `coverage` yellow |
| Colors drifting from the art | Hue, saturation, or value off from the concept; a palette the art does not have | `compareToConcept` palettes and color numbers, side by side with the art |

### Dressing

| Problem | What it looks like | Where it shows |
|---|---|---|
| Grid-like scatter | Props or plants in rows, at even spacing, or evenly everywhere instead of clustered where they would grow or be put | The plan; obliques; eye level along a row |
| Same-size props | Trees, rocks, or crates all one size, heading, and tilt | Eye level across a group |
| Floating or sunk objects | Daylight under a prop, or one buried past where it would sit | Close eye-level views at the contact; `labels` to name it |
| Intersecting objects | Props through each other, through walls, or through the ground at an angle | Close views from two sides |

### Lighting

| Problem | What it looks like | Where it shows |
|---|---|---|
| Night lighting wrong | Dark holes between lamps where a player would see nothing; the three-light seam across a floor (the client lights each terrain piece by three lights, so a fourth splits the floor at the piece's edge); a pool reading as a flat disc at night | Night renders: review cameras kept at a night hour, `renderReviewSet` with `sky` at night, and `renderRouteStrip` with `sky` along a lit route |

### Scale, routes, and composition

| Problem | What it looks like | Where it shows |
|---|---|---|
| Wrong scale against the player | Doors, steps, paths, ledges, plots, or props too big or too small beside the scale figure: a player of the default height as the client draws her in the zone, about 6.5 units tall where the zone lacks NewEngineZone (as EQEmu serves every zone) and 5 where it has it | Eye-level views with the figure; review cameras that keep her |
| No landmark anchoring the view | A view with nothing to orient by: no distinct silhouette, structure, fall, or light to steer toward | Zone-in, hub, and route views |
| Unreadable routes | Nowhere obvious to go; paths that vanish, dead ends without a reason, a climb that looks walkable and is not | `renderRouteStrip`; eye level at forks |
| The edge of the world | The rim below eye level, the void or bare sky under the horizon, the zone's end in plain sight | Eye level looking out from high ground and toward the edges |
| Composition off from the art | The big light and dark masses, the skyline, and the horizon placed unlike the concept's | `compareToConcept` masses, edges, and overlay |

### Era

| Problem | What it looks like | Where it shows |
|---|---|---|
| Too shiny, dense, or realistic for 2011 | Sheen, strong bump, photographic textures, tiny props in great numbers, geometry far denser than the client's | Eye level; `triangleDensity` and `texelDensity` against a client reference zone of the same kind rendered the same way; `compareWithClientZones` |

## The critic

After each authoring step (`author-zone`), and before showing work to the owner, have the zone-critic agent judge the review renders: start it with the Agent tool (`subagent_type` `zone-critic`) and give it only:

- the zone, and the step just done (what kind of work, which areas), in one line;
- the concept art's paths;
- the layout: the plan's path, and each region's name and intent;
- the zone's use: its housing role and intent (`getHousing`) and where its plots stand (each plot's address, centre, and size), so it can tell ground left open for players to build on from ground left bare;
- reference screenshots of client zones of the same kind (`getZoneSurvey` interpretations' screenshots or `importZone` views), with what each shows;
- each render's path, its view name, its note (what to judge there), its hour when it is not the zone's (the result's `sky`: a camera kept at night, or a sheet rendered at one hour), and its view (the `view` renderView took), so it can `pick` what it names;
- how tall the scale figure is drawn in the zone (see the checklist's scale item).

Never give it your reasoning about the work, what you expect it to find, or what you already fixed. A fresh critic runs each time, so it is never anchored by its own last verdict.

It returns, per view, `pass` or its problems, each with the checklist item, what is wrong, where (in the picture, and the object or scene position it picked), and severity. Fix what it names, render the same cameras again, and run a new critic on them. Where you judge it wrong, look at that spot again yourself before setting it aside; a problem you leave stands until the owner agrees.
