import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqRecording

look = "\t".join(["HUF_ACTORDEF", "-1", "0", "0", "0", "4", "2", "4", "255", "255", "9", "9", "0", "1", "255", "-1", "-1", "0", "0", "00000000", "0:0:0:0:0", "0:0:0:0:0"])
recordingLines = [
  "# MQ2PeridotLive behavior recording",
  "[behavior] start\ttime",
  "  arrive\tms\tspawnId\tfirst",
  "time\t20261001-011228",
  "server\tbristle",
  "zone\tneighborhood\tSunrise Hills\t712\t712\t1634",
  "start\t20261001-011228",
  "arrive\t18\t7\t1",
  "spawn\t7\tChristine00\tChristine\tHousing Information\t1\t1\t1\t1\t99\t2015.62\t-2849.12\t3.32\t246\t2.88\t6\t0\t-1\t-1",
  "raw\t4\t00000000",
  f"appearance\t{look}",
  "arrive\t18\t9\t1",
  "spawn\t9\tPariator\tPariator\t\t0\t1\t12\t0\t84\t2089.07\t-2687.46\t2.94\t289\t2.88\t6\t0\t-1\t-1",
  f"appearance\t{look}",
  "prop\t18\tdoor\t131\tarrive\tIT20027\t0 2063.97 -2885.08 -3.53 397.0",
  "prop\t18\tground\t3\tarrive\tIT11543_ACTORDEF\t2013 -2753 0.66 40",
  "move\t500\t7\t2020\t-2850\t3.4\t250\t1",
  "look\t700\t7\t7\t1\t5.3",
  f"appearance\t{look.replace('HUF_ACTORDEF', 'HAF_ACTORDEF')}",
  "depart\t900\t7",
  "prop\t900\tground\t3\tdepart\tIT11543_ACTORDEF\t2013 -2753 0.66 40",
  "arrive\t1100\t7\t0",
  "brief\t7\tChristine00\tChristine\tHousing Information\t1\t1\t1\t1\t99\t2015\t-2849\t3.3\t10\t2.88\t6\t0\t-1\t-1",
  f"appearance\t{look}",
]


def writeRecording(tmp_path):
  path = tmp_path / "peridotLiveBehavior_bristle_neighborhood_1634_Pariator_20261001-011228.txt"
  path.write_text("\n".join(recordingLines) + "\n", encoding="utf-8")
  return path


def testZoneAtFollowsArrivalsMovesLooksAndDepartures(tmp_path):
  path = writeRecording(tmp_path)
  assert str(eqRecording.recordingStart(path)) == "2026-10-01 01:12:28"
  arrived = eqRecording.zoneAt(path, 100)
  assert (arrived["zone"], arrived["server"], arrived["instance"]) == ("neighborhood", "bristle", 1634)
  # The recording character (a player) is the camera, so only the NPC stands; both props are there.
  assert [(npc["name"], npc["x"], npc["y"], npc["heading"], npc["appearance"]["actorDef"]) for npc in arrived["npcs"]] == [("Christine00", 2015.62, -2849.12, 246, "HUF_ACTORDEF")]
  assert [(prop["kind"], prop["id"], prop["x"], prop["heading"]) for prop in arrived["props"]] == [("door", 131, 2063.97, 397.0), ("ground", 3, 2013, 40)]
  # A move puts her where she went; a look changes her race, height, and model.
  changed = eqRecording.zoneAt(path, 800)
  assert [(npc["x"], npc["y"], npc["heading"], npc["race"], npc["height"], npc["appearance"]["actorDef"]) for npc in changed["npcs"]] == [(2020, -2850, 250, 7, 5.3, "HAF_ACTORDEF")]
  # Departures take her and the ground item away; a brief arrival brings her back where it says, in the look that follows it.
  assert eqRecording.zoneAt(path, 1000)["npcs"] == []
  assert [prop["kind"] for prop in eqRecording.zoneAt(path, 1000)["props"]] == ["door"]
  returned = eqRecording.zoneAt(path, 1200)["npcs"]
  assert [(npc["x"], npc["heading"], npc["appearance"]["actorDef"]) for npc in returned] == [(2015, 10, "HUF_ACTORDEF")]


def testPropSizesComeFromDumpRowsStandingWhereThePropStands(tmp_path):
  (tmp_path / "doors.tsv").write_text("\n".join([
    "zone\tid\tname\ttype\tscaleFactor\tx\ty\tz\theading",
    "neighborhood\t131\tIT20027\t57\t150\t2063.96\t-2885.07\t-3.53\t397",
    "neighborhood\t155\tOBP_LOTSQUARE\t55\t1\t2051.92\t-2799.4\t3.82\t214",
    "neighborhood\t200\tIT22803\t55\t90\t100\t100\t0\t0",
  ]) + "\n", encoding="utf-8")
  (tmp_path / "ground.tsv").write_text("\n".join([
    "server\tzone\tinstance\tdropId\tname\tx\ty\tz\theading\tpitch\troll\tscale",
    "antonius\tneighborhood\t11219\t3\tIT11543_ACTORDEF\t2013\t-2753\t0.66\t40\t0\t0\t1.5",
    "antonius\tneighborhood\t11219\t4\tIT11544_ACTORDEF\t2000\t-2766\t0.06\t44\t10\t0\t1",
  ]) + "\n", encoding="utf-8")
  sizes = eqRecording.propSizes(str(tmp_path), {"zone": "neighborhood", "server": "bristle", "instance": 1634})
  door = {"kind": "door", "name": "IT20027", "id": 131, "x": 2063.97, "y": -2885.08}
  assert eqRecording.propSize(sizes, door) == (1.5, None)
  # The same id and name elsewhere is another instance's door, so its scale is not known.
  moved = eqRecording.propSize(sizes, {"kind": "door", "name": "IT22803", "id": 200, "x": 2036.15, "y": -2829.68})
  assert moved[0] is None and "doors.tsv" in moved[1]
  # A ground item matches any server and instance's row standing where it stands; a tilted one is not placed.
  assert eqRecording.propSize(sizes, {"kind": "ground", "name": "IT11543_ACTORDEF", "id": 3, "x": 2013, "y": -2753}) == (1.5, None)
  tilted = eqRecording.propSize(sizes, {"kind": "ground", "name": "IT11544_ACTORDEF", "id": 4, "x": 2000, "y": -2766})
  assert tilted[0] is None and "pitched 10.0" in tilted[1]
