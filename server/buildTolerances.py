"""The tolerances a build is judged by apart from how a player moves (playerScale): how far a part may stand off what it rests on before
a gap shows, however high a player steps, which faces read as ground underfoot, and how far over a part its ground is probed from. The
bridge's modules judge by them and the server's tool descriptions state them."""
# A structure's end, foot, or deck this far or less over what it stands on reads as resting on it; higher, a gap shows under it.
supportTolerance = 2.0
# A kit piece's base this far or less over the lowest ground under it reads as standing on it; higher, it floats.
floatTolerance = 2.0
# A cave floor's middle this far or less over the ground reads as resting on it; higher, it hangs in the air.
floorHangTolerance = 2.0
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

# Footing and ground under a structure's part are looked for from this far over where the part is wanted, so ground a little higher
# (an end sunk into it) is found.
groundProbeLift = 2.0
# The ground under a kit piece is looked for from this far over its base, so ground its base is sunk into is found.
footingProbeLift = 2.0
# Ground and cover for a player at a level (a plot's) are looked for from this far over it: over ground a little higher, under a
# cave's roof.
levelProbeLift = 2.0
# Rock beside and around a cave's cut is probed this far over its floor, clear of the ground the floor lies on.
probeOverFloor = 2.0
