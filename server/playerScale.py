"""The player's measures that geometry is judged by, each with its source in `sources`: "measured: how, where, when", "unmeasured:
why the value", or "derived: from what". Every tool that judges where a player stands, climbs, or looks takes them from here."""
import math

playerHeight = 6.0
walkableNormalZ = 0.31
steepestWalkableDegrees = math.degrees(math.acos(walkableNormalZ))
stepHeight = 4.5
eyeHeight = 5.5
swimEyeAboveSurface = 1.0

rof2Walk = "the RoF2 client on 2026-10-04, a human male of size 6 (avatarHeight 3.75) walking straight at each site without jumping"
sources = {
  "playerHeight": (
    "unmeasured: the design player, a 6-unit character, whose height headroom and open space are judged by (docs/zoneWorkflow.md);"
    " the walked human male's avatarHeight is 3.75"
  ),
  "walkableNormalZ": (
    f"measured: {rof2Walk}: he climbed every one of 20 faces from 24.4 to 71.9 degrees in Freeport West, Freeport Sewers, Freeport"
    " Academy, High Pass Hold, and Nektulos and was stopped by none, so the walkable limit is at least the steepest, 71.9 degrees"
    " (normal z 0.31), and may lie steeper (one walk hints at 74 to 77, unmeasured); his speed held to 61 degrees and fell to about 62%"
    " at 72; one character size only"
  ),
  "steepestWalkableDegrees": "derived: walkableNormalZ as the angle from level of the steepest face players walk",
  "stepHeight": (
    f"measured: {rof2Walk}: of 20 risers in Freeport West, Freeport Sewers, the guild hall, and High Pass Hold he climbed every one"
    " from 0.50 to 4.50 (4.50 under a cornice) and was stopped by every one from 5.22 to 7.82, so the limit lies in (4.50, 5.22], 1.20"
    " to 1.39 times his avatarHeight; this is the highest climbed, for one character size only, so whether it scales with"
    " avatarHeight is unproven"
  ),
  "eyeHeight": (
    "unmeasured: the eye of the design player's eye-level views; RoF2's first-person eye sits 3.69 above the spawn's z for a human of"
    " avatarHeight 3.75 (docs/clientRendering.md, Camera), which these views do not follow"
  ),
  "swimEyeAboveSurface": (
    f"unmeasured: a unit over the water keeps a swimming view's eye clear of the surface; in {rof2Walk}, a swimming player's"
    " origin floated 0.10 under the swim box's top, which does not give the design player's eye"
  ),
}
