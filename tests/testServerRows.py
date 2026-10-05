import re
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

from conftest import everquestClient, serverRowValues

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqAxes
import eqClientZones
import eqgFiles
import serverRows
from playerScale import playerHeight
from testEntries import entryArguments, groundGrid
from testModelsAndDressing import freshScene
from testWater import environment

# A few of the client's registrations, as eqClientZones gives them.
clientZones = {"qeynos2": 2, "highkeep": 6, "eastkarana": 15, "kithicor": 20, "highpasshold": 407, "neighborhood": 712}
archiveSHA256 = "5" * 64
rowZone = {
  "shortName": "testhollow", "zoneId": 901, "longName": "The Testers' Hollow \\ East", "timeType": "outdoorCity", "entryGate": "open",
  "serverTemplate": "qeynos2", "safePoint": [10.5, -20.25, 3, 45], "underworld": -100, "minClip": 60, "maxClip": 2000, "fogOn": True,
  "fogStart": 100, "fogEnd": 900, "fogDensity": 0.25, "sky": {"type": "testhollow", "hour": 12, "minute": 0},
}
skyFog = [0.2, 0.4, 0.6]
# Peridot's Highpass Hold zone_points rows by number: target id, target_x, target_y, target_z, target_heading (server axes, EQ heading).
highpassPoints = {100: (15, -8335, -3073, 694, 119), 200: (20, 4897, 539, 694, 999), 300: (6, 90, 62, 3, 256), 400: (6, 90, -90, 3, 0), 500: (395, -283, -1541, 119, 118)}


def zoneLine(number, minimum, maximum, target, label="line"):
  return {"name": f"ATP_{number}_{label}", "number": number, "label": label, "minimum": minimum, "maximum": maximum, "target": target}


def written(zone, skyFogColor=None, zoneLines=(), entries=(), record=()):
  files = serverRows.rowsFiles(zone, list(zoneLines), list(entries), clientZones, skyFogColor, list(record), archiveSHA256)
  return files, serverRows.readRows(files["rows"])


def decided(row):
  """A zone_points row's number and the columns its zone line decides."""
  return {column: row[column] for column in ("number", *serverRows.ownershipFloatColumns, "target_zone_id")}


def testZoneRowCarriesTheStoredInputs():
  files, read = written(rowZone, skyFog)
  row = read["zoneInsert"]["written"]
  # The safe point in the server's axes (zone y, zone x), its heading in EQ units, counter-clockwise from the server's +y (zone +X):
  # 45 degrees clockwise from +Y is 45 counter-clockwise from +X, 64 of 512.
  assert (row["safe_x"], row["safe_y"], row["safe_z"], row["safe_heading"]) == (-20.25, 10.5, 3, 64.0)
  assert (row["zoneidnumber"], row["short_name"], row["long_name"], row["version"]) == (901, "testhollow", "The Testers' Hollow \\ East", 0)
  assert "'The Testers'' Hollow \\\\ East'" in files["rows"]
  # Fog from the sky's color at its hour, on (ztype 255), and the sky drawn.
  assert (row["fog_red"], row["fog_green"], row["fog_blue"], row["fog_minclip"], row["fog_maxclip"], row["fog_density"]) == (51, 102, 153, 100, 900, 0.25)
  assert (row["ztype"], row["sky"], row["time_type"], row["minclip"], row["maxclip"], row["underworld"]) == (255, 1, 2, 60, 2000, -100)
  # Open to every account and every level, pointing at no other zone's graveyard or zone points; the gate is entryGate.
  assert (row["expansion"], row["min_expansion"], row["max_expansion"], row["bypass_expansion_check"]) == (0, -1, -1, 0)
  assert (row["min_level"], row["max_level"], row["graveyard_id"], row["underworld_teleport_index"], row["min_status"], row["flag_needed"]) == (0, 255, 0, 0, 0, "")
  assert (row["map_file_name"], row["file_name"], row["content_flags"], row["content_flags_disabled"]) == (None, None, None, None)
  writtenColumns = {
    "zoneidnumber", "version", "short_name", "long_name", "min_status", "map_file_name", "min_expansion", "max_expansion", "content_flags",
    "content_flags_disabled", "expansion", "file_name", "safe_x", "safe_y", "safe_z", "safe_heading", "graveyard_id", "min_level", "max_level",
    "underworld", "minclip", "maxclip", "fog_minclip", "fog_maxclip", "fog_blue", "fog_red", "fog_green", "sky", "ztype", "time_type",
    "fog_density", "flag_needed", "bypass_expansion_check", "underworld_teleport_index",
  }
  # The insert copies exactly the other columns from the template's version-0 row; the update sets the written ones but the row's key.
  assert set(row) == writtenColumns and read["zoneInsert"]["copied"] == [column for column in serverRows.zoneColumns if column not in writtenColumns]
  assert {"ruleset", "zone_exp_multiplier", "canbind", "gravity", "lava_damage", "maxclients", "fog_red1"} <= set(read["zoneInsert"]["copied"])
  assert (read["zoneInsert"]["template"], read["zoneInsert"]["absentRow"], read["template"]) == ("qeynos2", "testhollow", "qeynos2")
  assert read["zoneUpdate"] == {"values": {column: value for column, value in row.items() if column not in ("zoneidnumber", "version", "short_name")}, "shortName": "testhollow"}
  assert "zoneidnumber =" not in files["rows"].split("UPDATE zone SET", 1)[1]
  # Fog by hand without a sky, and the sky stated none.
  handFog = written(rowZone | {"sky": "none", "fogColor": [1.0, 0.5, 0.0]})[1]["zoneInsert"]["written"]
  assert (handFog["fog_red"], handFog["fog_green"], handFog["fog_blue"], handFog["sky"]) == (255, 128, 0, 0)
  # Fog off: ztype 0, and the fog columns left to the template.
  fogless = written(rowZone | {"fogOn": False}, skyFog)[1]
  assert fogless["zoneInsert"]["written"]["ztype"] == 0 and set(serverRows.fogColumns) <= set(fogless["zoneInsert"]["copied"])
  assert not set(serverRows.fogColumns) & set(fogless["zoneUpdate"]["values"])
  # The three gates: a minimum status, or flag_needed '1' and a zone flag; never text.
  gates = {}
  for gate in ("open", {"minStatus": 80}, {"zoneFlag": True}):
    gated = written(rowZone | {"entryGate": gate}, skyFog)[1]["zoneInsert"]["written"]
    gates[str(gate)] = (gated["min_status"], gated["flag_needed"])
  assert gates == {"open": (0, ""), "{'minStatus': 80}": (80, ""), "{'zoneFlag': True}": (0, "1")}
  # A flag is true itself: text, 1, or 1.0 is refused, never stored as the flag.
  for gate in ({"zoneFlag": "keyed"}, {"zoneFlag": 1}, {"zoneFlag": 1.0}):
    with pytest.raises(ValueError) as refused:
      written(rowZone | {"entryGate": gate}, skyFog)
    assert str(refused.value) == f"{serverRows.entryGateRule}; got {gate!r}"
  with pytest.raises(ValueError, match="skyFogColor is the fog color of the zone's sky"):
    written(rowZone)


