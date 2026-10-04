---
name: review-zone
description: Review a zone pass in pictures against the zone checklist (one noise scale everywhere, repeating textures, grid-like scatter, same-size props, floating or sunk objects, wrong scale against the player, mushy silhouettes, no landmark, colors drifting from the art, too shiny, dense, or realistic for 2011), and have the zone-critic agent judge the renders with fresh eyes. Use after every authoring step and before showing zone work to the owner.
---

# Reviewing a zone

A review judges what a player would see. Render it, look at every picture, name what reads wrong and where, fix it, and look again. The checklist below is what to look for; the zone-critic agent applies it to the renders without seeing the code, the tools, or your reasoning, so it judges the pictures as they are.

## Render the review

1. **Review cameras.** Each area keeps saved review cameras (`saveReviewCamera`): its zone-in or approach at eye level, an oblique from above, each landmark, and each place the concept art shows (`compareToConcept` with `saveAs`, matched to the art). `renderReviewSet` renders them all on one sheet; give each its note, what to judge there.
2. **Routes.** The main routes are saved review routes (`saveReviewRoute`); `renderRouteStrip` walks each and frames it at eye level, with a frame at every problem the walk meets.
3. **The plan.** A `renderView` map in layout shading, and `renderSketch` over it with the regions' names, for where things are.
4. **The concept.** `compareToConcept` with each concept camera: side by side, the overlay, the squinted masses, the edges, and the palettes, with numbers.
5. **Diagnostics, where a question needs them.** `renderView` shadings: `curvature` (broad planes, crisp lips, one noise scale everywhere), `texelDensity` and `triangleDensity` (against a client reference zone rendered the same way), `coverage` (export status), `objects` and `labels` (naming what a view shows).

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

### Scale, routes, and composition

| Problem | What it looks like | Where it shows |
|---|---|---|
| Wrong scale against the player | Doors, steps, paths, ledges, plots, or props too big or too small beside the scale figure (5 units tall) | Eye-level views with the figure; review cameras that keep her |
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
- reference screenshots of client zones of the same kind (`getZoneSurvey` interpretations' screenshots or `importZone` views), with what each shows;
- each render's path, its view name, its note (what to judge there), and its view (the `view` renderView took), so it can `pick` what it names.

Never give it your reasoning about the work, what you expect it to find, or what you already fixed. A fresh critic runs each time, so it is never anchored by its own last verdict.

It returns, per view, `pass` or its problems, each with the checklist item, what is wrong, where (in the picture, and the object or scene position it picked), and severity. Fix what it names, render the same cameras again, and run a new critic on them. Where you judge it wrong, look at that spot again yourself before setting it aside; a problem you leave stands until the owner agrees.
