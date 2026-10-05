"""A zone's server rows as SQL files the owner applies with the mariadb command-line client (akk-stack's database); zonewright never
connects to a database. Each file is one compound statement between DELIMITER lines: an exit handler that rolls back and raises again
any error, the transaction, guards that SIGNAL naming what they found, the writes, and the commit. So a guard's SIGNAL or a failed write
undoes every write whether or not the client stops at the error (a sourced file, --force, a client that goes on).
<short>.sql upserts the zone row (inserted, when the short name has no version-0 row, with every column zonewright does not write copied
from the template zone's version-0 row; then its written columns updated, never zoneidnumber, which other tables reference), then
replaces the zone_points rows of its own zone lines: it deletes only rows equal to one an export of this short name wrote, and refuses
a number it writes where any other row stands, so a reused slot's other rows stay. <short>_waysIn.sql points neighbours' zone_points
rows at the zone's zoneIn entries, each guarded to exactly one row. Column lists are EQEmu's (base_zone_repository.h,
base_zone_points_repository.h at 4aceae1), numbers Python's shortest round trip, strings single-quoted with ' and \\ doubled. The
readers parse exactly what the writers write and raise on anything else."""
import math
import re

import eqAxes
import playerScale

zoneColumns = (
  "zoneidnumber", "version", "short_name", "long_name", "min_status", "map_file_name", "note", "min_expansion", "max_expansion",
  "content_flags", "content_flags_disabled", "expansion", "file_name", "safe_x", "safe_y", "safe_z", "safe_heading", "graveyard_id",
  "min_level", "max_level", "timezone", "maxclients", "ruleset", "underworld", "minclip", "maxclip", "fog_minclip", "fog_maxclip",
  "fog_blue", "fog_red", "fog_green", "sky", "ztype", "zone_exp_multiplier", "walkspeed", "time_type", "fog_red1", "fog_green1",
  "fog_blue1", "fog_minclip1", "fog_maxclip1", "fog_red2", "fog_green2", "fog_blue2", "fog_minclip2", "fog_maxclip2", "fog_red3",
  "fog_green3", "fog_blue3", "fog_minclip3", "fog_maxclip3", "fog_red4", "fog_green4", "fog_blue4", "fog_minclip4", "fog_maxclip4",
  "fog_density", "flag_needed", "canbind", "cancombat", "canlevitate", "castoutdoor", "hotzone", "insttype", "shutdowndelay", "peqzone",
  "bypass_expansion_check", "suspendbuffs", "rain_chance1", "rain_chance2", "rain_chance3", "rain_chance4", "rain_duration1",
  "rain_duration2", "rain_duration3", "rain_duration4", "snow_chance1", "snow_chance2", "snow_chance3", "snow_chance4", "snow_duration1",
  "snow_duration2", "snow_duration3", "snow_duration4", "gravity", "type", "skylock", "fast_regen_hp", "fast_regen_mana",
  "fast_regen_endurance", "npc_max_aggro_dist", "client_update_range", "underworld_teleport_index", "lava_damage", "min_lava_damage",
  "idle_when_empty", "seconds_before_idle", "shard_at_player_count",
)
zonePointColumns = (
  "zone", "version", "number", "y", "x", "z", "heading", "target_y", "target_x", "target_z", "target_heading", "zoneinst",
  "target_zone_id", "target_instance", "buffer", "client_version_mask", "min_expansion", "max_expansion", "content_flags",
  "content_flags_disabled", "is_virtual", "height", "width",
)
fogColumns = ("fog_minclip", "fog_maxclip", "fog_density", "fog_red", "fog_green", "fog_blue")
# The row's key, never updated: an update never changes a zone's id, which other tables reference.
keyColumns = ("zoneidnumber", "version", "short_name")
# rof2_structs.h:579.
timeTypes = {"indoorDungeon": 0, "outdoor": 1, "outdoorCity": 2, "dungeonCity": 3, "indoorCity": 4, "outdoorDungeon": 5}
entryGateRule = (
  'entryGate is "open", {"minStatus": n} (the account status needed, 0-255), or {"zoneFlag": true} (flag_needed \'1\': a character'
  " needs the zone flag for this zone's id, granted by a GM or a quest); the server reads flag_needed only as a number, so a text flag"
  " gates nothing (zoning.cpp:1462-1467)"
)
longNamePattern = re.compile(r"^[\x20-\x7e]{1,127}$")
longNameRule = "1 to 127 printable ASCII characters (the client's zone_long_name[128])"
# zone_points.number is a uint16 holding a zone line's number times 10.
zonePointNumberLimit = 65535
zoneLineNumberLimit = zonePointNumberLimit // 10
allClientVersions = 4294967295
# MariaDB keeps at most 128 characters of a SIGNAL's MESSAGE_TEXT.
messageLimit = 128
templateAlias = "templateRow"
numberPattern = r"-?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?"
literalPattern = rf"(?:NULL|{numberPattern}|'(?:[^'\\]|''|\\\\)*')"
blockStart = ("DELIMITER //", "BEGIN NOT ATOMIC", "  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;", "  START TRANSACTION;")
blockEnd = ("  COMMIT;", "END//", "DELIMITER ;")
# The columns a zone line decides in its zone_points row, by which a row an earlier export wrote is known again.
ownershipFloatColumns = ("x", "y", "z", "target_x", "target_y", "target_z", "target_heading")
# zone_points holds these as FLOAT, which gives a written value back within half a float32 step: under 0.004 below 65536.
storedFloatTolerance = 0.01
signalTail = r" THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = (?P<message>'(?:[^'\\]|''|\\\\)*'); END IF;$"