def testZonePointsAreOnePerLineNumberedTimesTenWithKeep():
  lines = [
    zoneLine(12, [100, 200, -5], [110, 240, 30], {"zone": "eastkarana", "x": -3073, "y": -8335, "z": 694, "headingDegrees": 6.328125}),
    zoneLine(3, [-20, 10, 0], [20, 14, 40], {"zone": "kithicor", "x": "keep", "y": 4897, "z": 694, "headingDegrees": "keep"}),
    zoneLine(7, [50, -60, 0], [60, -50, 10], {"zone": "testhollow", "x": 0, "y": 5, "z": "keep", "headingDegrees": 270}),
    zoneLine(9, [0, 0, 0], [1, 1, 1], None) | {"clientContent": "zoneFile"},
  ]
  earlier = written(rowZone, skyFog, [zoneLine(7, [40, -60, 0], [60, -50, 10], lines[2]["target"]), zoneLine(50, [0, 0, 0], [2, 2, 2], lines[0]["target"])])[0]["zonePoints"]
  files, read = written(rowZone, skyFog, lines, record=earlier)
  points = read["zonePoints"]
  assert [point["number"] for point in points] == [30, 70, 120]
  # Each at its box's center in the server's axes, so the server's closest-point lookup picks this box.
  assert [(point["x"], point["y"], point["z"]) for point in points] == [(12.0, 0.0, 20.0), (-55.0, 55.0, 5.0), (220.0, 105.0, 12.5)]
  # Kept coordinates are 999999 and a kept heading 999; the rest in the server's axes and EQ units.
  assert [(point["target_x"], point["target_y"], point["target_z"], point["target_heading"]) for point in points] == [
    (4897, eqAxes.keptCoordinate, 694, eqAxes.keptHeading), (5, 0, eqAxes.keptCoordinate, 256.0), (-8335, -3073, 694, 119.0),
  ]
  # A line back into this zone carries the zone's own id; the others the client's.
  assert [point["target_zone_id"] for point in points] == [20, 901, 15]
  assert {(point["zone"], point["version"], point["heading"], point["client_version_mask"], point["min_expansion"], point["max_expansion"], point["is_virtual"]) for point in points} == {("testhollow", 0, 0, 4294967295, -1, -1, 0)}
  # Only the rows earlier exports of this short name wrote are deleted, each where the table holds it as written (its number and what
  # its line decided); the rows written now go to the record.
  assert (read["numbersWritten"], read["numbersDeleted"]) == ([30, 70, 120], [70, 500])
  assert read["zonePointsDeleted"] == [{"zone": "testhollow"} | decided(row) for row in earlier]
  assert [decided(row) for row in earlier] == [
    {"number": 70, "x": -55.0, "y": 50.0, "z": 5.0, "target_x": 5, "target_y": 0, "target_z": eqAxes.keptCoordinate, "target_heading": 256.0, "target_zone_id": 901},
    {"number": 500, "x": 1.0, "y": 1.0, "z": 1.0, "target_x": -8335, "target_y": -3073, "target_z": 694, "target_heading": 119.0, "target_zone_id": 15},
  ]
  assert (files["numbersWritten"], files["numbersDeleted"], files["zonePoints"]) == ([30, 70, 120], [70, 500], points)
  # Without zone lines or a record there is nothing to delete or insert.
  bare = written(rowZone, skyFog)[1]
  assert (bare["zonePointsDeleted"], bare["zonePoints"], bare["numbersWritten"], bare["numbersDeleted"]) == ([], [], [], [])
  with pytest.raises(ValueError, match="The record holds testhollow's version-0 zone_points rows as rowsFiles gives them"):
    written(rowZone, skyFog, record=[earlier[0] | {"zone": "qeynos2"}])
  with pytest.raises(ValueError, match="numbered 6554"):
    written(rowZone, skyFog, [zoneLine(6554, [0, 0, 0], [1, 1, 1], lines[0]["target"])])
  with pytest.raises(ValueError, match="leads to 'nowhere', which the client does not register"):
    written(rowZone, skyFog, [zoneLine(1, [0, 0, 0], [1, 1, 1], lines[0]["target"] | {"zone": "nowhere"})])


