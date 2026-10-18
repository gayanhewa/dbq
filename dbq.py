"""dbq — run read-only SQL against Oracle, MySQL, Postgres, SQL Server,
SQLite, or libsql (including Turso) and print toon or json.

Read-only is enforced by the database where it can be: on Oracle, MySQL,
Postgres and SQLite every query runs inside a read-only transaction, so a write
fails server-side even if the account has permission. SQL Server has no
read-only transaction mode, so there the query runs in a transaction that is
always rolled back and the statement check is the only guard. For libsql
(Turso), read-only depends on the server token permissions. The statement check
otherwise exists to fail fast with a clearer message than the driver would give.

Credentials are never written anywhere. A profile either carries a dsn inline
or names an existing .env to read it from, and the value is held in memory for
one query.

An optional ssh_tunnel table sends the connection through a bastion. It is
only used when the table is present; --no-ssh-tunnel skips it for one run.

Config: ~/.config/dbq/config.toml (override with DBQ_CONFIG)

    default = "work"

    [profiles.work]
    driver   = "oracle"
    env_file = "~/code/app/.env"     # read the DSN from an existing .env
    env_var  = "ORACLE_CONNECTION"
    schema   = "APP_SCHEMA"          # optional, substituted for {{schema}}

    [profiles.local]
    driver = "mysql"
    dsn    = "mysql://user:pw@127.0.0.1:3306/app"

    [profiles.pg]
    driver = "postgres"
    dsn    = "postgres://user:pw@127.0.0.1:5432/app"    # or postgresql://

    [profiles.mssql]
    driver = "sqlserver"
    dsn    = "Server=db.example.com,1433;Database=app;User Id=user;Password=pw"
    # or mssql://user:pw@db.example.com/app, or sqlserver://...

    [profiles.localdb]
    driver = "sqlite"
    dsn    = "~/data/app.db"

    [profiles.turso]
    driver   = "libsql"
    dsn      = "libsql://my-db.turso.io"
    env_file = "~/.turso/env"
    env_var  = "TURSO_AUTH_TOKEN"      # reads the auth token, not the DSN

    [profiles.prod]
    driver = "postgres"
    dsn    = "postgres://reader:pw@db.internal:5432/app"

    [profiles.prod.ssh_tunnel]         # optional
    host     = "bastion.example.com"
    port     = 22                      # optional, default 22
    user     = "deploy"                # optional
    key_file = "~/.ssh/id_ed25519"     # optional
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

CONFIG_PATH = Path(os.environ.get("DBQ_CONFIG", "~/.config/dbq/config.toml")).expanduser()
DEFAULT_MAX_ROWS = 100
DEFAULT_TIMEOUT = 30
DEFAULT_PORTS = {"oracle": 1521, "mysql": 3306, "postgres": 5432, "sqlserver": 1433}
DRIVERS = tuple(DEFAULT_PORTS) + ("sqlite", "libsql")


class DbqError(Exception):
    """Anything the user should see as a clean message rather than a traceback."""


# --- config -----------------------------------------------------------------


def load_config():
    if not CONFIG_PATH.exists():
        raise DbqError(
            f"no config at {CONFIG_PATH}\n"
            "create it with a [profiles.<name>] section, or pass "
            "--dsn / --env-file explicitly"
        )
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def resolve_profile(cfg, args):
    """Merge a named profile with any CLI overrides. CLI always wins."""
    profiles = cfg.get("profiles", {})
    name = args.profile or cfg.get("default")

    if name and name not in profiles and not args.dsn and not args.env_file:
        known = ", ".join(sorted(profiles)) or "none defined"
        raise DbqError(f"unknown profile {name!r}. known: {known}")

    p = dict(profiles.get(name, {})) if name else {}
    for key, val in (
        ("driver", args.driver),
        ("dsn", args.dsn),
        ("env_file", args.env_file),
        ("env_var", args.env_var),
        ("schema", args.schema),
    ):
        if val:
            p[key] = val

    if not p.get("driver"):
        raise DbqError(f"no driver set. use --driver {'|'.join(DRIVERS)} or set it in the profile")
    if p["driver"] not in DRIVERS:
        raise DbqError(f"unsupported driver {p['driver']!r}, expected one of {', '.join(DRIVERS)}")
    p["name"] = name or "<ad-hoc>"
    return p


def read_dsn(profile):
    """Inline dsn, or the named variable out of an existing .env."""
    if profile.get("dsn"):
        return profile["dsn"]

    env_file = profile.get("env_file")
    if not env_file:
        raise DbqError(f"profile {profile['name']!r} has neither dsn nor env_file")

    path = Path(env_file).expanduser()
    if not path.exists():
        raise DbqError(f"env file not found: {path}")

    from dotenv import dotenv_values

    values = dotenv_values(path)
    var = profile.get("env_var")
    if not var:
        raise DbqError(f"profile {profile['name']!r} sets env_file but no env_var")
    if var not in values or not values[var]:
        raise DbqError(f"{var} not set in {path}")
    return values[var]


# --- dsn parsing ------------------------------------------------------------

# .NET-style connection strings use these aliases; normalise them all to one set.
_ALIASES = {
    "user id": "user", "userid": "user", "uid": "user", "user": "user",
    "username": "user",
    "password": "password", "pwd": "password",
    "data source": "target", "datasource": "target", "server": "host",
    "host": "host", "port": "port", "database": "database",
    "initial catalog": "database", "service name": "service",
    "integrated security": "trusted", "trusted_connection": "trusted",
}


def _parse_keyvalue(dsn):
    out = {}
    for part in dsn.split(";"):
        if "=" not in part:
            continue
        raw_key, _, val = part.partition("=")
        key = _ALIASES.get(raw_key.strip().lower())
        if key:
            out[key] = val.strip()
    return out


def parse_dsn(dsn, driver):
    """Accept url, .NET key=value, or Oracle EZ-connect and return kwargs."""
    dsn = dsn.strip()

    if driver == "sqlite":
        # sqlite:///path/to/db, sqlite://host/path, or plain file path
        if dsn.startswith("sqlite://"):
            u = urlparse(dsn)
            path = unquote(u.path)
            if u.hostname and u.hostname not in ("", "localhost"):
                # sqlite://host/path — treat host as part of the path
                path = "/" + u.hostname + (path if path.startswith("/") else "/" + path)
            return {"database": path.lstrip("/"), "host": path.lstrip("/")}
        path = str(Path(dsn).expanduser())
        return {"database": path, "host": path}

    if driver == "libsql":
        url = dsn
        auth_token = None
        # Extract authToken from query params
        if "?" in url:
            base, qs = url.split("?", 1)
            parts = []
            for part in qs.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k == "authToken":
                        auth_token = unquote(v)
                        continue
                parts.append(part)
            url = base + ("?" + "&".join(parts) if parts else "")
        params = {"url": url, "host": url}
        if auth_token:
            params["auth_token"] = auth_token
        return params

    if "://" in dsn:
        u = urlparse(dsn)
        params = {
            "user": unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "host": u.hostname or "",
            "port": u.port,
            "database": (u.path or "").lstrip("/"),
        }
    elif "=" in dsn and ";" in dsn or re.match(r"^\s*\w[\w ]*=", dsn):
        params = _parse_keyvalue(dsn)
        trusted = params.pop("trusted", "").lower() in ("true", "yes", "sspi")
        if trusted and not params.get("user"):
            raise DbqError("integrated/trusted auth is not supported, only SQL logins")
        target = params.pop("target", None)
        if driver == "sqlserver":
            # SQL Server writes host,port with a comma, often behind a tcp: prefix.
            target = target or params.pop("host", None)
            if target:
                host, _, port = target.removeprefix("tcp:").partition(",")
                params["host"] = host.strip()
                params["port"] = port.strip() or params.get("port")
        elif target:
            # Oracle's Data Source is host:port/service in one field.
            m = re.match(r"^(?P<host>[^:/]+)(?::(?P<port>\d+))?(?:/(?P<svc>.+))?$", target)
            if m:
                params["host"] = m.group("host")
                params["port"] = m.group("port")
                params["database"] = params.get("database") or m.group("svc")
            else:
                params["database"] = target
    else:
        # Oracle EZ-connect: user/password@host:port/service
        m = re.match(
            r"^(?P<user>[^/]+)/(?P<password>.+)@(?P<host>[^:/]+)"
            r"(?::(?P<port>\d+))?(?:/(?P<database>.+))?$",
            dsn,
        )
        if not m:
            raise DbqError("could not parse the connection string")
        params = m.groupdict()

    if params.get("service") and not params.get("database"):
        params["database"] = params.pop("service")
    params.pop("service", None)

    if not params.get("host"):
        raise DbqError("connection string has no host")
    params["port"] = int(params["port"]) if params.get("port") else DEFAULT_PORTS[driver]
    return params


# --- ssh tunnel -------------------------------------------------------------

SSH_TUNNEL_KEYS = ("host", "port", "user", "key_file")


def resolve_tunnel(profile):
    """Return a validated tunnel config, or None when the profile has none."""
    raw = profile.get("ssh_tunnel")
    if not raw:
        return None

    name = profile["name"]
    if not isinstance(raw, dict):
        raise DbqError(f"profile {name!r}: ssh_tunnel must be a table")

    unknown = sorted(set(raw) - set(SSH_TUNNEL_KEYS))
    if unknown:
        raise DbqError(
            f"profile {name!r}: unknown ssh_tunnel key(s) {', '.join(unknown)}, "
            f"expected one of {', '.join(SSH_TUNNEL_KEYS)}"
        )

    if profile["driver"] in ("sqlite", "libsql"):
        raise DbqError(
            f"profile {name!r}: ssh_tunnel is not supported for the "
            f"{profile['driver']} driver"
        )

    host = raw.get("host")
    if not isinstance(host, str) or not host.strip():
        raise DbqError(f"profile {name!r}: ssh_tunnel needs a host")
    host = host.strip()
    if "@" in host:
        raise DbqError(
            f"profile {name!r}: put the ssh user in ssh_tunnel.user, not in host"
        )

    port = raw.get("port", 22)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise DbqError(f"profile {name!r}: ssh_tunnel.port must be a number")
    if not 1 <= port <= 65535:
        raise DbqError(f"profile {name!r}: ssh_tunnel.port must be between 1 and 65535")

    tunnel = {"host": host, "port": port}

    user = raw.get("user")
    if user is not None:
        if not isinstance(user, str) or not user.strip():
            raise DbqError(
                f"profile {name!r}: ssh_tunnel.user must be a non-empty string"
            )
        tunnel["user"] = user.strip()

    key_file = raw.get("key_file")
    if key_file is not None:
        if not isinstance(key_file, str) or not key_file.strip():
            raise DbqError(f"profile {name!r}: ssh_tunnel.key_file must be a path")
        tunnel["key_file"] = str(Path(key_file).expanduser())

    return tunnel


def _tunnel_destination(tunnel):
    if tunnel.get("user"):
        return f"{tunnel['user']}@{tunnel['host']}"
    return tunnel["host"]


def tunnel_command(ssh, params, tunnel, local_port, timeout):
    """Build the ssh command that forwards a local port to the database."""
    remote_host = params["host"]
    if ":" in remote_host and not remote_host.startswith("["):
        remote_host = f"[{remote_host}]"
    cmd = [
        ssh,
        "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", f"ConnectTimeout={max(1, int(timeout))}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{local_port}:{remote_host}:{params['port']}",
        "-p", str(tunnel["port"]),
    ]
    if tunnel.get("key_file"):
        cmd += ["-i", tunnel["key_file"]]
    cmd += ["--", _tunnel_destination(tunnel)]
    return cmd


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc.stderr:
        proc.stderr.close()


class Tunnel:
    def __init__(self, proc, local_port, destination):
        self.proc = proc
        self.local_port = local_port
        self.destination = destination

    def close(self):
        _stop_process(self.proc)


def start_tunnel(params, tunnel, timeout):
    """Open ssh -L and wait until the local end accepts connections."""
    ssh = shutil.which("ssh")
    if not ssh:
        raise DbqError("ssh_tunnel is configured but no ssh binary was found on PATH")

    destination = _tunnel_destination(tunnel)
    local_port = _free_port()
    cmd = tunnel_command(ssh, params, tunnel, local_port, timeout)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as e:
        raise DbqError(f"could not start ssh for the tunnel: {e}")

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                err = (proc.stderr.read() or "").strip() if proc.stderr else ""
                detail = f": {err}" if err else ""
                raise DbqError(
                    f"ssh tunnel to {destination} exited with status {code}{detail}"
                )
            if _port_open(local_port):
                return Tunnel(proc, local_port, destination)
            time.sleep(0.05)
        raise DbqError(f"ssh tunnel to {destination} did not open within {timeout}s")
    except BaseException:
        _stop_process(proc)
        raise


# --- connect + read-only ----------------------------------------------------


def connect(driver, params, timeout):
    if driver == "oracle":
        import oracledb  # thin mode: no Instant Client needed

        conn = oracledb.connect(
            user=params.get("user"),
            password=params.get("password"),
            dsn=f"{params['host']}:{params['port']}/{params.get('database') or ''}",
            tcp_connect_timeout=timeout,
        )
        # Server-side guard. Any DML in this transaction raises ORA-01456,
        # whatever the account is allowed to do.
        conn.cursor().execute("SET TRANSACTION READ ONLY")
        return conn

    if driver == "postgres":
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(
            user=params.get("user") or "",
            password=params.get("password") or None,
            host=params["host"],
            port=params["port"],
            database=params.get("database") or None,
            timeout=timeout,
        )
        # The session default must be set outside any transaction: a SET inside
        # one is undone by the rollback main() always issues.
        conn.autocommit = True
        conn.cursor().execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.autocommit = False
        # pg8000 sends BEGIN before the first statement, so this lands inside
        # the transaction the query runs in. Any write then raises SQLSTATE 25006.
        conn.cursor().execute("SET TRANSACTION READ ONLY")
        return conn

    if driver == "sqlserver":
        import pytds

        # SQL Server has no read-only transaction mode. The query runs in a
        # transaction main() always rolls back; check_select_only is the guard.
        return pytds.connect(
            dsn=params["host"],
            port=params["port"],
            database=params.get("database") or None,
            user=params.get("user") or "",
            password=params.get("password") or "",
            login_timeout=timeout,
            autocommit=False,
        )

    if driver == "sqlite":
        import sqlite3

        # URI mode with mode=ro: fails if the file does not exist, and the
        # server rejects any write attempt with "attempt to write a readonly
        # database". No PRAGMA needed — the file itself is read-only.
        return sqlite3.connect(
            f"file:{params['database']}?mode=ro", uri=True
        )

    if driver == "libsql":
        try:
            import libsql_client
        except ImportError:
            raise DbqError(
                "libsql driver requires libsql-client. "
                "Install it with: pip install 'dbq[libsql]'"
            )

        return libsql_client.create_client_sync(
            url=params["url"],
            auth_token=params.get("auth_token"),
        )

    import pymysql

    conn = pymysql.connect(
        host=params["host"],
        port=params["port"],
        user=params.get("user") or "",
        password=params.get("password") or "",
        database=params.get("database") or None,
        connect_timeout=timeout,
        read_default_file=None,
    )
    # Same idea: the server rejects writes for the life of this transaction.
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute("START TRANSACTION READ ONLY")
    return conn


# --- sql guard --------------------------------------------------------------

_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)


def check_select_only(sql):
    """Fail fast on obvious writes. The read-only transaction is the real guard."""
    stripped = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if not stripped:
        raise DbqError("empty query")
    if ";" in stripped:
        raise DbqError("multiple statements are not allowed, send one SELECT")
    if not re.match(r"^(select|with)\b", stripped, re.I):
        first = stripped.split(None, 1)[0].upper()
        raise DbqError(f"only SELECT and WITH are allowed, got {first}")
    return stripped


# --- output -----------------------------------------------------------------


def to_toon(columns, rows, meta):
    """Token-oriented: a header naming the fields once, then bare rows."""
    out = [f"{k}: {v}" for k, v in meta.items()]
    out.append(f"rows[{len(rows)}]{{{','.join(columns)}}}:")
    for row in rows:
        out.append("  " + ",".join(_toon_cell(v) for v in row))
    if not rows:
        out.append("  (no rows)")
    return "\n".join(out)


def _toon_cell(v):
    if v is None:
        return "null"
    s = str(v)
    return f'"{s}"' if ("," in s or '"' in s or "\n" in s) else s


def to_json(columns, rows, meta):
    return json.dumps(
        {**meta, "columns": columns, "rows": [dict(zip(columns, r)) for r in rows]},
        indent=2,
        default=str,
    )


# --- queries ----------------------------------------------------------------


def run(conn, driver, sql, max_rows):
    if driver == "libsql":
        result = conn.execute(sql)
        columns = list(result.columns) if result.columns else []
        all_rows = [list(r) for r in result.rows]
        truncated = len(all_rows) > max_rows
        return columns, all_rows[:max_rows], truncated

    cur = conn.cursor()
    cur.execute(sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    return columns, [list(r) for r in rows[:max_rows]], truncated


def tables_sql(driver, schema):
    if driver in ("sqlite", "libsql"):
        return "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    if driver == "oracle":
        owner = f"'{schema}'" if schema else "SYS_CONTEXT('USERENV','CURRENT_SCHEMA')"
        return f"SELECT table_name FROM all_tables WHERE owner = {owner} ORDER BY table_name"
    if driver == "mysql":
        return "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name"
    return (
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = {_std_schema(driver, schema)} ORDER BY table_name"
    )


def _std_schema(driver, schema):
    if schema:
        return f"'{schema}'"
    return "current_schema()" if driver == "postgres" else "SCHEMA_NAME()"


def describe_sql(driver, table, schema):
    if driver in ("sqlite", "libsql"):
        return f"SELECT name, type, \"notnull\" AS nullable FROM pragma_table_info('{table}')"
    if driver == "oracle":
        owner = f"'{schema}'" if schema else "SYS_CONTEXT('USERENV','CURRENT_SCHEMA')"
        return (
            "SELECT column_name, data_type, nullable FROM all_tab_columns "
            f"WHERE owner = {owner} AND table_name = '{table.upper()}' ORDER BY column_id"
        )
    if driver == "mysql":
        return (
            "SELECT column_name, column_type, is_nullable FROM information_schema.columns "
            f"WHERE table_schema = DATABASE() AND table_name = '{table}' ORDER BY ordinal_position"
        )
    # lower() on both sides: postgres folds unquoted names, SQL Server is usually case-insensitive.
    return (
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        f"WHERE table_schema = {_std_schema(driver, schema)} "
        f"AND lower(table_name) = lower('{table}') ORDER BY ordinal_position"
    )


# --- cli --------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="dbq",
        description="read-only SQL against Oracle, MySQL, Postgres, SQL Server, SQLite or libsql",
    )
    p.add_argument("--profile", help="profile name from config.toml")
    p.add_argument("--sql", help="SQL to run")
    p.add_argument("--file", help="file containing the SQL")
    p.add_argument("--driver", choices=list(DRIVERS))
    p.add_argument("--dsn", help="connection string, overrides the profile")
    p.add_argument("--env-file", help=".env to read the connection string from")
    p.add_argument("--env-var", help="variable name inside that .env")
    p.add_argument("--schema", help="value substituted for {{schema}}")
    p.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument(
        "--no-ssh-tunnel",
        action="store_true",
        help="ignore any ssh_tunnel in the profile",
    )
    p.add_argument("--json", action="store_true", help="json instead of toon")
    p.add_argument("--tables", action="store_true", help="list tables")
    p.add_argument("--describe", metavar="TABLE", help="show a table's columns")
    p.add_argument("--list-profiles", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config() if CONFIG_PATH.exists() else {"profiles": {}}

        if args.list_profiles:
            profiles = cfg.get("profiles", {})
            meta = {"count": len(profiles), "default": cfg.get("default", "none")}
            rows = [[n, p.get("driver", "?"), p.get("schema", "")] for n, p in sorted(profiles.items())]
            print(to_toon(["profile", "driver", "schema"], rows, meta))
            return 0

        profile = resolve_profile(cfg, args)
        driver, schema = profile["driver"], profile.get("schema")

        if args.tables:
            sql = tables_sql(driver, schema)
        elif args.describe:
            sql = describe_sql(driver, args.describe, schema)
        elif args.file:
            sql = Path(args.file).expanduser().read_text()
        elif args.sql:
            sql = args.sql
        else:
            raise DbqError("nothing to run: pass --sql, --file, --tables or --describe")

        if "{{schema}}" in sql:
            if not schema:
                raise DbqError("query uses {{schema}} but the profile sets no schema")
            sql = sql.replace("{{schema}}", schema)

        sql = check_select_only(sql)

        params = parse_dsn(read_dsn(profile), driver)
        tunnel = None
        try:
            tunnel_cfg = None if args.no_ssh_tunnel else resolve_tunnel(profile)
            connect_params = params
            if tunnel_cfg:
                tunnel = start_tunnel(params, tunnel_cfg, args.timeout)
                connect_params = dict(
                    params, host="127.0.0.1", port=tunnel.local_port
                )

            conn = connect(driver, connect_params, args.timeout)
            try:
                columns, rows, truncated = run(conn, driver, sql, args.max_rows)
            finally:
                # libsql ClientSync has no rollback; sqlite3 and DB-API 2.0 do.
                if hasattr(conn, "rollback"):
                    conn.rollback()
                conn.close()
        finally:
            if tunnel is not None:
                tunnel.close()

        meta = {
            "profile": profile["name"],
            "driver": driver,
            "host": params["host"],
        }
        if tunnel is not None:
            meta["tunnel"] = tunnel.destination
        meta["count"] = len(rows)
        if schema:
            meta["schema"] = schema
        if truncated:
            meta["truncated"] = f"true, showing first {args.max_rows}"

        print(to_json(columns, rows, meta) if args.json else to_toon(columns, rows, meta))
        return 0

    except DbqError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - drivers raise all sorts
        # Never let a driver echo the connection string into the output.
        msg = re.sub(r"(?i)(password|pwd)\s*=\s*[^;\s]+", r"\1=***", str(e))
        print(f"error: {type(e).__name__}: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