def entryGateColumns(entryGate):
  """min_status and flag_needed for an entry gate (entryGateRule)."""
  if entryGate == "open":
    return 0, ""
  if isinstance(entryGate, dict) and set(entryGate) == {"minStatus"}:
    status = entryGate["minStatus"]
    if isinstance(status, int) and not isinstance(status, bool) and 0 <= status <= 255:
      return status, ""
  if isinstance(entryGate, dict) and set(entryGate) == {"zoneFlag"} and entryGate["zoneFlag"] is True:
    return 0, "1"
  raise ValueError(f"{entryGateRule}; got {entryGate!r}")


def literal(value):
  if value is None:
    return "NULL"
  if isinstance(value, bool) or not isinstance(value, (int, float, str)):
    raise TypeError(f"A row value is a number, a string, or NULL, got {value!r}")
  if isinstance(value, str):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
  if not math.isfinite(value):
    raise ValueError(f"A row value must be finite, got {value!r}")
  return repr(value)


def parseLiteral(text):
  if text == "NULL":
    return None
  if text.startswith("'"):
    return re.sub(r"''|\\\\", lambda match: match.group(0)[0], text[1:-1])
  return float(text) if any(mark in text for mark in ".e") else int(text)


def signal(condition, message):
  if len(message) > messageLimit:
    raise ValueError(f"A guard's message is over {messageLimit} characters: {message}")
  return f"  IF {condition} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = {literal(message)}; END IF;"


def numberList(numbers):
  return ", ".join(str(number) for number in numbers)


def rowMatch(row):
  """A condition true of a zone_points row that holds what `row` holds in the columns a zone line decides, as the table stores them."""
  near = [f"ABS({column} - ({literal(row[column])})) < {literal(storedFloatTolerance)}" for column in ownershipFloatColumns]
  return "(" + " AND ".join(near + [f"target_zone_id = {literal(row['target_zone_id'])}"]) + ")"


def ownedRows(record, short):
  """The record's distinct rows (every zone_points row an export of this short name wrote, as rowsFiles gives them), by number and the
  columns a zone line decides."""
  owned = {}
  for row in record:
    if not isinstance(row, dict) or set(row) != set(zonePointColumns) or (row["zone"], row["version"]) != (short, 0):
      raise ValueError(f"The record holds {short}'s version-0 zone_points rows as rowsFiles gives them, got {row!r}")
    owned[(row["number"], *(row[column] for column in ownershipFloatColumns), row["target_zone_id"])] = row
  return [owned[key] for key in sorted(owned)]


def requireKeys(zone, keys, why):
  missing = [key for key in keys if key not in zone]
  if missing:
    raise ValueError(f"The zone row needs {missing} ({why})")


def drawsSky(zone):
  return zone["sky"] != "none"