def testTheSQLGuardsNameWhatTheyProtect():
  lines = [
    zoneLine(3, [0, 0, 0], [1, 1, 1], {"zone": "kithicor", "x": 1, "y": 2, "z": 3, "headingDegrees": 0}),
    zoneLine(7, [0, 0, 0], [1, 1, 1], {"zone": "testhollow", "x": 1, "y": 2, "z": 3, "headingDegrees": 0}),
    zoneLine(12, [0, 0, 0], [1, 1, 1], {"zone": "eastkarana", "x": 1, "y": 2, "z": 3, "headingDegrees": 0}),
    zoneLine(13, [0, 0, 0], [1, 1, 1], {"zone": "eastkarana", "x": 1, "y": 2, "z": 3, "headingDegrees": 0}),
  ]
  earlier = written(rowZone, skyFog, [lines[1], zoneLine(50, [0, 0, 0], [1, 1, 1], lines[0]["target"])])[0]["zonePoints"]
  files, read = written(rowZone, skyFog, lines, record=earlier)
  guards = {(guard["guard"], guard.get("target", guard.get("number"))): guard for guard in read["guards"]}
  assert list(guards) == [
    ("zoneID", None), ("rowID", None), ("duplicateRow", None), ("template", None), ("target", "eastkarana"), ("target", "kithicor"),
    ("foreignZonePoints", 30), ("foreignZonePoints", 70), ("foreignZonePoints", 120), ("foreignZonePoints", 130),
  ]
  assert guards[("zoneID", None)] | {"message": None} == {"guard": "zoneID", "zoneID": 901, "shortName": "testhollow", "message": None}
  assert guards[("zoneID", None)]["message"] == "zoneidnumber 901 belongs to a zone other than testhollow"
  assert (guards[("rowID", None)]["zoneID"], guards[("rowID", None)]["shortName"]) == (901, "testhollow")
  assert guards[("duplicateRow", None)]["shortName"] == "testhollow"
  assert (guards[("template", None)]["shortName"], guards[("template", None)]["template"]) == ("testhollow", "qeynos2")
  # Each zone line target needs its version-0 row under the client's id; this zone's own needs none.
  assert [(guards[("target", name)]["targetID"], guards[("target", name)]["message"]) for name in ("eastkarana", "kithicor")] == [
    (15, "zone line target eastkarana has no version-0 zone row with the client's id 15"), (20, "zone line target kithicor has no version-0 zone row with the client's id 20"),
  ]
  # Every number written now is guarded: any row of the zone there that is none an export of this short name wrote (as the record holds
  # them) is someone else's, or changed on the server, and is never overwritten, the record's numbers included.
  foreign = {number: guards[("foreignZonePoints", number)] for number in (30, 70, 120, 130)}
  assert {number: (guard["shortName"], guard["owned"]) for number, guard in foreign.items()} == {
    30: ("testhollow", []), 70: ("testhollow", [{column: value for column, value in decided(earlier[0]).items() if column != "number"}]),
    120: ("testhollow", []), 130: ("testhollow", []),
  }
  assert foreign[70]["message"] == "testhollow zone_points 70 is not zonewright's: renumber the zone line or remove that row"
  # The longest short name and row number still fit MariaDB's 128 characters.
  longest = [guard for guard in written(rowZone | {"shortName": "z" * 31}, skyFog, [zoneLine(6553, [0, 0, 0], [1, 1, 1], lines[0]["target"])])[1]["guards"] if guard["guard"] == "foreignZonePoints"]
  assert [guard["message"] for guard in longest] == ["z" * 31 + " zone_points 65530 is not zonewright's: renumber the zone line or remove that row"]


def testWaysInUpdateTheNeighboursRowAndGuardAMissingOne():
  entries = [
    {"name": "fromKarana", "kind": "zoneIn", "source": "placeEntry", "at": [10.0, 20.0, 3.0], "headingDegrees": 180.0, "fromZone": "eastkarana", "fromNumber": 40, "isolated": False},
    {"name": "fromKeep", "kind": "zoneIn", "source": "placeEntry", "at": [-5.5, 7.25, 0.0], "headingDegrees": 90.0, "fromZone": "highkeep", "fromNumber": 120, "isolated": False},
    {"name": "fromKithicor", "kind": "zoneIn", "source": "placeEntry", "at": [0.0, 0.0, 0.0], "headingDegrees": 0.0, "fromZone": "kithicor", "fromNumber": None, "isolated": False},
    {"name": "dock", "kind": "landing", "source": "placeEntry", "at": [0.0, 0.0, 0.0], "headingDegrees": 0.0, "fromZone": None, "fromNumber": None, "isolated": True},
    {"name": "safe point", "kind": "safePoint", "source": "setZoneProperties safePoint", "at": [10.5, -20.25, 3], "headingDegrees": 90},
  ]
  files, _ = written(rowZone, skyFog, entries=entries)
  read = serverRows.readWaysIn(files["waysIn"])
  assert (read["shortName"], read["archiveSHA256"]) == ("testhollow", archiveSHA256)
  # Each named neighbour row, guarded to exactly one row, points in the server's axes at a player's height over the entry's footing,
  # where the server puts a player's origin as at the safe point and the client settles it onto the footing; its heading in EQ units.
  assert read["rows"] == [
    {
      "fromZone": "eastkarana", "fromNumber": 40, "message": "ways in: eastkarana zone_points number 40 is not exactly one version-0 row",
      "target_zone_id": 901, "target_x": 20.0, "target_y": 10.0, "target_z": 3.0 + playerHeight, "target_heading": 384.0,
    },
    {
      "fromZone": "highkeep", "fromNumber": 120, "message": "ways in: highkeep zone_points number 120 is not exactly one version-0 row",
      "target_zone_id": 901, "target_x": 7.25, "target_y": -5.5, "target_z": playerHeight, "target_heading": 0.0,
    },
  ]
  # Counted rows, not ROW_COUNT(), which reads 0 when an update changes nothing; a number two rows hold is refused.
  assert files["waysIn"].count("(SELECT COUNT(*) FROM zone_points WHERE") == 2 and "<> 1 THEN SIGNAL" in files["waysIn"]
  assert "ROW_COUNT" not in files["waysIn"] and "ROW_COUNT" not in files["rows"]
  assert files["waysInToAdd"] == [{"entry": "fromKithicor", "fromZone": "kithicor"}]
  assert written(rowZone, skyFog, entries=entries[2:])[0]["waysIn"] is None


