"""The ledger against claudeperidot's real MariaDB 10.5.4, in a scratch database ledgerTest that the module makes and drops:
float, enum, set, bit and temporal round trips, an ENUM's invalid value refused at stage, the server's digests against the
ledger's, the guards inside apply and revert, per-statement check failures from the real mysql client, and reading the real
binary log, where a schema change's CREATE TABLE and DROP TABLE keep their tags. Their writes carry GTID domain 77, not the
ledger's 7, and stay out of peq, so drift never counts them.

They run only when a connection to claudeperidot is configured: facet's inventory holds claudeperidot and Claude's
login for it is in OpenBao."""
import struct

import pytest

import ledger
from ledgerDoubles import git, planText, rowChangeFields, schemaChangeFields, writeChange

testDatabase = "ledgerTest"
testDomain = 77


def configuredHost():
  try:
    from facetClient.inventory import defaultInventoryPath, loadInventory
    from facetClient.secretStore import SecretStoreUnreachableError, hasSecret
  except ImportError:
    return None
  host = loadInventory(defaultInventoryPath).get(ledger.hostName)
  if host is None:
    return None
  try:
    return host if hasSecret(host.credentialPath) else None
  except SecretStoreUnreachableError:
    return None
  return host


claudeperidot = configuredHost()
pytestmark = [pytest.mark.mariadb, pytest.mark.skipif(claudeperidot is None, reason="no connection to claudeperidot is "
  "configured")]

payloadsTable = """CREATE TABLE payloads (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ratio FLOAT NULL,
  precise DOUBLE NULL,
  amount DECIMAL(10,2) NULL,
  created DATETIME(6) NULL,
  flags SET('a','b','c') CHARACTER SET latin1 NULL,
  mood ENUM('calm','angry','sad') CHARACTER SET latin1 NULL,
  mask BIT(12) NULL,
  label VARCHAR(64) CHARACTER SET utf8mb4 NULL,
  note TEXT CHARACTER SET latin1 NULL,
  payload BLOB NULL,
  path VARCHAR(64) CHARACTER SET latin1 NULL,
  doubled DECIMAL(11,2) AS (amount * 2) VIRTUAL
) ENGINE=InnoDB"""
payloadRows = """INSERT INTO payloads (id, ratio, precise, amount, created, flags, mood, mask, label, note, payload, path) VALUES
  (0, 0.123456789, 0.30000000000000004e0, -12.50, '2026-10-11 09:30:17.250000', 'a,b', 'angry', b'101000000101',
    _utf8mb4 X'6E6169766520E2988320636166C3A9', _latin1 X'636166E9', X'00275CFF3B2D2D', 'C:\\\\Games'),
  (1, -0e0, -0e0, 0.00, '2026-10-11 09:30:00', '', 'calm', 0, '', NULL, X'', '\\\\'),
  (2, 3e38, 1e-300, 99.99, NULL, 'c', 'sad', 4095, 'NULL', 'O''Brien; DROP TABLE x; --', NULL, 'plain'),
  (3, 16777217, 123456789.12345679, NULL, '1999-12-31 23:59:59', NULL, NULL, NULL, NULL, NULL, 'x', NULL)"""


@pytest.fixture(scope="module")
def server():
  from facetClient.sshConnection import SSHConnection
  connection = SSHConnection(claudeperidot)
  admin = ledger.Server(connection, "mysql", testDomain)
  admin.sql(f"DROP DATABASE IF EXISTS {testDatabase};\nCREATE DATABASE {testDatabase} CHARACTER SET utf8mb4;\n", writes=True)
  try:
    yield ledger.Server(connection, testDatabase, testDomain)
  finally:
    admin.sql(f"DROP DATABASE IF EXISTS {testDatabase};\n", writes=True)
    connection.close()


@pytest.fixture
def payloads(server):
  server.sql(f"DROP TABLE IF EXISTS payloads;\n{payloadsTable};\n{payloadRows};\n", writes=True)
  return server