def zoneRowValues(zone, skyFogColor):
  """The zone row's written columns from the zone's properties: its id, names, gate, safe point (server axes, EQ heading), underworld,
  clips, sky, ztype (the client's fog switch, 255 with fog on), and time type; with fog on, its fog's start, end, density, and color
  (the sky's resolved fog color at its hour, which the client draws with its Sky option on, else fogColor); with fog off the fog columns
  are left to the template, as the client draws no fog then."""
  requireKeys(zone, ("shortName", "zoneId", "longName", "timeType", "entryGate", "serverTemplate", "safePoint", "underworld", "minClip", "maxClip", "fogOn", "sky"), "setZoneProperties")
  if drawsSky(zone) != (skyFogColor is not None):
    raise ValueError("skyFogColor is the fog color of the zone's sky at its hour (eqSky.skyState), given exactly when the zone draws a sky")
  if zone["timeType"] not in timeTypes:
    raise ValueError(f"timeType is one of {list(timeTypes)}, got {zone['timeType']!r}")
  minimumStatus, flagNeeded = entryGateColumns(zone["entryGate"])
  x, y, z, heading = zone["safePoint"]
  values = {
    "zoneidnumber": zone["zoneId"], "version": 0, "short_name": zone["shortName"], "long_name": zone["longName"], "map_file_name": None,
    "file_name": None, "expansion": 0, "min_expansion": -1, "max_expansion": -1, "bypass_expansion_check": 0, "min_status": minimumStatus,
    "flag_needed": flagNeeded, "min_level": 0, "max_level": 255, "graveyard_id": 0, "underworld_teleport_index": 0, "content_flags": None,
    "content_flags_disabled": None, "safe_x": y, "safe_y": x, "safe_z": z, "safe_heading": eqAxes.eqHeadingFromHeading(heading),
    "underworld": zone["underworld"], "minclip": zone["minClip"], "maxclip": zone["maxClip"], "sky": 1 if drawsSky(zone) else 0,
    "ztype": 255 if zone["fogOn"] else 0, "time_type": timeTypes[zone["timeType"]],
  }
  if zone["fogOn"]:
    requireKeys(zone, ("fogStart", "fogEnd", "fogDensity"), "fog is on")
    if not drawsSky(zone):
      requireKeys(zone, ("fogColor",), "fog is on and the zone draws no sky")
    color = skyFogColor if drawsSky(zone) else zone["fogColor"]
    values |= {"fog_minclip": zone["fogStart"], "fog_maxclip": zone["fogEnd"], "fog_density": zone["fogDensity"]}
    values |= {column: round(255 * channel) for column, channel in zip(("fog_red", "fog_green", "fog_blue"), color)}
  return values


def zonePointRows(zone, zoneLines, clientZones):
  """One zone_points row per zone line of the zone's own (getZoneLines' zoneLines; those an imported archive brought are left out):
  numbered its number times 10, at its box's center in the server's axes, so the closest-point lookup among rows of one target picks
  this box (zone.cpp:2031), leading to its target in the server's axes and EQ heading, a kept coordinate 999999 and a kept heading 999,
  the target's id the client's (clientZones: short name to id) or the zone's own."""
  rows = []
  for line in zoneLines:
    if "clientContent" in line:
      continue
    target = line["target"]
    if target is None:
      raise ValueError(f"Zone line '{line['name']}' has no target")
    if not 1 <= line["number"] <= zoneLineNumberLimit:
      raise ValueError(f"Zone line '{line['name']}' is numbered {line['number']}; its row number (x 10) must fit zone_points' uint16 number: 1 to {zoneLineNumberLimit}")
    if target["zone"] == zone["shortName"]:
      targetID = zone["zoneId"]
    elif target["zone"] in clientZones:
      targetID = clientZones[target["zone"]]
    else:
      raise ValueError(f"Zone line '{line['name']}' leads to '{target['zone']}', which the client does not register and is not this zone")
    # getZoneLines gives corners to 2 decimals, so their midpoint holds at most 3; rounding there drops only float noise.
    center = [round((low + high) / 2, 3) for low, high in zip(line["minimum"], line["maximum"])]
    serverCenter = [float(value) for value in eqAxes.serverFromZone(center)]
    kept = {axis: target[axis] == "keep" for axis in ("x", "y", "z", "headingDegrees")}
    rows.append({
      "zone": zone["shortName"], "version": 0, "number": line["number"] * 10, "y": serverCenter[1], "x": serverCenter[0], "z": serverCenter[2],
      "heading": 0, "target_y": eqAxes.keptCoordinate if kept["x"] else target["x"], "target_x": eqAxes.keptCoordinate if kept["y"] else target["y"],
      "target_z": eqAxes.keptCoordinate if kept["z"] else target["z"],
      "target_heading": eqAxes.keptHeading if kept["headingDegrees"] else eqAxes.eqHeadingFromHeading(target["headingDegrees"]),
      "zoneinst": 0, "target_zone_id": targetID, "target_instance": 0, "buffer": 0, "client_version_mask": allClientVersions,
      "min_expansion": -1, "max_expansion": -1, "content_flags": None, "content_flags_disabled": None, "is_virtual": 0, "height": 0, "width": 0,
    })
  numbers = [row["number"] for row in rows]
  repeated = sorted({number for number in numbers if numbers.count(number) > 1})
  if repeated:
    raise ValueError(f"Zone lines share row numbers {repeated}")
  return sorted(rows, key=lambda row: row["number"])