def testWaysInNameOnlyANeighboursRow():
  fromKeep = {"name": "fromKeep", "kind": "zoneIn", "source": "placeEntry", "at": [1.0, 2.0, 3.0], "headingDegrees": 0.0, "fromZone": "highkeep", "fromNumber": 65535}
  # zone_points.number is a uint16: its highest is named, and nothing past it, which no row can hold.
  assert [row["fromNumber"] for row in serverRows.readWaysIn(written(rowZone, skyFog, entries=[fromKeep])[0]["waysIn"])["rows"]] == [65535]
  for number in (65536, 10 ** 90, 0, True):
    with pytest.raises(ValueError) as refused:
      written(rowZone, skyFog, entries=[fromKeep | {"fromNumber": number}])
    assert str(refused.value) == f"zoneIn 'fromKeep' names fromNumber {number!r}: a zone_points number is a whole number from 1 to 65535"
  # The zone's own rows are its zone lines', which <short>.sql deletes and writes: a ways-in update there would write one row twice.
  with pytest.raises(ValueError, match="names testhollow zone_points 10, this zone's own row, which testhollow.sql writes from its zone lines"):
    written(rowZone, skyFog, entries=[fromKeep | {"fromZone": "testhollow", "fromNumber": 10}])


def clientStatements(text):
  """text split as the mariadb command-line client splits a file it reads: a DELIMITER line sets the delimiter, which ends a statement
  only outside quotes and -- comments."""
  delimiter, statements, pending, quote = ";", [], "", None
  for line in text.split("\n"):
    if quote is None and not pending.strip() and line.startswith("DELIMITER "):
      delimiter = line.removeprefix("DELIMITER ")
      statements.append(line)
      continue
    index = 0
    while index < len(line):
      character = line[index]
      if quote is None and line.startswith("-- ", index):
        break
      if quote is None and line.startswith(delimiter, index):
        statements.append(pending.strip())
        pending, index = "", index + len(delimiter)
        continue
      if quote is not None and character == "\\":
        pending, index = pending + line[index:index + 2], index + 2
        continue
      if character in "'\"`":
        quote = character if quote is None else None if character == quote else quote
      pending, index = pending + character, index + 1
    pending += "\n"
  assert quote is None and not pending.strip()
  return statements


def testEveryWriteRunsInsideTheGuardedTransaction():
  zone = rowZone | {"longName": "Ends; here // END// -- not a comment"}
  lines = [zoneLine(3, [0, 0, 0], [1, 1, 1], {"zone": "kithicor", "x": 1, "y": 2, "z": 3, "headingDegrees": 0})]
  entries = [{"name": "fromKeep", "kind": "zoneIn", "source": "placeEntry", "at": [1.0, 2.0, 3.0], "headingDegrees": 0.0, "fromZone": "highkeep", "fromNumber": 120}]
  files, read = written(zone, skyFog, lines, entries, record=written(zone, skyFog, lines)[0]["zonePoints"])
  assert read["zoneInsert"]["written"]["long_name"] == zone["longName"]
  writes = {"rows": ["INSERT", "UPDATE", "DELETE", "INSERT"], "waysIn": ["UPDATE"]}
  for name, kinds in writes.items():
    statements = clientStatements(files[name])
    # The client reads one compound statement and nothing outside it, so no write runs after a guard's error, whether the client stops
    # at the error or goes on (a sourced file, --force).
    assert len(statements) == 3 and (statements[0], statements[2]) == ("DELIMITER //", "DELIMITER ;") and statements[1].startswith("BEGIN NOT ATOMIC\n"), name
    inner = clientStatements(statements[1].removeprefix("BEGIN NOT ATOMIC").removesuffix("END"))
    # Any error, a guard's SIGNAL among them, rolls the transaction back and is raised again; every guard runs before the first write.
    assert inner[:4] == ["DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK", "RESIGNAL", "END", "START TRANSACTION"] and inner[-1] == "COMMIT", name
    body = [statement.split()[0] for statement in inner[4:-1]]
    firstWrite = min(body.index(kind) for kind in set(kinds))
    assert set(body[:firstWrite]) == {"IF", "END"} and body[firstWrite:] == kinds, name


def asStored(row):
  """A zone_points row as the table keeps it: its FLOAT columns in float32."""
  floats = {"y", "x", "z", "heading", "target_y", "target_x", "target_z", "target_heading", "buffer"}
  return [struct.unpack("<f", struct.pack("<f", row[column]))[0] if column in floats else row[column] for column in serverRows.zonePointColumns]


def zonePointsTable(*rows):
  """A sqlite stand-in for the server's zone_points table, holding rows as the table stores them."""
  database = sqlite3.connect(":memory:")
  database.execute(f"CREATE TABLE zone_points ({', '.join(serverRows.zonePointColumns)})")
  for row in rows:
    database.execute(f"INSERT INTO zone_points VALUES ({', '.join('?' * len(serverRows.zonePointColumns))})", asStored(row))
  return database


def applyZonePoints(database, rowsText):
  """A rows file's zone_points statements run on the stand-in as MariaDB runs them: the guards on zone_points first, and only when none
  signals, the deletes, then the rows inserted, stored as the table stores them. The numbers whose guards signal."""
  guards = [line.removeprefix("  IF ").split(" THEN SIGNAL ")[0] for line in rowsText.splitlines() if line.startswith("  IF EXISTS (SELECT 1 FROM zone_points ")]
  signalled = [int(re.search(r" AND number = ([0-9]+)", guard).group(1)) for guard in guards if database.execute(f"SELECT {guard}").fetchone()[0]]
  if signalled:
    return signalled
  for line in rowsText.splitlines():
    if line.startswith("  DELETE FROM zone_points "):
      database.execute(line.strip().removesuffix(";"))
  for row in serverRows.readRows(rowsText)["zonePoints"]:
    database.execute(f"INSERT INTO zone_points VALUES ({', '.join('?' * len(serverRows.zonePointColumns))})", asStored(row))
  return []


