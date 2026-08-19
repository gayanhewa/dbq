# dbq

Read-only SQL against Oracle or MySQL, printing toon or json.

One Python file, three pure-Python dependencies. Neither driver needs a native
client library: `oracledb` runs in thin mode and speaks the Oracle wire
protocol directly, `pymysql` is pure Python. Built for coding agents to query
real data safely, and pleasant enough for humans.

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

Every query runs inside a read-only transaction — `SET TRANSACTION READ ONLY`
on Oracle, `START TRANSACTION READ ONLY` on MySQL. A write fails server-side
**even when the account has permission**:

```
$ mysql -uroot ... -e "START TRANSACTION READ ONLY; DELETE FROM widget WHERE id=1;"
ERROR 1792 (25006): Cannot execute statement in a READ ONLY transaction.
```

That is the guarantee. The SELECT-only check in `dbq` runs first, but only so
the error is clearer than the driver's — it is not what makes this safe. Do not
weaken the transaction mode on the assumption the string check will hold; SQL
has too many ways to hide a write.

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
```

A profile either carries `dsn` inline or names an existing `.env` to read it
from. Pointing at a `.env` you already have avoids a second copy of the same
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
dbq --driver oracle --env-file ./.env --env-var ORACLE_CONNECTION --sql '...'
```

Multi-line SQL goes in a file. Passing it through shell quoting is how it ends
up mangled.

## Connection string formats

All of these parse, so you can point at whatever the app already uses:

| Form | Example |
|---|---|
| .NET | `User Id=SCOTT;Password=tiger;Data Source=host:1521/ORCLPDB1` |
| URL | `mysql://root:pw@127.0.0.1:3306/appdb` |
| Oracle EZ-connect | `scott/tiger@host:1521/ORCLPDB1` |

`Server`, `Initial Catalog`, `Uid`, `Pwd` and friends are accepted as aliases.

## Output

toon by default — a header naming the columns once, then bare rows, which costs
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

## Notes

- Credentials are never written anywhere. The DSN is held in memory for one
  query, and error output has `password=` scrubbed before printing, because
  drivers sometimes echo the connection string in exceptions.
- The transaction is rolled back and the connection closed on every path,
  including errors.