def zoneGuards(zone, targets, written, owned):
  """The guards before any write, each a SIGNAL naming what it found: the id another zone's, the row's id another, two version-0 rows, no
  row and no single template row to copy, a zone line target without its row under the client's id ({short name: id}), and per
  zone_points number written now, a row of this zone there that is none an export of this short name wrote (owned): someone else's, or
  changed on the server since, never overwritten."""
  short, zoneID, template = zone["shortName"], zone["zoneId"], zone["serverTemplate"]
  guards = [
    signal(f"EXISTS (SELECT 1 FROM zone WHERE zoneidnumber = {zoneID} AND short_name <> {literal(short)})", f"zoneidnumber {zoneID} belongs to a zone other than {short}"),
    signal(
      f"EXISTS (SELECT 1 FROM zone WHERE short_name = {literal(short)} AND version = 0 AND zoneidnumber <> {zoneID})",
      f"{short}'s version-0 row has a zoneidnumber other than {zoneID}; an update never changes a zone's id",
    ),
    signal(f"(SELECT COUNT(*) FROM zone WHERE short_name = {literal(short)} AND version = 0) > 1", f"more than one version-0 zone row holds {short}"),
    signal(
      f"NOT EXISTS (SELECT 1 FROM zone WHERE short_name = {literal(short)} AND version = 0) AND (SELECT COUNT(*) FROM zone WHERE short_name = {literal(template)} AND version = 0) <> 1",
      f"{short} has no version-0 row to update and {template} not exactly one to copy",
    ),
  ]
  for name, targetID in sorted(targets.items()):
    guards.append(signal(
      f"NOT EXISTS (SELECT 1 FROM zone WHERE short_name = {literal(name)} AND version = 0 AND zoneidnumber = {targetID})",
      f"zone line target {name} has no version-0 zone row with the client's id {targetID}",
    ))
  for number in written:
    versions = [row for row in owned if row["number"] == number]
    exclusion = f" AND NOT ({' OR '.join(rowMatch(row) for row in versions)})" if versions else ""
    guards.append(signal(
      f"EXISTS (SELECT 1 FROM zone_points WHERE zone = {literal(short)} AND version = 0 AND number = {number}{exclusion})",
      f"{short} zone_points {number} is not zonewright's: renumber the zone line or remove that row",
    ))
  return guards