def testRowsZonewrightDidNotWriteAreNeverDeleted():
  slot = rowZone | {"shortName": "neighborhood", "zoneId": 712}
  theirs = {column: 0 for column in serverRows.zonePointColumns} | {"zone": "neighborhood", "content_flags": None, "content_flags_disabled": None}
  # The slot's own rows, which no export wrote: a lobby row at 10 and a telepad at 20.
  database = zonePointsTable(
    theirs | {"number": 10, "x": -2940.0, "y": 2035.0, "z": 6.0, "target_zone_id": 344, "target_z": 2.0},
    theirs | {"number": 20, "x": -2900.0, "y": 2000.0, "z": 6.0, "target_zone_id": 712, "target_x": 100.0, "target_y": 100.0, "target_z": 3.0},
  )

  def held():
    return database.execute("SELECT number, x, y, z, target_zone_id FROM zone_points ORDER BY number, target_zone_id").fetchall()

  toQeynos = {"zone": "qeynos2", "x": 1944.74, "y": 871.87, "z": -268, "headingDegrees": 10}
  first, fifth = zoneLine(1, [0.1, 0.2, 0.3], [2.35, 4.45, 6.55], toQeynos), zoneLine(5, [0.1, 0.2, 0.3], [2.35, 4.45, 6.55], toQeynos)
  lobby = written(slot, skyFog, [first])[0]
  # The lobby's row stands at 10: the guard there signals, so nothing is written, yet the export was, and the record holds 10.
  assert applyZonePoints(database, lobby["rows"]) == [10]
  record = list(lobby["zonePoints"])
  # Renumbered as the guard says: the record's row at 10 is deleted only as written, so the lobby's row stays.
  renumbered = written(slot, skyFog, [fifth], record=record)[0]
  assert applyZonePoints(database, renumbered["rows"]) == []
  assert [(number, target) for number, _, _, _, target in held()] == [(10, 344), (20, 712), (50, 2)]
  record += renumbered["zonePoints"]
  # Back at 1, which the record holds: the lobby's row there is still not one an export wrote, and is refused, never overwritten.
  both = written(slot, skyFog, [first, fifth], record=record)[0]
  assert applyZonePoints(database, both["rows"]) == [10]
  # Once the owner removes it, the export applies, replacing its own row at 50 as the table stored it.
  database.execute("DELETE FROM zone_points WHERE number = 10")
  assert applyZonePoints(database, both["rows"]) == []
  assert [(number, target) for number, _, _, _, target in held()] == [(10, 2), (20, 712), (50, 2)]
  record += both["zonePoints"]
  # A moved line replaces its own row and a removed line's row goes; the telepad stays.
  moved = written(slot, skyFog, [zoneLine(1, [10, 10, 0], [12, 12, 6], toQeynos)], record=record)[0]
  assert applyZonePoints(database, moved["rows"]) == []
  assert held() == [(10, 11.0, 11.0, 3.0, 2), (20, -2900.0, 2000.0, 6.0, 712)]


def testTheReadersRefuseWhatTheWritersNeverWrite():
  lines = [zoneLine(3, [0, 0, 0], [1, 1, 1], {"zone": "kithicor", "x": 1, "y": 2, "z": 3, "headingDegrees": 0})]
  entries = [{"name": "fromKeep", "kind": "zoneIn", "source": "placeEntry", "at": [1.0, 2.0, 3.0], "headingDegrees": 0.0, "fromZone": "highkeep", "fromNumber": 120}]
  files, _ = written(rowZone, skyFog, lines, entries, record=written(rowZone, skyFog, lines)[0]["zonePoints"])
  rows, waysIn = files["rows"], files["waysIn"]
  handler = serverRows.blockStart[2] + "\n"
  refusals = {
    "rowCount": (serverRows.readWaysIn, waysIn.replace("(SELECT COUNT(*) FROM zone_points", "(SELECT ROW_COUNT() FROM zone_points")),
    "extra": (serverRows.readRows, rows + "DROP TABLE zone;\n"),
    "column": (serverRows.readRows, rows.replace("note, ", "", 1)),
    "guard": (serverRows.readRows, rows.replace("AND version = 0) > 1 THEN", "AND version = 0) > 2 THEN")),
    "template": (serverRows.readRows, rows.replace("templateRow.ruleset", "templateRow.maxclients")),
    "points": (serverRows.readRows, rows.replace("4294967295, -1, -1, NULL, NULL, 0, 0, 0)", "4294967295, -1, -1, NULL, NULL, 0, 0)")),
    "handler": (serverRows.readRows, rows.replace(handler, "")),
    "waysInHandler": (serverRows.readWaysIn, waysIn.replace(handler, "")),
    "outside": (serverRows.readRows, rows.replace("  UPDATE zone SET", "END//\nDELIMITER ;\nUPDATE zone SET")),
    "deleteByNumber": (serverRows.readRows, re.sub(r"AND number = 30 AND \(.+\);", "AND number = 30;", rows)),
  }
  messages = {}
  for name, (reader, text) in refusals.items():
    with pytest.raises(ValueError) as refused:
      reader(text)
    messages[name] = str(refused.value)
  assert "expected the guard counting the neighbour's row" in messages["rowCount"]
  assert "expected the end of the file, got 'DROP TABLE zone;'" in messages["extra"]
  assert "does not name every column of zone but id" in messages["column"]
  assert "expected a guard, got \"  IF (SELECT COUNT(*) FROM zone WHERE short_name = 'testhollow' AND version = 0) > 2 THEN" in messages["guard"]
  assert "fills ruleset from templateRow.maxclients" in messages["template"]
  assert "a zone_points row of 22 values, not 23" in messages["points"]
  assert all(f"expected {serverRows.blockStart[2]!r}, got '  START TRANSACTION;'" in messages[name] for name in ("handler", "waysInHandler"))
  assert "expected the zone row's update, got 'END//'" in messages["outside"]
  assert "expected a zone_points delete, got \"  DELETE FROM zone_points WHERE zone = 'testhollow' AND version = 0 AND number = 30;\"" in messages["deleteByNumber"]


