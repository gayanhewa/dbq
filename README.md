# dbq

![ci](https://github.com/gayanhewa/dbq/actions/workflows/ci.yml/badge.svg)

Read-only SQL against Oracle, MySQL, Postgres or SQL Server, printing toon or
json.

One Python file, five pure-Python dependencies. No driver needs a native
client library: `oracledb` runs in thin mode and speaks the Oracle wire
protocol directly, and `pymysql`, `pg8000` and `python-tds` are pure Python.
Built for coding agents to query real data safely, and pleasant enough for
humans.

## Install

```bash
# nix
nix profile install github:gayanhewa/dbq
# or as a flake input
inputs.dbq.url = "github:gayanhewa/dbq";

# uv / pipx
uv tool install git+https://github.com/gayanhewa/dbq
pipx install git+https://github.com/gayanhewa/dbq
```

## Read-only is enforced by the database

On Oracle, MySQL and Postgres every query runs inside a read-only transaction:
`SET TRANSACTION READ ONLY` on Oracle and Postgres, `START TRANSACTION READ
ONLY` on MySQL. A write fails server-side even when the account has permission:

```
$ mysql -uroot ... -e "START TRANSACTION READ ONLY; DELETE FROM widget WHERE id=1;"
ERROR 1792 (25006): Cannot execute statement in a READ ONLY transaction.

$ psql ... -c "BEGIN; SET TRANSACTION READ ONLY; DELETE FROM widget WHERE id=1;"
ERROR:  cannot execute DELETE in a read-only transaction   (SQLSTATE 25006)
```

That is the guarantee on those three. The SELECT-only check in `dbq` runs
first, but only so the error is clearer than the driver's. It is not what makes
this safe. Do not weaken the transaction mode on the assumption the string
check will hold; SQL has too many ways to hide a write.

SQL Server is weaker. It has no read-only transaction mode. There, `dbq` opens
an explicit transaction and always rolls it back, whatever happened inside it.
So the order flips: the SELECT-only statement check is the first line of
defence and the rollback is the second. Side effects that survive a rollback
are not covered: consumed identity and sequence values, and anything a stored
procedure does with its own commits. Where you can, point `dbq` at a login that
only has read permissions on SQL Server, and treat the tool's own guard as a
backstop rather than the guarantee.

## Config

`~/.config/dbq/config.toml` (override the path with `DBQ_CONFIG`):

```toml
default = "work"

[profiles.work]
driver   = "oracle"
env_file = "~/code/app/.env"          # read the DSN from an existing .env
env_var  = "ORACLE_CONNECTION"
schema   = "APP_SCHEMA"               # optional, substituted for {{schema}}

[profiles.local]
driver = "mysql"
dsn    = "mysql://root:pw@127.0.0.1:3306/appdb"

[profiles.analytics]
driver = "postgres"
dsn    = "postgres://reader:pw@127.0.0.1:5432/analytics"
schema = "public"

[profiles.legacy]
driver   = "sqlserver"
env_file = "~/code/legacy/.env"
env_var  = "SQL_CONNECTION"           # e.g. Server=host,1433;Database=app;User Id=ro;Password=pw
```

`driver` is one of `oracle`, `mysql`, `postgres` or `sqlserver`. A profile
either carries `dsn` inline or names an existing `.env` to read it from.
Pointing at a `.env` you already have avoids a second copy of the same
credential going stale.

## Use

```bash
dbq --list-profiles
dbq --tables
dbq --describe ORDERS
dbq --profile work --sql "SELECT * FROM {{schema}}.TENANT_SETTING"
dbq --profile work --file query.sql --json

# no config needed
dbq --driver mysql --dsn 'mysql://root:pw@127.0.0.1:3306/app' --sql 'SELECT 1'
dbq --driver postgres --dsn 'postgres://reader:pw@127.0.0.1:5432/app' --sql 'SELECT 1'
dbq --driver sqlserver --dsn 'Server=127.0.0.1,1433;Database=app;User Id=sa;Password=pw' --sql 'SELECT 1'
dbq --driver oracle --env-file ./.env --env-var ORACLE_CONNECTION --sql '...'
```

Multi-line SQL goes in a file. Passing it through shell quoting is how it ends
up mangled.

`--tables` and `--describe` work on all four drivers. Oracle reads
`all_tables` and `all_tab_columns`; the other three read `information_schema`.
Postgres compares the table name case-insensitively. SQL Server looks in the
login's default schema, usually `dbo`, unless the profile sets `schema`.

## Connection string formats

All of these parse, so you can point at whatever the app already uses:

| Form | Example |
|---|---|
| .NET (Oracle) | `User Id=SCOTT;Password=tiger;Data Source=host:1521/ORCLPDB1` |
| .NET (SQL Server) | `Server=host,1433;Database=app;User Id=sa;Password=pw` |
| URL | `mysql://root:pw@127.0.0.1:3306/appdb` |
| URL | `postgres://user:pw@host:5432/db` (also `postgresql://`) |
| URL | `mssql://user:pw@host:1433/db` (also `sqlserver://`) |
| Oracle EZ-connect | `scott/tiger@host:1521/ORCLPDB1` |

`Server`, `Initial Catalog`, `Uid`, `Pwd` and friends are accepted as aliases.
The SQL Server .NET form puts a comma, not a colon, before the port.
`Data Source=host,1433` and a `tcp:` prefix on the host also work.
`Encrypt` and `TrustServerCertificate` are accepted and ignored. Integrated or
trusted authentication is not supported; use a SQL login.

When the port is missing, the driver's default applies: 1521 for Oracle, 3306
for MySQL, 5432 for Postgres, 1433 for SQL Server.

## Output

toon by default: a header naming the columns once, then bare rows. That costs
far fewer tokens than a table when an agent is reading it. `--json` switches.

```
profile: work
driver: oracle
host: db.example.com
schema: APP_SCHEMA
count: 3
rows[3]{ID,NAME,NOTE}:
  1,alpha,null
  2,beta,"has,comma"
  3,gamma,x
```

Cells are quoted only when they contain a comma, quote or newline. `null` is
distinct from the empty string. An empty result prints `(no rows)` rather than
nothing, so "no matches" never looks like "the query failed".

`--max-rows` defaults to 100; when it truncates it says so in the header rather
than silently cutting.

## Agent skill

`skills/dbq/SKILL.md` is a ready-made skill for coding agents (written for
Claude Code, portable to anything that takes markdown instructions). It
teaches the discipline the tool assumes: use profiles instead of pasting
credentials, `--describe` before writing SQL against an unseen table, how to
read toon output, and that `count: 0` is an answer rather than a failure.

The skill only teaches the agent how to use `dbq`. The binary still has to be
on PATH, so install it first (see above). Then pick whichever fits your agent:

```bash
# any agent that reads SKILL.md folders: Claude Code, Codex, Copilot, Cursor, opencode, pi
npx skills add gayanhewa/dbq

# Claude Code, as a plugin
/plugin marketplace add gayanhewa/dbq
/plugin install dbq@dbq

# by hand
ln -s "$(pwd)/skills/dbq" ~/.claude/skills/dbq
```

The nix package also ships the skill at `share/dbq/skills/dbq`, so a nix
consumer can link it straight out of the store.

## CI

GitHub Actions runs on every push: unit tests first, then integration tests
against real Postgres, MySQL, SQL Server and Oracle containers.

## Notes

- Credentials are never written anywhere. The DSN is held in memory for one
  query, and error output has `password=` scrubbed before printing, because
  drivers sometimes echo the connection string in exceptions.
- The transaction is rolled back and the connection closed on every path,
  including errors.
