"""The tolerances a build is judged by apart from how a player moves (playerScale): how far a part may stand off what it rests on before
a gap shows, however high a player steps, which faces read as ground underfoot, how far over a part its ground is probed from, and how
alike an exported zone must draw to its scene. The bridge's modules judge by them and the server's tool descriptions state them."""
# A structure's end, foot, or deck this far or less over what it stands on reads as resting on it; higher, a gap shows under it.
supportTolerance = 2.0
# A kit piece's base this far or less over the lowest ground under it reads as standing on it; higher, it floats.
floatTolerance = 2.0
# A cave floor's middle this far or less over the ground reads as resting on it; higher, it hangs in the air.
floorHangTolerance = 2.0
# A floor stroke raising a cave wall's foot leaves at least this much of the wall's straight part over it, under the lowest trim band
# or the walls' top; less, and the wall folds.
wallFootClearance = 2.0
# A point within this of a cave floor stroke's height (a level way's floor, a pad's top) stands on the stroke; farther, it floats over
# it or is sunk in it.
strokeStandTolerance = 2.0
# A cave branch leaving through its parent's rubble or pad may run this far under it; deeper, the union cuts a trench nobody drew.
trenchTolerance = 2.0
# A prefab's floor without a plinth reads as standing on the ground within this of it: ground higher comes up through it, and ground
# lower shows a gap under its walls.
floorTolerance = 2.0
# A flight's or walkway's underside may rest in the ground it starts from this far in from its ends, in plan; past that it clears the
# ground.
endRest = 2.0
# A prefab's entrance is a doorway's threshold, within this of the footprint's edge; its ground outside is looked up this far past the
# edge.
thresholdReach = 2.0
# A prefab's footprint is the plan outline of its pieces' points within this of the floor: walls' feet and floors, not a roof's eaves.
footprintBand = 2.0
# A view of a structure's end stands on its approach where the ground there lies within this of the end's height, else on the
# structure, so it shows the end from where it is reached.
approachGroundReach = 4.0
# A ground border wants a transition strip where a side's normal z is at least this, reading as ground underfoot; a steeper face reads
# as a cliff, where materials meet without one.
transitionGroundNormalZ = 0.5
# The client draws no placed model of more triangles than this, which is as many as its own placed models run to (the Chapterhouse
# chancery's 6,763); one of more draws nothing, so a mesh of more is split into parts (bridgeSplit) and export refuses one that is not.
maxModelTriangles = 6763
# A view's lane tint covers a face whose normal z is at least this, a floor players stand or slide on; a steeper face is a wall beside
# the lane's floors, or something standing on them, and stays untinted.
laneTintNormalZ = 0.1

# Footing and ground under a structure's part are looked for from this far over where the part is wanted, so ground a little higher
# (an end sunk into it) is found.
groundProbeLift = 2.0
# The ground under a kit piece is looked for from this far over its base, so ground its base is sunk into is found.
footingProbeLift = 2.0
# Ground and cover for a player at a level (a plot's) are looked for from this far over it: over ground a little higher, under a
# cave's roof.
levelProbeLift = 2.0
# Rock beside and around a cave's cut, and the walls a branch passes out through, are probed this far over its floor, clear of the
# ground or floor it lies on.
probeOverFloor = 2.0
# A point given with its height finds the ground from this far above it, so a height read off a floor or a little under it still
# stands on that floor, down to groundSearchDistance below it: standAt, entries, the lane's seeds and teleport landings, and the
# ground under a lane view's eye.
groundSearchAbove = 3.0
groundSearchDistance = 50.0

# A sight to a point counts as clear when nothing blocks it short of this far before the point: a hit that near is the point's own
# face or the floor it lies over. Problem views and lane views judge their sights by it.
sightShort = 2.0
# A floor is looked at this far over it, so a sight to it is not lost in the floor's own faces: a problem's view target and a region's
# points in a lane view.
floorSightLift = 1.0

# A zone drawn from its exported archive looks as its scene drew when, over the same view, the mean difference of every pixel's channels
# is under this many levels (of 255), the 99th percentile of each pixel's largest channel difference at most this many, and under
# roundTripChangedShare of its pixels have a channel more than roundTripChangedLevels off: the same picture outside a few antialiased
# edge pixels (verifyExport's look check, and the round trips testZoneExport makes). The mean and the percentile alone pass a model
# missing or a texture changed over under 1% of the frame (a crate takes 0.3 to 1% of the views that show it); a fresh export draws
# bit-identical, and testZoneExport's round trips differ by 2 levels at most anywhere.
roundTripMeanLevels = 0.5
roundTripPercentile = 99
roundTripPercentileLevels = 2
roundTripChangedLevels = 8
roundTripChangedShare = 0.001

# A trace view's scale figure stands beside its line of sight, this share of the way from the frame's middle to its edge at her
# distance, so what lies along the line past her shows.
figureAside = 0.5