async def namedZone(session, folder, properties):
  """Ground saved as rowtest.blend, with the given zone properties."""
  await freshScene(session)
  await groundGrid(session, folder, size=64)
  await session.expectSuccess("setZoneProperties", properties)
  await session.expectSuccess("saveFile", {"path": str(folder / "rowtest.blend")})


def testSetZonePropertiesRefusesBadIDsAndNames(stageBlenderServer):
  bad = [
    {"zoneId": 0}, {"zoneId": 1000}, {"zoneId": 997}, {"zoneId": 407}, {"shortName": "highpasshold"}, {"shortName": "row_test"}, {"shortName": "a" * 32},
    {"longName": "Row\tTest"}, {"longName": "R" * 128}, {"timeType": "night"}, {"entryGate": {"minStatus": "80"}}, {"entryGate": {"minStatus": 80, "zoneFlag": True}},
    {"entryGate": {"zoneFlag": "keyed"}}, {"entryGate": {"minStatus": 256}}, {"entryGate": "closed"}, {"serverTemplate": "nowhere"}, {"serverTemplate": "rowtest"},
  ]

  async def steps(session):
    await freshScene(session)
    accepted = await session.expectSuccess("setZoneProperties", serverRowValues("rowtest") | {"longName": "R" * 127, "entryGate": {"minStatus": 255}})
    refusals = [await session.expectError("setZoneProperties", values) for values in bad]
    kept = (await session.expectSuccess("getSceneSummary", {"objectLimit": 0}))["zoneProperties"]
    reused = await session.expectSuccess("setZoneProperties", {"shortName": "highpasshold", "zoneId": 407, "entryGate": {"zoneFlag": True}})
    return accepted, refusals, kept, reused

  accepted, refusals, kept, reused = stageBlenderServer.session(steps)
  assert accepted["zone"] == serverRowValues("rowtest") | {"longName": "R" * 127, "entryGate": {"minStatus": 255}}
  ids = "zoneId is the zone's id, 1 to 999 (the client's AddZone takes no other) but not 997 (the client's run-time zone)"
  assert all(ids in refusal for refusal in refusals[:3]) and [refusal.count("got " + str(value)) for refusal, value in zip(refusals, (0, 1000, 997))] == [1, 1, 1]
  assert "The client registers id 407 to 'highpasshold', not 'rowtest'" in refusals[3]
  assert "The client registers 'highpasshold' under id 407, not 901" in refusals[4]
  assert all(f"shortName is {eqgFiles.zoneNameRule}" in refusal for refusal in refusals[5:7])
  assert all(f"longName is {serverRows.longNameRule}" in refusal for refusal in refusals[7:9])
  assert "timeType is one of ['indoorDungeon', 'outdoor', 'outdoorCity', 'dungeonCity', 'indoorCity', 'outdoorDungeon'], got 'night'" in refusals[9]
  assert all(serverRows.entryGateRule in refusal for refusal in refusals[10:15]) and "a text flag gates nothing" in refusals[12] and "got {'minStatus': 256}" in refusals[13]
  assert "serverTemplate 'nowhere' is not a zone the client registers" in refusals[15]
  assert "not this zone's own 'rowtest'" in refusals[16]
  # A refusal changes nothing; a short name the client registers is taken under its own id.
  assert kept == accepted["zone"]
  assert (reused["zone"]["shortName"], reused["zone"]["zoneId"], reused["zone"]["entryGate"]) == ("highpasshold", 407, {"zoneFlag": True})


def slotFindings(report):
  return [{key: value for key, value in finding.items() if key != "message"} for finding in report["findings"] if finding["finding"] in ("unregistered zone id", "reused slot")]


def testAnUnregisteredIDIsAFinding(stageBlenderServer, tmp_path):
  async def steps(session):
    await namedZone(session, tmp_path, serverRowValues("rowtest"))
    return {purpose: await session.expectSuccess("checkExport", {"path": str(tmp_path / "rowtest.eqg"), "purpose": purpose}) for purpose in ("test", "game")}

  reports = stageBlenderServer.session(steps)
  for purpose, report in reports.items():
    assert slotFindings(report) == [{"finding": "unregistered zone id", "zoneId": 901}], purpose
    assert "unregistered zone id" not in [failure["failure"] for failure in report["failures"]]
  message = [finding["message"] for finding in reports["game"]["findings"] if finding["finding"] == "unregistered zone id"][0]
  assert message == "The client registers no zone under id 901: the client needs a registration entry before it can load this zone"


def testAReusedSlotIsAFinding(stageBlenderServer, tmp_path):
  async def steps(session):
    await namedZone(session, tmp_path, serverRowValues("neighborhood", 712))
    return {purpose: await session.expectSuccess("checkExport", {"path": str(tmp_path / "neighborhood.eqg"), "purpose": purpose}) for purpose in ("test", "game")}

  reports = stageBlenderServer.session(steps)
  for purpose, report in reports.items():
    assert slotFindings(report) == [{"finding": "reused slot", "shortName": "neighborhood", "zoneId": 712}], purpose
  message = [finding["message"] for finding in reports["game"]["findings"] if finding["finding"] == "reused slot"][0]
  assert message == "The client registers 'neighborhood' already: this export replaces neighborhood's server maps and rows, which every instance of it uses"