def rowsFiles(zone, zoneLines, entries, clientZones, skyFogColor, record, archiveSHA256):
  """The zone's server rows: {rows: <short>.sql, waysIn: <short>_waysIn.sql or None, zonePoints, numbersWritten, numbersDeleted,
  waysInToAdd}. zone is the zone's properties (getZoneProperties), zoneLines getZoneLines' zoneLines, entries getEntries' entries,
  clientZones the client's registrations (short name to id), skyFogColor the fog color of the zone's sky at its hour when it draws one,
  record every zone_points row an export of this short name wrote before (the zonePoints each gave), and archiveSHA256 the archive's,
  as the manifest records it. zonePoints are the rows written now, for the record; numbersDeleted the numbers of the record's rows,
  which <short>.sql deletes where the table still holds them as written. zoneIn entries with fromNumber point that neighbour row at
  them; those without are listed in waysInToAdd."""
  values = zoneRowValues(zone, skyFogColor)
  short = zone["shortName"]
  lines = [line for line in zoneLines if "clientContent" not in line]
  rows = zonePointRows(zone, lines, clientZones)
  owned = ownedRows(record, short)
  waysIn = zoneInEntries(zone, entries)
  targets = {line["target"]["zone"]: clientZones[line["target"]["zone"]] for line in lines if line["target"]["zone"] != short}
  written = [row["number"] for row in rows]
  deleted = sorted({row["number"] for row in owned})
  text = [
    f"-- zonewright server rows: zone {short}", f"-- archive sha256: {archiveSHA256}", f"-- template: {zone['serverTemplate']}",
    f"-- zone_points written: {numberList(written) or 'none'}", f"-- zone_points deleted: {numberList(deleted) or 'none'}",
    *blockStart, *zoneGuards(zone, targets, written, owned),
  ]
  selected = [literal(values[column]) if column in values else f"{templateAlias}.{column}" for column in zoneColumns]
  text.append(
    f"  INSERT INTO zone ({', '.join(zoneColumns)}) SELECT {', '.join(selected)} FROM zone AS {templateAlias} WHERE {templateAlias}.short_name ="
    f" {literal(zone['serverTemplate'])} AND {templateAlias}.version = 0 AND NOT EXISTS (SELECT 1 FROM zone WHERE short_name = {literal(short)} AND version = 0);"
  )
  updated = [f"{column} = {literal(values[column])}" for column in zoneColumns if column in values and column not in keyColumns]
  text.append(f"  UPDATE zone SET {', '.join(updated)} WHERE short_name = {literal(short)} AND version = 0;")
  text += [f"  DELETE FROM zone_points WHERE zone = {literal(short)} AND version = 0 AND number = {row['number']} AND {rowMatch(row)};" for row in owned]
  if rows:
    text.append(f"  INSERT INTO zone_points ({', '.join(zonePointColumns)}) VALUES")
    text += [f"    ({', '.join(literal(row[column]) for column in zonePointColumns)})" + ("," if index < len(rows) - 1 else ";") for index, row in enumerate(rows)]
  text += blockEnd
  return {
    "rows": "\n".join(text) + "\n", "waysIn": waysInFile(zone, [entry for entry in waysIn if entry["fromNumber"] is not None], archiveSHA256),
    "zonePoints": rows, "numbersWritten": written, "numbersDeleted": deleted,
    "waysInToAdd": [{"entry": entry["name"], "fromZone": entry["fromZone"]} for entry in waysIn if entry["fromNumber"] is None],
  }


def zoneInEntries(zone, entries):
  """The placed zoneIn entries, each fromNumber a neighbour's zone_points number: a uint16, and never this zone's own row, which
  <short>.sql writes from its zone lines."""
  found = [entry for entry in entries if entry["source"] == "placeEntry" and entry["kind"] == "zoneIn"]
  for entry in found:
    number = entry["fromNumber"]
    if number is None:
      continue
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= zonePointNumberLimit:
      raise ValueError(f"zoneIn '{entry['name']}' names fromNumber {number!r}: a zone_points number is a whole number from 1 to {zonePointNumberLimit}")
    if entry["fromZone"] == zone["shortName"]:
      raise ValueError(
        f"zoneIn '{entry['name']}' names {entry['fromZone']} zone_points {number}, this zone's own row, which {entry['fromZone']}.sql writes"
        " from its zone lines; fromNumber names a neighbour's row"
      )
  return found


def arrivalPoint(entry):
  """Where a ways-in row puts a player arriving on a zoneIn entry's footing, in the server's axes: the server sets a player's origin to
  a zone point's target as to the safe point (zoning.cpp:261-320), and the client stands an origin avatarHeight over the floor (3.75 for
  the walked human male), so the target is playerHeight over the footing, the top of the space placeEntry keeps clear there, and the
  player settles onto it rather than arriving with the origin in the floor."""
  x, y, z = entry["at"]
  return [float(value) for value in eqAxes.serverFromZone([x, y, z + playerScale.playerHeight])]


