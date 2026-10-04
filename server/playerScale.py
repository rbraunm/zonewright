"""The player's measures that geometry is judged by: how tall, and how steep a slope it can walk."""
playerHeight = 6.0
# EQEmu's navmesh agent climbs slopes up to 60 degrees; the client's own limit is unconfirmed.
walkableNormalZ = 0.5
# Unmeasured: the highest step a player walks up without jumping; the RoF2 client is to measure it.
stepHeight = 2.0
# Unmeasured: how far above the surface a swimming player's eye sits; the RoF2 client is to measure it.
swimEyeAboveSurface = 1.0
