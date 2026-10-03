"""The player's measures that geometry is judged by: how tall, and how steep a slope it can walk."""
playerHeight = 6.0
# EQEmu's navmesh agent climbs slopes up to 60 degrees; the client's own limit is unconfirmed.
walkableNormalZ = 0.5