def testAGameExportIsNamedByItsShortName(stageBlenderServer, tmp_path):
  exports = [("rowtest", "game"), ("elsewhere", "game"), ("elsewhere", "test")]

  async def steps(session):
    await namedZone(session, tmp_path, serverRowValues("rowtest"))
    return [await session.expectSuccess("checkExport", {"path": str(tmp_path / f"{name}.eqg"), "purpose": purpose}) for name, purpose in exports]

  named, elsewhere, test = stageBlenderServer.session(steps)

  def nameGaps(report, key):
    return [{name: value for name, value in entry.items() if name != "message"} for entry in report[key + "s"] if entry[key] == "archive not named by the short name"]

  assert nameGaps(named, "failure") == [] and nameGaps(named, "finding") == []
  # The client loads <short>.eqg by the id the server sends, so a game export under another name is refused, naming both ways out.
  assert nameGaps(elsewhere, "failure") == [{"failure": "archive not named by the short name", "shortName": "rowtest", "archive": "elsewhere"}]
  message = [failure["message"] for failure in elsewhere["failures"] if failure["failure"] == "archive not named by the short name"][0]
  assert message == "A game export is named by the zone's short name: export to rowtest.eqg, or set shortName 'elsewhere'"
  # A test export may take any name: listed, never refused.
  assert nameGaps(test, "failure") == [] and nameGaps(test, "finding") == [{"finding": "archive not named by the short name", "shortName": "rowtest", "archive": "elsewhere"}]


def testTheSkyMustBeTheShortNamesSky(stageBlenderServer, tmp_path):
  noon = {"hour": 12, "minute": 0}
  archive = {"path": str(tmp_path / "rowtest.eqg")}

  async def steps(session):
    await namedZone(session, tmp_path, serverRowValues("rowtest") | {"sky": {"type": "erudsxing"} | noon})
    reports = {"own sky": [await session.expectSuccess("checkExport", archive | {"purpose": purpose}) for purpose in ("game", "test")]}
    await session.expectSuccess("setZoneProperties", {"sky": {"type": "nowhere"} | noon})
    await session.expectSuccess("saveFile", {})
    reports["both default"] = [await session.expectSuccess("checkExport", archive | {"purpose": "game"})]
    await session.expectSuccess("setZoneProperties", {"shortName": "qeynos", "zoneId": 1})
    await session.expectSuccess("saveFile", {})
    reports["default for qeynos"] = [await session.expectSuccess("checkExport", {"path": str(tmp_path / "qeynos.eqg"), "purpose": "game"})]
    await session.expectSuccess("setZoneProperties", {"sky": {"type": "qeynos"} | noon})
    await session.expectSuccess("saveFile", {})
    reports["qeynos"] = [await session.expectSuccess("checkExport", {"path": str(tmp_path / "qeynos.eqg"), "purpose": "game"})]
    return reports

  reports = stageBlenderServer.session(steps)

  def skyGaps(report, key):
    return [{name: value for name, value in entry.items() if name != "message"} for entry in report[key + "s"] if entry[key] == "sky not the short name's"]

  game, test = reports["own sky"]
  assert skyGaps(game, "failure") == [{"failure": "sky not the short name's", "previews": "erudsxing", "game": "default"}]
  assert skyGaps(test, "finding") == [{"finding": "sky not the short name's", "previews": "erudsxing", "game": "default"}] and skyGaps(test, "failure") == []
  message = [failure["message"] for failure in game["failures"] if failure["failure"] == "sky not the short name's"][0]
  assert message.startswith("previews draw erudsxing's sky, the game draws default's: the client picks its sky by the short name 'rowtest'")
  assert skyGaps(reports["both default"][0], "failure") == []
  assert skyGaps(reports["default for qeynos"][0], "failure") == [{"failure": "sky not the short name's", "previews": "default", "game": "qeynos"}]
  assert skyGaps(reports["qeynos"][0], "failure") == []


def testZoneLineNumbersAbove6553AreRefused(stageBlenderServer, tmp_path):
  target = {"zone": "qeynos2", "x": 0, "y": 0, "z": 0, "headingDegrees": 0}

  async def steps(session):
    await namedZone(session, tmp_path, serverRowValues("rowtest"))
    refused = await session.expectError("placeZoneLine", {"number": 6554, "label": "far", "minimum": [0, 0, 0], "maximum": [4, 4, 4], "target": target})
    highest = await session.expectSuccess("placeZoneLine", {"number": 6553, "label": "far", "minimum": [0, 0, 0], "maximum": [4, 4, 4], "target": target})
    await session.expectSuccess("organize", {"renames": {"ATP_6553_far": "ATP_6554_far"}})
    lines = await session.expectSuccess("getZoneLines", {})
    await session.expectSuccess("saveFile", {})
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "rowtest.eqg"), "purpose": "test"})
    return refused, highest, lines, checked

  refused, highest, lines, checked = stageBlenderServer.session(steps)
  assert "to 6553 (its zone_points row is numbered number x 10, which must fit the row's 16-bit number), got 6554" in refused
  assert highest["name"] == "ATP_6553_far" and highest["number"] == 6553
  assert lines["errors"] == ["'ATP_6554_far' is numbered over 6553: its zone_points row, numbered x 10, cannot hold it"]
  assert [(failure["failure"], failure["object"]) for failure in checked["failures"]] == [("zone line", "ATP_6554_far")]