def ledgerOver(folder, server):
  return ledger.Ledger(folder, lambda: server, ledger.pushBranch)


def payloadChange(repository, server, files=None, where="id < 10"):
  writeChange(repository, "0001-payloads", rowChangeFields(scope=[{"table": "payloads", "where": where}]), files or {
    "apply.sql": "UPDATE payloads SET ratio = 0.5, mood = 'calm', mask = 1 WHERE id = 0;\n",
    "check.sql": "SELECT COUNT(*) AS applied FROM payloads WHERE id = 0 AND ratio = 0.5;\n"})
  (repository / "plan.md").write_text(planText(["- [ ] 1. The payloads [0001]"]), encoding="utf-8")
  git(repository, "add", "-A")
  git(repository, "commit", "-q", "-m", "the change")
  return ledgerOver(repository, server)


def ourTransactionsSince(server, position):
  """The binary log's transactions in the tests' GTID domain from position on, as the ledger reads them."""
  transactions = []
  parser = ledger.BinaryLogParser(transactions.append)
  server.stream(ledger.binaryLogCommand(position["file"], position["position"]), parser.feed)
  parser.finish()
  return [transaction for transaction in transactions if transaction.domain == testDomain]


def exactRows(server):
  return server.sql("SELECT id, CONCAT(ratio * 1e0), CONCAT(precise * 1e0), amount, created, flags, mood, mask + 0, HEX(label), "
    "HEX(note), HEX(payload), HEX(path), doubled FROM payloads ORDER BY id;\n")


def test_everyKindOfValueComesBackExactlyFromRevertSQL(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads)
  before = exactRows(payloads)
  assert before[0][1] == repr(struct.unpack("<f", struct.pack("<f", 0.123456789))[0])
  tables = ledgerUnderTest.fetchScope(ledgerUnderTest.find("0001"))
  assert "doubled" not in [column["name"] for column in tables[0]["columns"]]
  payloads.sql("DELETE FROM payloads;\nINSERT INTO payloads (id, ratio) VALUES (9, 1);\n", writes=True)
  payloads.sql("START TRANSACTION;\n" + "".join(f"{statement};\n" for statement in ledger.splitStatements(
    ledger.revertScript(tables))) + "COMMIT;\n", writes=True)
  assert exactRows(payloads) == before


def test_theServersDigestIsTheLedgersDigestOfTheRowsItSaved(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads)
  tables = ledger.describeScope(ledgerUnderTest.fetchScope(ledgerUnderTest.find("0001")))
  assert tables[0]["rowCount"] == 4
  assert ledgerUnderTest.scopeDigests(tables) == [tables[0]["digest"]]
  empty = [dict(tables[0], where="id > 100", digest=None)]
  assert ledgerUnderTest.scopeDigests(empty) == [None]


def test_applyAndRevertRunThroughTheirGuards(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads)
  before = exactRows(payloads)
  ledgerUnderTest.stage("0001")
  ledgerUnderTest.apply("0001")
  assert exactRows(payloads)[0][1] == "0.5"
  ledgerUnderTest.revert("0001")
  assert exactRows(payloads) == before


def test_anApplyThatWritesOutsideItsScopeRollsBack(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads, {
    "apply.sql": "UPDATE payloads SET ratio = 0.5 WHERE id = 0;\nUPDATE payloads SET note = 'outside' WHERE id = 3;\n",
    "check.sql": "SELECT COUNT(*) AS applied FROM payloads WHERE id = 0 AND ratio = 0.5;\n"}, where="id < 3")
  before = exactRows(payloads)
  ledgerUnderTest.stage("0001")
  with pytest.raises(ledger.LedgerError, match="ledger: 0001 apply wrote rows outside its scope"):
    ledgerUnderTest.apply("0001")
  assert exactRows(payloads) == before


