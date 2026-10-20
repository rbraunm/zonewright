# claudeperidot

claudeperidot is Claude's own EQEmu test server in the owner's home lab: a faithful copy of Peridot (the same EQEmu build, akk-stack, quests, maps and database) on VM 143 on labradorite, at `claudeperidot.strata.unseencolor.com` (10.111.111.235), LAN only and never published. Claude has GM status there, a spare RoF2 client (`S:\Research\EverQuest\housingclient`) and a control link (`rbraunm/claudelink`), and experiments freely. The owner's client, install and MacroQuest setup stay the owner's.

This folder is the change ledger: every change to claudeperidot, to Claude's side of the workstation, and to the lab hosts and devices claudeperidot needs, is written here and committed before it runs, so it can be checked, reverted, and carried to Peridot when it is meant for production. `plan.md` holds the goal, the ordered steps, the step in progress and the next one.

## Layout

```
claudeperidot/
  README.md           this file
  plan.md             goal, ordered steps, step in progress, next step; rewritten, never appended
  baseline.yaml       what the copy came from, and the binary-log position and table checksums drift starts from
  tableChecksums.json each table a ledger apply or revert wrote since the baseline, with its checksum after the latest one
  changes/
    template/         the starting point for a new change
    NNNN-<name>/
      change.yaml     what the change is (fields below)
      apply.sql       db: the change; rows in one transaction, schema one DDL statement per step
      check.sql       db: one row with a column named applied: 1 or 0, or the steps done for a schema change
      revert.sql      db: written by stage from the saved rows (on the host instead, for secrets); hand-written for a schema change
      saved.json      written by stage: the rows (or only their digests, for secrets), file hashes or table definitions
      applied.json    written by apply: the digests the rows or tables had after it ran
      intent.json     while an apply or revert runs: applying or reverting, committed and pushed before anything is written
      showCreate/     written by stage for a schema change: SHOW CREATE TABLE of each table before it
      peridot/        written by promote: the change's Peridot form
  tools/ledger.py     status | stage | apply | revert | drift | advance | promote | seed
  tools/zoneDeploy.py zonewright's deployZone and undeployZone, and its read-only zone slot reads, as ledger changes
  tests/              the ledger's tests
```

## Running the ledger

From the zonewright repository root, on the `claude` branch, with Python 3.14:

```
python claudeperidot/tools/ledger.py status
python claudeperidot/tools/ledger.py stage 0011
python claudeperidot/tools/ledger.py apply 0011
python claudeperidot/tools/ledger.py revert 0011
python claudeperidot/tools/ledger.py drift
python claudeperidot/tools/ledger.py advance
python claudeperidot/tools/ledger.py promote 0011 [--manifestVersion N]
python claudeperidot/tools/ledger.py seed --copyScript <path>
python claudeperidot/tools/ledger.py seed --baseline
```

A change is named by its number or its folder name. The ledger needs PyYAML (`requirements.txt`). For anything that reaches claudeperidot it also needs facet's client (`pip install -e` of `rbraunm/mcps/facet`) and reads facet's inventory (`inventory.yaml` in `rbraunm/mcps/facet`); it connects as Claude to the inventory's `claudeperidot` host. It logs to `%LOCALAPPDATA%\claudeperidot\logs\ledger.log` (rotated daily, 90 days); no value of a table holding secrets or personal data reaches the log.

