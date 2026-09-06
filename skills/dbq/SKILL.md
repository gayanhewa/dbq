---
name: dbq
description: Run read-only SQL against Oracle, MySQL, Postgres or SQL Server with the dbq CLI and read the results. Use when a question needs real data from a database: checking rows, inspecting a schema, confirming a fix landed, or grounding a claim about production-shaped data instead of guessing from code.
---

# dbq

`dbq` runs a single SELECT against a configured database and prints toon or
json. It cannot write. On Oracle, MySQL and Postgres every query runs inside a
read-only transaction, so the database itself rejects DML even when the account
could perform it. SQL Server has no such mode: there dbq rejects anything that
isn't a SELECT, runs the rest in a transaction and always rolls it back. That
guarantee is weaker, so on SQL Server stick to plain SELECTs and never call a
procedure.

Credentials never reach this session. A profile holds the connection string,
inline or read from a `.env` at query time, and you pass a profile name, never
a credential. Profiles live in `~/.config/dbq/config.toml`. Do not read that
file; you do not need what is in it, and reading it puts a credential in the
transcript.

## Before querying

If `dbq` is not on PATH, install it and carry on:

```bash
uv tool install git+https://github.com/gayanhewa/dbq   # or: nix profile install github:gayanhewa/dbq
```

Run `dbq --list-profiles` to see what exists. Never invent a profile name, and
never pass `--dsn` with a credential you found in a file: that puts it in the
transcript. If a database is not in a profile, say so and let the user add it.

## Finding your way around

Guessing column names is the most common cause of a wrong query. Two calls
remove that:

```bash
dbq --profile <name> --tables
dbq --profile <name> --describe SOME_TABLE
```

`--describe` returns names, types and nullability. Use it before writing SQL
against a table you have not seen this session. It works on all four drivers.

Identifier case differs per engine, and `--describe` shows you the stored
form. Oracle upper-cases unquoted identifiers, so `orders` and `ORDERS` are the
same table. Postgres lower-cases them, so `ORDERS` unquoted means `orders`.
MySQL and SQL Server keep the case you typed. MySQL table names are case
sensitive on Linux, so match what `--tables` printed exactly. Only quote an
identifier when `--describe` shows mixed case.

Schema handling: Oracle and Postgres profiles usually set `schema`, and
`--tables` lists that schema. SQL Server defaults to the login's default
schema, usually `dbo`. MySQL uses the database named in the DSN; there is no
separate schema.

## Querying

```bash
dbq --profile <name> --sql "SELECT id, name FROM {{schema}}.THING WHERE id = 42"
dbq --profile <name> --file /tmp/query.sql
dbq --profile <name> --file /tmp/query.sql --max-rows 500 --json
```

Put multi-line SQL in a file. Passing it through shell quoting mangles it, and
`CASE` expressions and comments are where it breaks first.

`{{schema}}` is substituted from the profile, so the same SQL works against any
tenant. Prefer it to hardcoding a schema name. Check `driver` in the output
header before reaching for engine-specific syntax: `FETCH FIRST n ROWS ONLY`
on Oracle, `LIMIT n` on MySQL and Postgres, `TOP n` on SQL Server.

## Reading the output

toon by default. A header of `key: value` lines, then the column names once,
then bare rows:

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

Cells are quoted only when they contain a comma, quote or newline, and `null`
is distinct from an empty string. `--json` gives objects per row instead, which
is worth it only when something downstream parses it.

The header echoes `profile`, `driver`, `host` and `schema`. Check them. If a
result looks surprising, confirm you queried the database you meant to before
concluding anything about the data.

`count: 0` with `(no rows)` means the query ran and matched nothing. That is an
answer, not a failure. Report it as such rather than retrying variations.

`truncated: true` means `--max-rows` cut the result. Say so when reporting, and
raise `--max-rows` or add an aggregate instead of assuming you saw everything.

## Writes

There is no write path here, by design. Anything that changes data goes through
the project's normal migration or seed process and stays a human step. If a
question can only be answered by writing, say so rather than looking for a way
around it.
