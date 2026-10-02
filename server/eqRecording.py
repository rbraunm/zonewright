"""A live behavior recording (MQ2PeridotLive's peridotLiveBehavior_*.txt): every spawn, door, ground item, and placed object in a zone
as each arrives, changes, and departs, read back as the zone stood at a moment."""
import datetime

timeFormat = "%Y%m%d-%H%M%S"
spawnFields = (
  "spawnId", "name", "displayedName", "lastname", "type", "race", "class", "gender", "level", "x", "y", "z", "heading", "avatarHeight", "height",
  "guildId", "realEstateItemId", "realEstateId",
)
appearanceFields = (
  "actorDef", "textureType", "material", "variation", "headType", "faceStyle", "hairStyle", "hairColor", "facialHair", "facialHairColor",
  "eyeColor1", "eyeColor2", "raceOverride", "showHelm", "heritage", "tattoo", "details", "npcTintIndex", "light", "armorColors", "actorEquipment",
  "equipment",
)
propStates = {
  "door": ("state", "x", "y", "z", "heading"),
  "ground": ("x", "y", "z", "heading"),
  "placed": ("propertyId", "itemId", "x", "y", "z", "heading", "angle", "roll", "scale"),
}
# Spawn types: players (0) are the recording character and others, whose lines are never written; NPCs are 1.
npcType = 1
# How near a live dump row must stand to a recorded door or ground item to be the same one: doors.tsv has no instance column (a
# housing instance's doors differ from another's), and ground.tsv may lack the recording's instance.
sameSpotDistance = 0.05


def number(text):
  value = float(text)
  return int(value) if value.is_integer() and "." not in text else value


def recordingStart(path):
  """When a recording started (its start line, local time)."""
  with open(path, encoding="utf-8", errors="replace") as recording:
    for line in recording:
      fields = line.rstrip("\r\n").split("\t")
      if fields[0] == "start":
        return datetime.datetime.strptime(fields[1], timeFormat)
  raise ValueError(f"{path}: no start line")


def zoneAt(path, atMilliseconds):
  """The zone as it stood atMilliseconds after the recording's start: its zone and server, the NPCs present (each with its latest
  position, race, gender, height, and look), and the doors, ground items, and placed objects present, with their latest states."""
  recording = {"zone": None, "server": None, "start": None}
  spawns, props = {}, {}
  pending = None
  with open(path, encoding="utf-8", errors="replace") as lines:
    for line in lines:
      if line.startswith(("#", "[", " ")):
        continue
      fields = line.rstrip("\r\n").split("\t")
      kind = fields[0]
      if kind in ("server", "start"):
        recording[kind] = fields[1]
        continue
      if kind == "zone":
        recording["zone"], recording["instance"] = fields[1], int(fields[5])
        continue
      if kind in ("spawn", "brief") and pending is not None:
        record = dict(zip(spawnFields, fields[1:]))
        spawn = {key: number(value) if key not in ("name", "displayedName", "lastname") else value for key, value in record.items()}
        spawn["arrived"] = pending
        spawns[spawn["spawnId"]] = spawn
        pending = spawn
        continue
      if kind == "appearance":
        if pending is None:
          raise ValueError(f"{path}: an appearance line follows no spawn or look record")
        pending["appearance"] = dict(zip(appearanceFields, fields[1:]))
        pending = None
        continue
      if kind == "raw" or kind not in ("arrive", "depart", "move", "look", "state", "prop"):
        continue
      if int(fields[1]) > atMilliseconds:
        break
      if kind == "arrive":
        pending = int(fields[1])
      elif kind == "depart":
        spawns.pop(int(fields[2]), None)
      elif kind == "move" and int(fields[2]) in spawns:
        spawns[int(fields[2])].update(dict(zip(("x", "y", "z", "heading"), (number(value) for value in fields[3:7]))))
      elif kind == "look" and int(fields[2]) in spawns:
        pending = spawns[int(fields[2])]
        pending.update(dict(zip(("race", "gender", "height"), (number(value) for value in fields[3:6]))))
      elif kind == "state" and int(fields[2]) in spawns:
        spawns[int(fields[2])].update({"type": number(fields[3]), "name": fields[4], "displayedName": fields[5]})
      elif kind == "prop":
        propKind, propId, event, name = fields[2], int(fields[3]), fields[4], fields[5]
        if event == "depart":
          props.pop((propKind, propId), None)
        else:
          props[(propKind, propId)] = {"kind": propKind, "id": propId, "name": name} | dict(zip(propStates[propKind], (number(value) for value in fields[6].split())))
  if recording["start"] is None:
    raise ValueError(f"{path}: no start line")
  return recording | {
    "npcs": [spawn for spawn in spawns.values() if spawn["type"] == npcType],
    "props": sorted(props.values(), key=lambda prop: (prop["kind"], prop["id"])),
  }


def readTable(path):
  with open(path, encoding="utf-8", errors="replace") as table:
    header = table.readline().rstrip("\r\n").split("\t")
    return [dict(zip(header, line.rstrip("\r\n").split("\t"))) for line in table]


def propSizes(liveDumpsFolder, recording):
  """What the recording's props lack, from the live dumps' doors.tsv and ground.tsv rows of its zone: each door's scale and each ground
  item's scale and tilt, by id (a ground item's drop id) and name, the rows of each with their positions."""
  sizes = {"door": {}, "ground": {}}
  for kind, fileName, idKey in (("door", "doors.tsv", "id"), ("ground", "ground.tsv", "dropId")):
    for row in readTable(f"{liveDumpsFolder}/{fileName}"):
      if row["zone"] != recording["zone"]:
        continue
      size = {"x": float(row["x"]), "y": float(row["y"])} | (
        {"scale": float(row["scaleFactor"]) / 100} if kind == "door" else {"scale": float(row["scale"]), "pitch": float(row["pitch"]), "roll": float(row["roll"])}
      )
      sizes[kind].setdefault((int(row[idKey]), row["name"]), []).append(size)
  return sizes


def propSize(sizes, prop):
  """A prop's scale, or why it is not known or not drawn: a door's or ground item's from a live dump row of its id and name standing
  where it stands."""
  if prop["kind"] == "placed":
    if prop["angle"] or prop["roll"]:
      return None, f"tilted {prop['angle']} and rolled {prop['roll']}; tilted objects are not placed yet"
    return prop["scale"], None
  rows = [row for row in sizes[prop["kind"]].get((prop["id"], prop["name"]), []) if abs(row["x"] - prop["x"]) <= sameSpotDistance and abs(row["y"] - prop["y"]) <= sameSpotDistance]
  if not rows:
    return None, f"no {'doors' if prop['kind'] == 'door' else 'ground'}.tsv row of its id and name stands where it stands, so its scale is not known"
  scales = {(row["scale"], row.get("pitch", 0), row.get("roll", 0)) for row in rows}
  if len(scales) > 1:
    return None, f"live dump rows standing where it stands disagree on its scale and tilt: {sorted(scales)}"
  scale, pitch, roll = scales.pop()
  if pitch or roll:
    return None, f"pitched {pitch} and rolled {roll}; tilted objects are not placed yet"
  return scale, None