def waysInFile(zone, entries, archiveSHA256):
  """<short>_waysIn.sql for zoneIn entries naming their neighbour's row, or None without any: a guard per entry that SIGNALs unless
  exactly one version-0 row of the neighbour holds the number (counted, as ROW_COUNT() reads 0 for an update that changes nothing, and
  a number two rows hold is refused, never both rewritten), then an update per entry pointing that row at the entry's arrival point."""
  if not entries:
    return None
  ordered = sorted(entries, key=lambda found: (found["fromZone"], found["fromNumber"]))

  def where(entry):
    return f"zone = {literal(entry['fromZone'])} AND version = 0 AND number = {entry['fromNumber']}"

  text = [f"-- zonewright ways in: zone {zone['shortName']}", f"-- archive sha256: {archiveSHA256}", *blockStart]
  text += [
    signal(f"(SELECT COUNT(*) FROM zone_points WHERE {where(entry)}) <> 1", f"ways in: {entry['fromZone']} zone_points number {entry['fromNumber']} is not exactly one version-0 row")
    for entry in ordered
  ]
  for entry in ordered:
    serverAt = arrivalPoint(entry)
    text.append(
      f"  UPDATE zone_points SET target_zone_id = {zone['zoneId']}, target_x = {literal(serverAt[0])}, target_y = {literal(serverAt[1])},"
      f" target_z = {literal(serverAt[2])}, target_heading = {literal(eqAxes.eqHeadingFromHeading(entry['headingDegrees']))} WHERE {where(entry)};"
    )
  text += blockEnd
  return "\n".join(text) + "\n"


class Lines:
  """A file's lines, read in order, each matched against what must come next."""

  def __init__(self, text, fileName):
    if not text.endswith("\n"):
      raise ValueError(f"{fileName} does not end with a line break")
    self.lines = text[:-1].split("\n")
    self.index = 0
    self.fileName = fileName

  def peek(self):
    return self.lines[self.index] if self.index < len(self.lines) else None

  def take(self, pattern, what):
    line = self.peek()
    match = None if line is None else re.fullmatch(pattern, line)
    if match is None:
      raise ValueError(f"{self.fileName} line {self.index + 1}: expected {what}, got {line!r}")
    self.index += 1
    return match

  def exact(self, text):
    return self.take(re.escape(text), repr(text))

  def finish(self):
    if self.index != len(self.lines):
      raise ValueError(f"{self.fileName} line {self.index + 1}: expected the end of the file, got {self.lines[self.index]!r}")


def literals(text, what):
  """A comma-separated list of literals."""
  found = re.findall(literalPattern + r"(?:, |$)", text)
  if ", ".join(item.removesuffix(", ") for item in found) != text:
    raise ValueError(f"{what} is not a list of literals: {text!r}")
  return [parseLiteral(item.removesuffix(", ")) for item in found]


def numbersOf(text):
  return [] if text == "none" else [int(number) for number in text.split(", ")]


shortNameLiteral = r"'[a-z0-9]{1,31}'"
guardPatterns = {
  "zoneID": rf"EXISTS \(SELECT 1 FROM zone WHERE zoneidnumber = (?P<zoneID>[0-9]+) AND short_name <> (?P<shortName>{shortNameLiteral})\)",
  "rowID": rf"EXISTS \(SELECT 1 FROM zone WHERE short_name = (?P<shortName>{shortNameLiteral}) AND version = 0 AND zoneidnumber <> (?P<zoneID>[0-9]+)\)",
  "duplicateRow": rf"\(SELECT COUNT\(\*\) FROM zone WHERE short_name = (?P<shortName>{shortNameLiteral}) AND version = 0\) > 1",
  "template": (
    rf"NOT EXISTS \(SELECT 1 FROM zone WHERE short_name = (?P<shortName>{shortNameLiteral}) AND version = 0\) AND \(SELECT COUNT\(\*\) FROM zone WHERE"
    rf" short_name = (?P<template>{shortNameLiteral}) AND version = 0\) <> 1"
  ),
  "target": rf"NOT EXISTS \(SELECT 1 FROM zone WHERE short_name = (?P<target>{shortNameLiteral}) AND version = 0 AND zoneidnumber = (?P<targetID>[0-9]+)\)",
  "foreignZonePoints": (
    rf"EXISTS \(SELECT 1 FROM zone_points WHERE zone = (?P<shortName>{shortNameLiteral}) AND version = 0 AND number = (?P<number>[0-9]+)"
    r"(?: AND NOT \((?P<owned>.+)\))?\)"
  ),
}
rowMatchPattern = re.compile(
  r"\(" + " AND ".join(rf"ABS\({column} - \((?P<{column}>{numberPattern})\)\) < {re.escape(literal(storedFloatTolerance))}" for column in ownershipFloatColumns)
  + r" AND target_zone_id = (?P<target_zone_id>[0-9]+)\)"
)