def testTargetsTheClientDoesNotRegisterAreRefused(stageBlenderServer, tmp_path):
  box = {"minimum": [20, -4, -5], "maximum": [24, 4, 20]}

  def target(zone):
    return {"zone": zone, "x": 0, "y": 0, "z": 0, "headingDegrees": 0}

  async def steps(session):
    await freshScene(session)
    await groundGrid(session, tmp_path, size=64)
    refusals = [
      await session.expectError("placeZoneLine", {"number": 1, "label": "away", "target": target("nowhere")} | box),
      await session.expectError("placeEntry", entryArguments("fromNowhere", [0, 0], "zoneIn", fromZone="nowhere")),
      await session.expectError("placeZoneLine", {"number": 1, "label": "loop", "target": target("rowtest")} | box),
      await session.expectError("placeEntry", entryArguments("fromHere", [0, 0], "zoneIn", fromZone="rowtest")),
    ]
    registered = await session.expectSuccess("placeZoneLine", {"number": 2, "label": "keep", "target": target("highkeep")} | box | {"minimum": [-24, -4, -5], "maximum": [-20, 4, 20]})
    await session.expectSuccess("setZoneProperties", serverRowValues("rowtest") | environment)
    loop = await session.expectSuccess("placeZoneLine", {"number": 1, "label": "loop", "target": target("rowtest")} | box)
    _, fromHere = await session.expectImage("placeEntry", entryArguments("fromHere", [0, 0], "zoneIn", fromZone="rowtest"))
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "rowtest.blend")})
    named = await session.expectSuccess("checkExport", {"path": str(tmp_path / "rowtest.eqg"), "purpose": "game"})
    await session.expectSuccess("setZoneProperties", {"shortName": "rowtwo"})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "rowtwo.blend")})
    renamed = await session.expectSuccess("checkExport", {"path": str(tmp_path / "rowtwo.eqg"), "purpose": "game"})
    return refusals, registered, loop, fromHere, named, renamed

  refusals, registered, loop, fromHere, named, renamed = stageBlenderServer.session(steps)
  assert "A zone line's target 'nowhere' is not a zone the client registers, and a client cannot load a zone it never registered" in refusals[0]
  assert "An entry's fromZone 'nowhere' is not a zone the client registers" in refusals[1]
  # The zone's own short name needs setting first.
  assert all("set its shortName first (setZoneProperties; now unset)" in refusal for refusal in refusals[2:])
  assert registered["target"]["zone"] == "highkeep" and loop["target"]["zone"] == "rowtest" and fromHere["fromZone"] == "rowtest"
  # A short name changed after a line led back into the zone leaves that line leading nowhere the client registers.
  assert "zone line target unregistered" not in [failure["failure"] for failure in named["failures"]]
  unregistered = [failure for failure in renamed["failures"] if failure["failure"] == "zone line target unregistered"]
  assert [(failure["zoneLine"], failure["target"]) for failure in unregistered] == [("ATP_1_loop", "rowtest")]


@pytest.mark.clientData("clientFiles")
def testHighpassHoldsRowsFromItsScene(stageBlenderServer, tmp_path):
  clientRoot = Path(everquestClient)
  registrations = eqClientZones.loadClientZones(clientRoot, tmp_path)
  regions = eqgFiles.parseZone((clientRoot / "highpasshold.zon").read_bytes(), "highpasshold.zon")["regions"]
  lines = []
  for region in regions:
    if not eqgFiles.isZoneLine(region["name"]):
      continue
    number = eqgFiles.zoneLineNumber(region["name"])
    targetID, targetX, targetY, targetZ, heading = highpassPoints[number * 10]
    box = eqgFiles.regionBox(region)
    lines.append({
      "number": number, "label": region["name"].split("_", 2)[2],
      "minimum": [center - half for center, half in zip(box["center"], box["halfExtents"])], "maximum": [center + half for center, half in zip(box["center"], box["halfExtents"])],
      # Peridot's targets, from the server's axes and EQ units into the zone's axes and degrees.
      "target": {
        "zone": registrations.byID[targetID]["shortName"], "x": targetY, "y": targetX, "z": targetZ,
        "headingDegrees": "keep" if heading == eqAxes.keptHeading else (90 - heading * 360 / eqAxes.eqHeadingUnits) % 360,
      },
    })
  highpass = {
    "safePoint": [-148, -219, -24, 0], "underworld": -275, "minClip": 50, "maxClip": 1500, "fogOn": False, "sky": {"type": "highpasshold", "hour": 12, "minute": 0},
    "shortName": "highpasshold", "zoneId": 407, "longName": "Highpass Hold", "timeType": "outdoor", "entryGate": "open", "serverTemplate": "highkeep",
  }

  async def steps(session):
    await freshScene(session)
    stored = await session.expectSuccess("setZoneProperties", highpass)
    for line in lines:
      await session.expectSuccess("placeZoneLine", line)
    zone = (await session.expectSuccess("getSceneSummary", {"objectLimit": 0}))["zoneProperties"]
    zoneLines = (await session.expectSuccess("getZoneLines", {}))["zoneLines"]
    return stored, zone, zoneLines

  stored, zone, zoneLines = stageBlenderServer.session(steps)
  files = serverRows.rowsFiles(zone, zoneLines, [], registrations.idsByShortName(), stored["sky"]["environment"]["fogColor"], [], archiveSHA256)
  read = serverRows.readRows(files["rows"])
  row = read["zoneInsert"]["written"]
  # Peridot's row, its safe point from the zone's y and x.
  peridot = {"zoneidnumber": 407, "short_name": "highpasshold", "safe_x": -219, "safe_y": -148, "safe_z": -24, "underworld": -275, "minclip": 50, "maxclip": 1500, "sky": 1, "ztype": 0}
  assert {column: row[column] for column in peridot} == peridot
  assert read["zoneUpdate"]["values"]["safe_x"] == -219 and read["zoneUpdate"]["shortName"] == "highpasshold"
  # By design not Peridot's expansion 12: open to every account (0, bounds -1), the entry gate deciding who enters.
  assert (row["expansion"], row["min_expansion"], row["max_expansion"]) == (0, -1, -1)
  points = read["zonePoints"]
  assert [point["number"] for point in points] == [100, 200, 300, 400, 500]
  assert [point["target_zone_id"] for point in points] == [15, 20, 6, 6, 395]
  assert [(point["target_zone_id"], point["target_x"], point["target_y"], point["target_z"], point["target_heading"]) for point in points] == [highpassPoints[number] for number in (100, 200, 300, 400, 500)]
  # Each row stands inside its box, where Peridot's hand-placed ones need not.
  for point, line in zip(points, sorted(lines, key=lambda found: found["number"])):
    zonePoint = eqAxes.zoneFromServer([point["x"], point["y"], point["z"]])
    assert all(line["minimum"][axis] <= zonePoint[axis] <= line["maximum"][axis] for axis in range(3)), line["label"]
  assert [guard["target"] for guard in read["guards"] if guard["guard"] == "target"] == ["eastkarana", "highkeep", "kithicor", "moors"]