def test_anApplyOverRowsChangedSinceStageRollsBack(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads)
  ledgerUnderTest.stage("0001")
  payloads.sql("UPDATE payloads SET note = 'edited' WHERE id = 1;\n", writes=True)
  changed = exactRows(payloads)
  with pytest.raises(ledger.LedgerError, match="changed since stage"):
    ledgerUnderTest.apply("0001")
  assert exactRows(payloads) == changed


def test_aRowWhoseEnumHoldsItsInvalidValueIsRefusedAtStage(repository, payloads):
  payloads.sql("SET SESSION sql_mode = '';\nUPDATE payloads SET mood = 'furious' WHERE id = 1;\n", writes=True)
  assert payloads.sql("SELECT mood + 0, HEX(CONCAT(mood)) FROM payloads WHERE id = 1;\n") == [["0", ""]]
  ledgerUnderTest = payloadChange(repository, payloads)
  with pytest.raises(ledger.LedgerError, match=r"column mood holds its ENUM's invalid value \(index 0"):
    ledgerUnderTest.stage("0001")
  assert not (repository / "changes" / "0001-payloads" / "saved.json").exists()


def test_eachFailingStatementIsReportedAtItsOwnIndex(payloads):
  rows, failures = payloads.sqlByStatement(["SELECT '0001', 1", "SELECT '0002', applied FROM nothere",
    "SELECT '0003', NULL", "SELECT\n'0004',\n2", "SELECT '0005', COUNT(*) FROM nothereEither"])
  assert rows == [["0001", "1"], ["0003", "NULL"], ["0004", "2"]]
  assert sorted(failures) == [1, 4]
  assert failures[1].startswith("ERROR 1146 (42S02): Table '?' doesn't exist")


def test_theBinaryLogShowsTheLedgersTaggedTransactionAndWhereEachStatementRan(repository, payloads):
  ledgerUnderTest = payloadChange(repository, payloads)
  ledgerUnderTest.requireBinaryLog()
  position = ledgerUnderTest.masterStatus()
  payloads.sql(f"DROP TABLE payloads;\n{payloadsTable};\n{payloadRows};\n", writes=True)
  ledgerUnderTest.stage("0001")
  ledgerUnderTest.apply("0001")
  ours = ourTransactionsSince(payloads, position)
  assert [statement[2] for transaction in ours for statement in transaction.statements
    if statement[1].startswith("CREATE TABLE payloads")] == [testDatabase]
  assert [transaction.writes for transaction in ours if transaction.writes] == [
    {(testDatabase, "payloads", "INSERT", None): 4}, {(testDatabase, "payloads", "UPDATE", "0001"): 1}]
  drift = ledger.DriftAccount(ledger.loadChanges(repository))
  for transaction in ours:
    drift.add(transaction)
  assert drift.problems == []


def test_aSchemaChangesCreateAndDropTableKeepTheirTagsInTheBinaryLog(repository, server):
  writeChange(repository, "0001-extra", schemaChangeFields(title="extra exists", scope=[{"table": "extra"}], schemaSteps=1), {
    "apply.sql": "CREATE TABLE extra (id INT NOT NULL PRIMARY KEY) ENGINE=InnoDB;\n", "revert.sql": "DROP TABLE extra;\n",
    "check.sql": "SELECT COUNT(*) AS applied FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = "
      "'extra';\n"})
  (repository / "plan.md").write_text(planText(["- [ ] 1. The extra table [0001]"]), encoding="utf-8")
  git(repository, "add", "-A")
  git(repository, "commit", "-q", "-m", "the change")
  ledgerUnderTest = ledgerOver(repository, server)
  position = ledgerUnderTest.masterStatus()
  ledgerUnderTest.stage("0001")
  ledgerUnderTest.apply("0001")
  ledgerUnderTest.revert("0001")
  assert [statement for transaction in ourTransactionsSince(server, position) for statement in transaction.statements] == [
    ("0001", "CREATE TABLE extra (id INT NOT NULL PRIMARY KEY) ENGINE=InnoDB", testDatabase),
    ("0001", "DROP TABLE `extra` /* generated by server */", testDatabase)]