def readRowMatches(text, what):
  """The rows a list of rowMatch conditions joined by OR names, each by the columns a zone line decides."""
  rows, position = [], 0
  while True:
    match = rowMatchPattern.match(text, position)
    if match is None:
      raise ValueError(f"{what}: expected a zonewright row's condition at {text[position:]!r}")
    rows.append({column: parseLiteral(value) for column, value in match.groupdict().items()})
    position = match.end()
    if position == len(text):
      return rows
    if not text.startswith(" OR ", position):
      raise ValueError(f"{what}: expected OR or the end of the conditions at {text[position:]!r}")
    position += len(" OR ")


def readGuard(lines):
  line = lines.peek()
  for kind, pattern in guardPatterns.items():
    match = re.fullmatch(r"  IF " + pattern + signalTail, line)
    if match is not None:
      lines.index += 1
      found = {"guard": kind}
      for name, value in match.groupdict().items():
        if name == "owned":
          found[name] = [] if value is None else readRowMatches(value, f"{lines.fileName} line {lines.index}")
        else:
          found[name] = parseLiteral(value) if value.startswith("'") else int(value)
      return found
  raise ValueError(f"{lines.fileName} line {lines.index + 1}: expected a guard, got {line!r}")


def startsWith(lines, prefix):
  return lines.peek() is not None and lines.peek().startswith(prefix)


def readRows(text, fileName="the rows file"):
  """What <short>.sql holds: its header, its guards (each kind with its values and message), the zone row's insert (the written values,
  the columns copied, the template, the short name whose absence it needs), its update, the zone_points rows deleted (each by number and
  the columns a zone line decides), and the rows inserted."""
  lines = Lines(text, fileName)
  header = {
    "shortName": lines.take(r"-- zonewright server rows: zone ([a-z0-9]{1,31})", "the header's zone").group(1),
    "archiveSHA256": lines.take(r"-- archive sha256: ([0-9a-f]{64})", "the archive's SHA-256").group(1),
    "template": lines.take(r"-- template: ([a-z0-9]{1,31})", "the template").group(1),
    "numbersWritten": numbersOf(lines.take(r"-- zone_points written: (none|[0-9]+(?:, [0-9]+)*)", "the numbers written").group(1)),
    "numbersDeleted": numbersOf(lines.take(r"-- zone_points deleted: (none|[0-9]+(?:, [0-9]+)*)", "the numbers deleted").group(1)),
  }
  for statement in blockStart:
    lines.exact(statement)
  guards = []
  while startsWith(lines, "  IF "):
    guards.append(readGuard(lines))
  insert = lines.take(
    rf"  INSERT INTO zone \((?P<columns>[a-z_0-9, ]+)\) SELECT (?P<selected>.+) FROM zone AS {templateAlias} WHERE {templateAlias}\.short_name ="
    rf" (?P<template>{shortNameLiteral}) AND {templateAlias}\.version = 0 AND NOT EXISTS \(SELECT 1 FROM zone WHERE short_name = (?P<shortName>{shortNameLiteral}) AND version = 0\);",
    "the zone row's insert",
  )
  if tuple(insert["columns"].split(", ")) != zoneColumns:
    raise ValueError(f"{fileName}: the zone row's insert does not name every column of zone but id, in EQEmu's order")
  items = re.findall(rf"({literalPattern}|{templateAlias}\.[a-z_0-9]+)(?:, |$)", insert["selected"])
  if ", ".join(items) != insert["selected"] or len(items) != len(zoneColumns):
    raise ValueError(f"{fileName}: the zone row's insert selects {insert['selected']!r}, not one literal or template column per column")
  written, copied = {}, []
  for column, item in zip(zoneColumns, items):
    if item.startswith(templateAlias + "."):
      if item != f"{templateAlias}.{column}":
        raise ValueError(f"{fileName}: the zone row's insert fills {column} from {item}")
      copied.append(column)
    else:
      written[column] = parseLiteral(item)
  update = lines.take(rf"  UPDATE zone SET (?P<assignments>.+) WHERE short_name = (?P<shortName>{shortNameLiteral}) AND version = 0;", "the zone row's update")
  assignments = re.findall(rf"([a-z_0-9]+) = ({literalPattern})(?:, |$)", update["assignments"])
  if ", ".join(f"{column} = {value}" for column, value in assignments) != update["assignments"]:
    raise ValueError(f"{fileName}: the zone row's update sets {update['assignments']!r}, not column = literal pairs")
  updated = {column: parseLiteral(value) for column, value in assignments}
  unknown = sorted(set(updated) - set(zoneColumns))
  if unknown or len(updated) != len(assignments):
    raise ValueError(f"{fileName}: the zone row's update sets unknown or repeated columns {unknown or [column for column, _ in assignments]}")
  deleted = []
  while startsWith(lines, "  DELETE"):
    match = lines.take(rf"  DELETE FROM zone_points WHERE zone = ({shortNameLiteral}) AND version = 0 AND number = ([0-9]+) AND (\(.+\));", "a zone_points delete")
    row = readRowMatches(match.group(3), f"{fileName} line {lines.index}")
    if len(row) != 1:
      raise ValueError(f"{fileName} line {lines.index}: a zone_points delete names {len(row)} rows, not one")
    deleted.append({"zone": parseLiteral(match.group(1)), "number": int(match.group(2))} | row[0])
  zonePoints = []
  if startsWith(lines, "  INSERT INTO zone_points"):
    lines.exact(f"  INSERT INTO zone_points ({', '.join(zonePointColumns)}) VALUES")
    while True:
      match = lines.take(r"    \((.+)\)([,;])", "a zone_points row")
      values = literals(match.group(1), f"{fileName} line {lines.index}")
      if len(values) != len(zonePointColumns):
        raise ValueError(f"{fileName} line {lines.index}: a zone_points row of {len(values)} values, not {len(zonePointColumns)}")
      zonePoints.append(dict(zip(zonePointColumns, values)))
      if match.group(2) == ";":
        break
  for statement in blockEnd:
    lines.exact(statement)
  lines.finish()
  return header | {
    "guards": guards,
    "zoneInsert": {"written": written, "copied": copied, "template": parseLiteral(insert["template"]), "absentRow": parseLiteral(insert["shortName"])},
    "zoneUpdate": {"values": updated, "shortName": parseLiteral(update["shortName"])},
    "zonePointsDeleted": deleted, "zonePoints": zonePoints,
  }


