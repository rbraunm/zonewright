import hashlib
import json
import sys
from pathlib import Path

import pytest

from conftest import everquestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqClientZones


@pytest.mark.clientData("clientFiles")
def testClientZoneTableHasTheClientsRegistrations(tmp_path):
  clientRoot = Path(everquestClient)
  zones = eqClientZones.loadClientZones(clientRoot, tmp_path)
  assert len(zones.zones) == len(zones.byID) == len(zones.byShortName) == 522
  assert {name: zones.byShortName[name]["id"] for name in ("highpasshold", "neighborhood", "guildhall", "thulehouse2", "freeporteast", "qeynos2")} == {
    "highpasshold": 407, "neighborhood": 712, "guildhall": 345, "thulehouse2": 702, "freeporteast": 382, "qeynos2": 2,
  }
  assert zones.byShortName["neighborhood"] == {"id": 712, "shortName": "neighborhood", "longName": "Sunrise Hills", "expansion": 0, "flags": 0x800000, "eqstrID": 1216}
  # The registrar with player counts (tutorialb), and the first of two registrations of one id holding its slot: the arena's second,
  # under expansion 6, is the one AddZone turns away.
  assert zones.byShortName["tutorialb"] == {"id": 189, "shortName": "tutorialb", "longName": "The Mines of Gloomingdeep", "expansion": 0, "flags": 0x1000006, "eqstrID": 5856}
  assert (zones.byID[77]["shortName"], zones.byID[77]["expansion"]) == ("arena", 0)
  assert eqClientZones.runtimeZoneID not in zones.byID and max(zones.byID) <= eqClientZones.highestZoneID
  # Kept under the tooling root by the exe's hash, and read from there again.
  digest = hashlib.sha256((clientRoot / "eqgame.exe").read_bytes()).hexdigest()
  cachePath = tmp_path / "cache" / f"clientZones-{digest[:16]}.json"
  cached = json.loads(cachePath.read_text(encoding="ascii"))
  assert cached["exeSHA256"] == digest and cached["zones"] == zones.zones
  cached["zones"][0]["longName"] = "read from the cache"
  cachePath.write_text(json.dumps(cached), encoding="ascii")
  assert eqClientZones.loadClientZones(clientRoot, tmp_path).byID[1]["longName"] == "read from the cache"
  # Another build is refused before anything is read.
  other = tmp_path / "other"
  other.mkdir()
  (other / "eqgame.exe").write_bytes(b"MZ" + bytes(4094))
  with pytest.raises(ValueError, match="eqgame.exe is not the RoF2 build of May 10 2013 23:30:08"):
    eqClientZones.loadClientZones(other, tmp_path)