- **status** reads every change's state live and stores none: each db change's `check.sql` (all in one script, so a check that fails or gives NULL marks only its own change), each quest or config change's files on the host, each client change's checks on the workstation. It prints each change as applied, not applied, partly applied or check failed, with its kind and disposition, an interrupted apply or revert (`intent.json`), and rows changed since apply, beside the step `plan.md` names as in progress. Lab changes show as recorded: their checks run through facet or the sonicwall MCP, by hand. It flags a step whose changes are applied but which is not ticked, a ticked step whose changes are not all applied, and an In progress section that does not name the first unticked step, and ends with the close-of-session list.
- **stage** saves what the change will touch and commits and pushes before anything runs. For a files change it keeps each host file it replaces or removes, as it stands, in the change's host folder (`/opt/eqemu/ledger/<change>/previous/`, root only), and saves every file's SHA-256 before and after in `saved.json`. For a row change it saves the rows the scope matches, writes `revert.sql` from them (delete what the scope matches, insert the saved rows back), and stores their digests; a scope table must be InnoDB, and an ENUM in a saved row must hold one of its members, not the invalid value (index 0) a write outside strict sql_mode leaves, which revert could not write back. For a schema change it saves `SHOW CREATE TABLE` and the table checksums; `revert.sql` is hand-written. For a quest or config change it checks that the commits are on the source clone's branch, pushes that branch, fetches on the host and fails unless every commit is then there, and saves each file's hash on the host now and at the newest commit. A client change's record is its `change.yaml`.
- **apply** refuses unless the change folder, `plan.md` and `tableChecksums.json` are committed and the change is staged, not applied and as staged. It then commits and pushes `intent.json` (applying), and only then writes. A row change runs in one transaction under `SERIALIZABLE`, with `NO_AUTO_VALUE_ON_ZERO` so a row with id 0 keeps it: guards that the scope's rows are as staged, a digest of every other row of the scope's tables, `apply.sql`, guards that those other rows are unchanged and that `check.sql` gives 1. Any failure rolls the whole transaction back. A schema change runs its remaining steps; when one fails, fixing the cause and running apply again resumes from the next step. A quest or config change fast-forwards the host checkout to its newest commit and, for a rendered config, runs its `render`; a client change runs its steps. Then apply checks the change, records the digests in `applied.json` and each written table's checksum in `tableChecksums.json`, ticks the plan steps whose changes are all applied, rewrites In progress and Next, removes `intent.json`, commits and pushes, and prints the change's activation.
- **revert** refuses when what the change touched changed since apply (the rows' digests differ from `applied.json`, the host's files differ, a client check fails), then writes `intent.json` (reverting) the same way. A row change's revert runs in one transaction between guards: the rows are as apply left them, and afterwards they are exactly the rows saved at stage, no other row of the scope's tables changed, and the check gives 0. A schema change runs the inverse of each step done, in reverse order. A quest or config change is undone by a revert commit made in the source clone (its SHA kept in `intent.json` until the record), pushed, and fast-forwarded on the host, so the host never holds a commit the source lacks and later changes still fast-forward; a client change runs its undo. Then revert unticks the plan steps that name the change, commits and pushes.
- **drift** compares `CHECKSUM TABLE` of every watched table with the baseline, overridden by `tableChecksums.json` (a table a schema change dropped is expected gone). Watched means every table except the player, state, login and bot tables (play writes them) and the runtime tables (below), but including the security and config tables EQEmu files under state or login (`login_api_tokens`, `login_server_admins`, `login_server_list_types`, `discord_webhooks`, `server_scheduled_events`, `player_event_log_settings`, `gm_ips`, `banned_ips`, `ip_exemptions`). It streams the binary log from the baseline's position: every write the ledger makes carries GTID domain 7 and a `/* ledger NNNN apply */` (or revert) tag that the binary log keeps (first in each statement, as the annotated rows and DDL keep it; in a `DROP TABLE`, which MariaDB rebuilds for the log keeping only a comment right after `DROP TABLE [IF EXISTS]`, there; stage refuses a schema step MariaDB logs without it: a view, trigger, routine or event, which it rebuilds with its definer, `CREATE TABLE ... SELECT`, and `DROP TABLES` or `DROP TEMPORARY TABLE`), so a ledger write to a table outside its change's scope is listed, and so is every other write to a watched table in `peq` and every other statement in `peq` (GM commands, Spire, the PEQ editor, a hand-run query, a statement inside versioned comments, a statement naming `peq` from a session in another database or none, which mysqlbinlog files under that session's database; mysqlbinlog prints a session's database only when it changes to one, so a statement before any `use` line ran in none and writes `peq` only by naming it; MariaDB logs `CREATE`, `ALTER` and `DROP DATABASE` without their session's database, so they count as the database they name). The runtime tables' row writes, a `TRUNCATE` of one in `peq` (world empties the trader and buyer tables at every boot), and Spire's migration of its own tables at start (`ALTER TABLE spire_...`) are reported apart, as runtime, counted by table and operation. A statement that touches no `peq` table (`CREATE`, `DROP`, `ALTER` or `RENAME USER`, `CREATE` or `DROP ROLE`, `GRANT`, `REVOKE`, `SET PASSWORD`, `SET DEFAULT ROLE`, `FLUSH`) is listed apart as a server account change, with its GTID, whatever database its session had. Neither is drift: it prints `drift: clean` when nothing else is listed. It refuses unless the server has `log_bin` on, `binlog_format` ROW and `binlog_annotate_row_events` on.
- **advance** runs drift and, only when it is clean, moves the baseline to the binary log's position between the table checksums and the log, and to those checksums, and empties `tableChecksums.json`. The provisioner sets the binary log to expire after 90 days, so advance at least that often; drift cannot read a log that has expired.
- **promote** writes a production change's Peridot form into its `peridot/` folder and commits it: `content.sql` (the change in one transaction, between a guard that Peridot still holds the rows the change was made against, the guard that no other row of the scope's tables changed, and a guard on its check; run it with the mysql command-line client, `mysql peq < content.sql`, whose `DELIMITER` command its guards use) and `check.sql` for a row change; a `ManifestEntry` for `database_update_manifest_custom.cpp` for a schema change (`--manifestVersion` is the next custom version there); `cherryPick.sh` for a quest change (fetch its commits into Peridot's quests checkout first); `edit.md` for a config change, to carry into the runbook or the provisioner. It refuses a change that is not production, not applied, or not cleared by the owner, a change on a table holding secrets or personal data, and any change while a significant production change awaits the owner.
- **seed --copyScript** runs the operations repository's database copy script (`.ps1` or `.py`), which streams Peridot's database into claudeperidot. Then boot world once (its migrations and rule injection write to the database), and run **seed --baseline**: it refuses unless the binary log is on in ROW format with annotated rows, and records the dump's SHA-256 and time, the `db_version` row, the binary-log position, the deployed akk-stack, code, quests and maps commits, and every watched table's checksum in `baseline.yaml`, and commits it. Both refuse once `baseline.yaml` exists.

The ledger reaches the database only through facet's `SSHConnection`, piping SQL into `sudo -n docker exec -i akk-stack-mariadb-1 sh -c '...mysql --comments...'`; the root password is expanded from `MYSQL_ROOT_PASSWORD` inside the container, so it never leaves the host or reaches a command line.

## Runtime tables

The tables world, zone, ucs and loginserver (EQEmu at ae4bd2833, Peridot's build) and Spire (v4.24.2) write on their own: at start, on a timer, or as play goes on. The first five are server tables the checksum watch would otherwise cover; the rest are player, state, login or bot tables, which drift never checksums, listed so that their writes show as runtime. shared_memory writes nothing on a converged database. Start writes that only add what is missing or remove what the code dropped leave their tables watched: `rule_values` (world, `world/world_boot.cpp:314-339`), `command_settings` and `command_subsettings` (zone, `zone/command.cpp:273-305`), `logsys_categories` (every process, `common/eqemu_logsys.cpp:617-719`), `player_event_log_settings` (`common/events/player_event_logs.cpp:16-83`), `db_version` (world's migrations), `variables` (shared_memory, only a `hotfix_` name, `shared_memory/main.cpp:136-141`) and the rows of `spire_settings` (Spire adds only a missing `AUTH_ENABLED`, `internal/spire/settings.go:36-65`).

| Table | Written by, when | Source |
|---|---|---|
| `eqtime` | world, every 10 minutes | `world/main.cpp:166`, `:443-447`; `common/database.cpp:1795-1810` |
| `spawn_events` | zone, at zone boot and each time an event fires (checked every EQ minute, 3 s) | `zone/zone.cpp:1200`; `zone/spawn2.cpp:733-788`, `:845-862`, `:1053-1086` |
| `spawn_condition_values` | zone, when an event changes a condition; world and zone, for expired instances | `zone/spawn2.cpp:767`, `:864-880`; `common/database_instances.cpp:496-510`, `:562-597` |
| `chatchannels` | ucs, a player's permanent channel (`Chat:MaxPermanentPlayerChannels`) | `ucs/chatchannel.cpp:112-125` |
| `spire_server_database_connections` | Spire, two whole-row saves at every start of any `spire` command, the launcher included (a fresh password ciphertext and `updated_at` each time) | `internal/spire/init.go:88-101`, `:210-285`, `:453-462` |
| `trader`, `buyer`, `buyer_buy_lines`, `buyer_trade_items` | world at boot (`TRUNCATE TABLE`); zone when it empties | `world/world_boot.cpp:294-295`; `common/database.cpp:2265-2273`; `common/repositories/buyer_repository.h:96-101`; `zone/entity.cpp:487-511` |
| `group_id`, `group_leaders`, `raid_members`, `raid_details`, `raid_leaders` | world at boot, every row | `world/world_boot.cpp:283-287`; `common/database.cpp:1152-1164`, `:1340-1353`, `:1371-1395`, `:1636-1639` |
| `guild_members` | world at boot, `online = 0` | `world/world_boot.cpp:289`; `common/repositories/guild_members_repository.h:194-198` |
| `inventory_snapshots` | world at boot, past `Character:InvSnapshotHistoryD` | `world/world_boot.cpp:291`; `common/database.cpp:1739-1754` |
| `merchantlist_temp` | world at boot (`TRUNCATE`) when `World:ClearTempMerchantlist` (false on claudeperidot) | `world/world_boot.cpp:344-347`; `common/repositories/merchantlist_temp_repository.h:47-51` |
| `bot_pet_buffs` | world at boot when `Bots:Enabled` | `world/world_boot.cpp:298-303` |
| `data_buckets`, `character_expedition_lockouts`, `character_task_timers` | world at boot and every 450 s, expired rows | `world/world_boot.cpp:271`, `:372-375`; `world/main.cpp:436-440`; `common/database.cpp:1413-1422` |
| `instance_list`, `instance_list_player`, `respawn_times`, `spawn2_disabled`, `zone_state_spawns`, `character_corpses` | world at boot and every 450 s, for expired instances | `world/world_boot.cpp:378`; `world/main.cpp:437`; `common/database_instances.cpp:562-597` |
| `respawn_times` | zone at zone boot (expired rows) and as its spawns cycle | `zone/zone.cpp:1189`; `common/repositories/respawn_times_repository.h:11-19`; `zone/spawn2.cpp:454-472` |
| `zone_state_spawns` | world at boot; zone at shutdown (`Zone:StateSavingOnShutdown`) | `world/world_boot.cpp:409-412`; `zone/zone.cpp:887-889`; `zone/zone_save_state.cpp:795-814` |
| `character_corpses`, `character_corpse_items` | world at boot, stale and instance corpses; zone, its corpse timers | `world/world_boot.cpp:355`, `:407`; `common/shareddb.cpp:1504-1516`; `zone/corpse.cpp:994-1066` |
| `dynamic_zones`, `dynamic_zone_members`, `dynamic_zone_lockouts` | world at boot and on its process timer, expired ones | `world/world_boot.cpp:369`; `world/dynamic_zone_manager.cpp:19-36`, `:120-156` |
| `shared_tasks`, `shared_task_members`, `shared_task_activity_state`, `shared_task_dynamic_zones` | world at boot and on its process timer, expired ones | `world/world_boot.cpp:404`; `world/shared_task_manager.cpp:283-286`, `:1718-1732`, `:1798-1820` |
| `character_parcels` | world daily, when `Parcel:EnablePruning` | `world/main.cpp:168`, `:422-433`; `common/database.cpp:2215-2219` |
| `player_event_logs` and its ETL tables (`player_event_loot_items`, `_merchant_sell`, `_merchant_purchase`, `_npc_handin`, `_npc_handin_entries`, `_trade`, `_trade_entries`, `_speech`, `_killed_npc`, `_killed_named_npc`, `_killed_raid_npc`, `_aa_purchase`) | world at boot and hourly, past retention, while `Logging:PlayerEventsQSProcess` is off | `common/events/player_event_logs.cpp:85-92`, `:932-998`, `:1042-1049` |
| `quest_globals` | zone at zone boot, expired rows | `zone/zone.cpp:1094`; `zone/zonedb.cpp:2844-2848` |
| `saylink` | zone, an NPC's bracketed speech (`Chat:AutoInjectSaylinksToSay`) | `zone/mob.cpp:5049-5050`; `common/say_link.cpp:384-415` |
| `mail` | ucs at boot, expired mail | `ucs/ucs.cpp:161`; `ucs/database.cpp:624-680` |
| `login_world_servers` | loginserver, at each world registration | `loginserver/world_server.cpp:438`, `:470-475` |

Spire's start also migrates its own tables with GORM's AutoMigrate (`internal/database/connections.go:41-79`). At v4.24.2 that writes `ALTER TABLE spire_settings MODIFY COLUMN setting varchar(190)` every time: GORM 1.24 sees the column's unique key but no `unique` tag on the model (`internal/models/spire_settings.go:9`), so it modifies the column, which keeps the key.

## Secrets and personal data

Rows of the player, state, login and bot tables (`account`, `login_accounts`, `login_api_tokens`, `discord_webhooks` among them), and of any table with a column named like a password, token, key, secret or webhook, never reach a commit. stage writes their `saved.json` and `revert.sql` to `/opt/eqemu/ledger/NNNN-<name>/` on claudeperidot (root only), before the stage commit, and commits only their digests and the two files' hashes; apply and revert refuse a host copy whose hash differs. MariaDB's errors from such a change are printed with every quoted value replaced by `?`. promote never writes such a change. A change's own files never hold a secret: a config change's secrets are filled on the host from the template and judged by the template's hash and the names of the filled keys, never their values; a client step generates or reads a secret itself and is handed to PowerShell on standard input, so no secret reaches a command line.

## Changes

Copy `changes/template` to `changes/<time>-<name>`, the time in UTC as `YYYYMMDD-HHMMSS` (so changes written in parallel never collide; 0001 to 0030 keep their numbers and sort first) and a drinkingCamelCase name, fill in `change.yaml` (the template's comments describe each form), and stage it.

| Field | Meaning |
|---|---|
| `title` | One line, stating the change |
| `why` | Why the change is made |
| `kind` | `db`, `quest`, `config`, `client`, `files` or `lab` |
| `scope` | `db`: tables and a `where` predicate matching the touched rows both before and after apply (a schema change: tables only); `quest`, `config`: files in the repository; `client`: workstation paths; `files`: the host files it writes, under the server's maps folder, in `files`' order; `lab`: what the change touches, in words |
| `disposition` | `fixture`, `exploration` or `production` |
| `significance` | `minor` or `significant` |
| `ownerClearance` | `pending`, or `ownerMessage`: the owner's message that cleared the change, quoted with its date. Only the owner's words clear a change, never Claude's judgment |
| `activation` | What makes the server see the change: `#reload rules`, a world restart, a full restart |
| `production` | `none` for fixtures and explorations; for production, `contentSQL` (rows), `customMigration` (schema), `questCommit`, `runbook` or `provisioner` |
| `commits` | `quest` and `config`: the commit SHAs, oldest first; empty otherwise |
| `author` | Who wrote the change: `session`, the Claude session's id (`session_...`, as in commit trailers), and `agent`, the subagent's id when a subagent wrote it |
| `schemaSteps` | A schema change: the number of DDL statements in `apply.sql` |
| `repository`, `source` | `quest` and `config`: the checkout's absolute path on claudeperidot, and the workstation clone the commits are made in |
| `rendered`, `render` | A config change the host fills with secrets: each live file with its template and the names of the keys the host fills, and the host command that renders them |
| `files` | `files`: each `{path, source, sha256}`, a host path and the workstation file installed there with its SHA-256, or `source` and `sha256` both null for a file the change removes |
| `steps`, `undo`, `checks` | `client`: PowerShell 5.1 scripts to apply and undo it, and checks of existence (file, directory, junction and its target, a Credential Manager entry, a persona row in MacroQuest's login.db), SHA-256, an ini key's value, or a name a DLL's export table lists. `lab`: the facet run or sonicwall calls made, their inverse, and checks with what they must show |

| Kind | Covers | Apply and revert | Check |
|---|---|---|---|
| `db` | database rows and schema | SQL, guarded | `check.sql` |
| `quest`, `config` | quest scripts, server settings; settings are templates whose secrets are filled in on the host and never committed | a commit fast-forwarded on the host; a revert commit made in the source | file hashes on the host; for a rendered setting, its template and the names of its filled keys |
| `client` | the spare client folder, Claude's files in the owner's MacroQuest tree, the link DLL, Credential Manager entries, the login.db persona row, MacroQuest's own MQ2Main build | the recorded steps and undo | existence, hash, ini value, or a DLL export |
| `files` | claudeperidot's maps folder, which no remote Claude pushes to carries | each file sent over SSH, its hash checked, installed owned as its folder is (mode 644); revert puts back the files stage kept, or removes what was absent | each file's SHA-256 on the host |
| `lab` | Peridot, olivine's DNS, the SonicWall | by hand, through facet or the sonicwall MCP | by hand: each check's call and what it must show |

| Disposition | Meaning | At session close |
|---|---|---|
| `fixture` | Part of claudeperidot's approved setup | Kept; never promoted |
| `exploration` | An experiment | Reverted |
| `production` | Meant for Peridot | Cleared by the owner; promoted |

## Deploying a zone

zonewright's `deployZone` and `undeployZone` reach claudeperidot only through `tools/zoneDeploy.py`, which takes a request on standard input and writes progress and its result as JSON lines. `deploy` reads the zone's id and short name in the zone table (another zone holding either refuses), writes `<UTC>-deploy<Short>Maps` (files: the zone's `.map`, `.wtr`, `.nav`) and, a second later, `-deploy<Short>Rows` (db: every row of the zone in `zone`, `zone_points`, `doors` and `object`, deleted and the export's inserted, the zone row copying the template's other columns), leaves out a part the server already holds as the export has it, reverts an earlier deploy of the zone still applied and takes it off the plan, names the new changes in the caller's plan step, commits and pushes, stages and applies each, then restarts EQEmu with Spire's launcher (`eqemu-server:launcher stop` until no world or zone process is left, `start` detached until world and a zone process run). `undeploy` reverts the zone's applied deploys, newest first, and restarts. `slot` reads (never writes) the zone rows holding an id or short name on claudeperidot or Peridot. Peridot is never a deploy's target. Its tests are `tests/testZoneDeploy.py`.

## Resuming after an interruption

1. `git pull` on `claude`.
2. Read `plan.md`: the goal, the step in progress and the next one.
3. `python claudeperidot/tools/ledger.py status`. Every change on the server was committed before it ran, and every apply and revert pushed its `intent.json` before it wrote, so status shows each one. A change shown with an interrupted apply or revert is finished by running that command again: when its check shows it done, the command writes only the record; when the write rolled back, it runs again.
4. `python claudeperidot/tools/ledger.py drift`, for anything written outside the ledger.

A wreck is rolled back to the Proxmox `baseline` snapshot, then the ledger's fixtures are applied again; or the binary log is replayed to a point in time.

## Closing a session

- Every exploration change is reverted.
- Every production change is cleared by the owner.
- The fixtures are listed (status prints them).
- `drift` is clean, and `advance` has moved the baseline to it.

## Tests

`python -m pytest claudeperidot/tests` from the repository root. They use real git repositories with bare remotes they really push to, real files, real PowerShell, a real POSIX shell over a temporary folder standing in for the host's `/opt/eqemu`, and a stand-in for claudeperidot's MariaDB 10.5.4 that evaluates the expressions the ledger sends as that server does, reads scripts as its mysql client does and writes a binary log as its mysqlbinlog prints it; they never reach a host. `testMariaDB.py` runs the ledger against claudeperidot's real MariaDB in a scratch database `ledgerTest` (GTID domain 77), and only when facet's client is importable, facet's inventory holds claudeperidot, and Claude's login for it is in OpenBao (from `claudeperidot`: `python -m pytest tests/testMariaDB.py`). Where the two disagree, the real server is right, and the ledger and the stand-in follow it. zonewright's own suite (`pytest` or `pytest tests`) does not collect them: zonewright's `pytest.ini` limits its test paths to `tests/`, and this folder has its own `pytest.ini`.
