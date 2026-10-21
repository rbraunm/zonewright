"""runLedger's refusals, and the real ledger's error for a change it does not hold coming back as an error. The ledger's own
behavior is tested in claudeperidot/tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import ledgerRun


def test_aCommandOutsideTheListIsRefused():
  with pytest.raises(ValueError, match="command is one of \\['status', 'stage', 'apply', 'revert'\\], got 'drift'"):
    ledgerRun.runLedger("drift", None)


@pytest.mark.parametrize("command, change", [("stage", None), ("apply", None), ("revert", None), ("status", "0003")])
def test_aChangeIsNamedForStageApplyAndRevertAndForNoOtherCommand(command, change):
  with pytest.raises(ValueError, match="change names the change for"):
    ledgerRun.runLedger(command, change)


@pytest.mark.parametrize("change", ["--help", "-x", "a b", "0003; ls", "", "../0003"])
def test_aChangeThatIsNotANumberOrFolderNameIsRefused(change):
  with pytest.raises(ValueError, match="change is a change's number or folder name"):
    ledgerRun.runLedger("revert", change)


def test_theLedgersRefusalOfAnUnknownChangeComesBackAsAnError():
  with pytest.raises(ValueError, match="ledger apply failed: ledger: no change 99999999-000000-noSuchChange in "):
    ledgerRun.runLedger("apply", "99999999-000000-noSuchChange")