def readWaysIn(text, fileName="the ways-in file"):
  """What <short>_waysIn.sql holds: its zone, archive, and per neighbour row the guard counting it and the update pointing it here."""
  lines = Lines(text, fileName)
  shortName = lines.take(r"-- zonewright ways in: zone ([a-z0-9]{1,31})", "the header's zone").group(1)
  archiveSHA256 = lines.take(r"-- archive sha256: ([0-9a-f]{64})", "the archive's SHA-256").group(1)
  for statement in blockStart:
    lines.exact(statement)
  where = rf"zone = (?P<fromZone>{shortNameLiteral}) AND version = 0 AND number = (?P<fromNumber>[0-9]+)"
  guards = []
  while not guards or startsWith(lines, "  IF "):
    guards.append(lines.take(r"  IF \(SELECT COUNT\(\*\) FROM zone_points WHERE " + where + r"\) <> 1" + signalTail, "the guard counting the neighbour's row"))
  updates = []
  while startsWith(lines, "  UPDATE "):
    updates.append(lines.take(
      rf"  UPDATE zone_points SET target_zone_id = (?P<zoneID>[0-9]+), target_x = (?P<x>{literalPattern}), target_y = (?P<y>{literalPattern}),"
      rf" target_z = (?P<z>{literalPattern}), target_heading = (?P<heading>{literalPattern}) WHERE {where};",
      "the neighbour row's update",
    ))
  counted, pointed = ([(match["fromZone"], match["fromNumber"]) for match in found] for found in (guards, updates))
  if counted != pointed:
    raise ValueError(f"{fileName}: the updates' rows {pointed} are not the rows the guards count {counted}")
  for statement in blockEnd:
    lines.exact(statement)
  lines.finish()
  rows = [
    {
      "fromZone": parseLiteral(guard["fromZone"]), "fromNumber": int(guard["fromNumber"]), "message": parseLiteral(guard["message"]),
      "target_zone_id": int(update["zoneID"]), "target_x": parseLiteral(update["x"]), "target_y": parseLiteral(update["y"]),
      "target_z": parseLiteral(update["z"]), "target_heading": parseLiteral(update["heading"]),
    }
    for guard, update in zip(guards, updates)
  ]
  return {"shortName": shortName, "archiveSHA256": archiveSHA256, "rows": rows}
