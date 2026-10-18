import io
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import dbq
from dbq import DbqError, check_select_only, parse_dsn, to_json, to_toon

# --- parse_dsn ---------------------------------------------------------------


@pytest.mark.parametrize(
    "dsn, driver, expected",
    [
        # URLs, default port per driver
        (
            "mysql://root:pw@db.example.com/appdb",
            "mysql",
            dict(user="root", password="pw", host="db.example.com", port=3306, database="appdb"),
        ),
        (
            "mysql://root:pw@127.0.0.1:3307/appdb",
            "mysql",
            dict(user="root", password="pw", host="127.0.0.1", port=3307, database="appdb"),
        ),
        (
            "postgres://app:pw@pg.local/app",
            "postgres",
            dict(user="app", password="pw", host="pg.local", port=5432, database="app"),
        ),
        (
            "postgresql://app:pw@pg.local:6543/app",
            "postgres",
            dict(user="app", password="pw", host="pg.local", port=6543, database="app"),
        ),
        (
            "mssql://sa:pw@sql.local/master",
            "sqlserver",
            dict(user="sa", password="pw", host="sql.local", port=1433, database="master"),
        ),
        (
            "sqlserver://sa:pw@sql.local:14330/master",
            "sqlserver",
            dict(user="sa", password="pw", host="sql.local", port=14330, database="master"),
        ),
        # percent-encoded password in a URL
        (
            "mysql://root:p%40ss%3Aw%2Frd@127.0.0.1/app",
            "mysql",
            dict(user="root", password="p@ss:w/rd", host="127.0.0.1", port=3306, database="app"),
        ),
        (
            "postgres://app:p%40ss@127.0.0.1/app",
            "postgres",
            dict(user="app", password="p@ss", host="127.0.0.1", port=5432, database="app"),
        ),
        # .NET key=value, oracle: Data Source carries host:port/service
        (
            "User Id=SCOTT;Password=tiger;Data Source=host:1521/ORCLPDB1",
            "oracle",
            dict(user="SCOTT", password="tiger", host="host", port=1521, database="ORCLPDB1"),
        ),
        (
            "User Id=SCOTT;Password=tiger;Data Source=host/ORCLPDB1",
            "oracle",
            dict(user="SCOTT", password="tiger", host="host", port=1521, database="ORCLPDB1"),
        ),
        (
            "Uid=SCOTT;Pwd=tiger;Server=host;Service Name=ORCL",
            "oracle",
            dict(user="SCOTT", password="tiger", host="host", port=1521, database="ORCL"),
        ),
        # .NET key=value, sqlserver: Server=host,port with a comma
        (
            "Server=host,1433;Database=d;User Id=u;Password=p",
            "sqlserver",
            dict(user="u", password="p", host="host", port=1433, database="d"),
        ),
        (
            "Server=host,14330;Database=d;User Id=u;Password=p",
            "sqlserver",
            dict(user="u", password="p", host="host", port=14330, database="d"),
        ),
        (
            "Server=host;Initial Catalog=d;Uid=u;Pwd=p",
            "sqlserver",
            dict(user="u", password="p", host="host", port=1433, database="d"),
        ),
        (
            "Server=tcp:host,1433;Database=d;User Id=u;Password=p",
            "sqlserver",
            dict(user="u", password="p", host="host", port=1433, database="d"),
        ),
        (
            "Data Source=tcp:host;Initial Catalog=d;User Id=u;Password=p",
            "sqlserver",
            dict(user="u", password="p", host="host", port=1433, database="d"),
        ),
        # .NET key=value for mysql and postgres
        (
            "Server=h;Port=3307;Database=d;Uid=u;Pwd=p",
            "mysql",
            dict(user="u", password="p", host="h", port=3307, database="d"),
        ),
        (
            "Server=h;Database=d;Uid=u;Pwd=p",
            "mysql",
            dict(user="u", password="p", host="h", port=3306, database="d"),
        ),
        (
            "Host=h;Port=5433;Database=d;Username=u;Password=p",
            "postgres",
            dict(user="u", password="p", host="h", port=5433, database="d"),
        ),
        (
            "Host=h;Database=d;Username=u;Password=p",
            "postgres",
            dict(user="u", password="p", host="h", port=5432, database="d"),
        ),
        # Oracle EZ-connect
        (
            "scott/tiger@host:1521/ORCLPDB1",
            "oracle",
            dict(user="scott", password="tiger", host="host", port=1521, database="ORCLPDB1"),
        ),
        (
            "scott/tiger@host/ORCLPDB1",
            "oracle",
            dict(user="scott", password="tiger", host="host", port=1521, database="ORCLPDB1"),
        ),
        (
            "scott/tiger@host:1522/ORCLPDB1",
            "oracle",
            dict(user="scott", password="tiger", host="host", port=1522, database="ORCLPDB1"),
        ),
        # SQLite: plain file path
        (
            "/data/app.db",
            "sqlite",
            dict(database="/data/app.db", host="/data/app.db"),
        ),
        (
            "sqlite:///data/app.db",
            "sqlite",
            dict(database="data/app.db", host="data/app.db"),
        ),
        (
            "./local.db",
            "sqlite",
            dict(database="local.db", host="local.db"),
        ),
        # libsql: remote Turso URL with auth token in query param
        (
            "libsql://my-db.turso.io?authToken=sekret",
            "libsql",
            dict(url="libsql://my-db.turso.io", host="libsql://my-db.turso.io", auth_token="sekret"),
        ),
        # libsql: remote Turso URL without auth token
        (
            "libsql://my-db.turso.io",
            "libsql",
            dict(url="libsql://my-db.turso.io", host="libsql://my-db.turso.io"),
        ),
        # libsql: local file
        (
            "file:///data/db.sqlite",
            "libsql",
            dict(url="file:///data/db.sqlite", host="file:///data/db.sqlite"),
        ),
    ],
)
def test_parse_dsn(dsn, driver, expected):
    got = parse_dsn(dsn, driver)
    assert {k: got[k] for k in expected} == expected
    if "port" in expected:
        assert isinstance(got["port"], int)


def test_parse_dsn_strips_surrounding_whitespace():
    got = parse_dsn("  mysql://u:p@h/d\n", "mysql")
    assert (got["host"], got["database"]) == ("h", "d")


@pytest.mark.parametrize("driver", ["oracle", "mysql", "postgres", "sqlserver"])
def test_parse_dsn_rejects_missing_host(driver):
    with pytest.raises(DbqError, match="no host"):
        parse_dsn("User Id=u;Password=p;Database=d", driver)


def test_parse_dsn_rejects_garbage():
    with pytest.raises(DbqError):
        parse_dsn("this is not a connection string", "oracle")


# --- check_select_only -------------------------------------------------------


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("  select 1 ;  ", "select 1"),
        ("-- leading comment\nSELECT 1", "SELECT 1"),
        ("/* block */ SELECT 1", "SELECT 1"),
        ("SELECT 1 -- trailing", "SELECT 1"),
        ("WITH t AS (SELECT 1 AS x) SELECT x FROM t", "WITH t AS (SELECT 1 AS x) SELECT x FROM t"),
        ("with t as (select 1) select * from t", "with t as (select 1) select * from t"),
    ],
)
def test_check_select_only_accepts(sql, expected):
    assert check_select_only(sql) == expected


def test_check_select_only_rejects_delete_with_clear_message():
    with pytest.raises(DbqError, match=r"only SELECT and WITH are allowed, got DELETE"):
        check_select_only("DELETE FROM widget WHERE id = 1")


@pytest.mark.parametrize("sql", ["UPDATE t SET a = 1", "insert into t values (1)", "DROP TABLE t", "TRUNCATE t"])
def test_check_select_only_rejects_other_writes(sql):
    with pytest.raises(DbqError, match="only SELECT and WITH are allowed"):
        check_select_only(sql)


def test_check_select_only_rejects_write_hidden_behind_comment():
    with pytest.raises(DbqError, match="got DELETE"):
        check_select_only("/* SELECT */ DELETE FROM t")


def test_check_select_only_rejects_multiple_statements():
    with pytest.raises(DbqError, match="multiple statements"):
        check_select_only("SELECT 1; SELECT 2")


def test_check_select_only_rejects_select_then_write():
    with pytest.raises(DbqError, match="multiple statements"):
        check_select_only("SELECT 1; DELETE FROM t")


@pytest.mark.parametrize("sql", ["", "   ", ";", "-- only a comment", "/* nothing */"])
def test_check_select_only_rejects_empty(sql):
    with pytest.raises(DbqError, match="empty query"):
        check_select_only(sql)


# --- output ------------------------------------------------------------------

COLUMNS = ["ID", "NAME", "NOTE"]
ROWS = [[1, "alpha", None], [2, "beta", "has,comma"], [3, "gamma", "x"]]
META = {"profile": "work", "driver": "oracle", "host": "db.example.com", "count": 3}


def test_to_toon_matches_readme_shape():
    assert to_toon(COLUMNS, ROWS, META) == (
        "profile: work\n"
        "driver: oracle\n"
        "host: db.example.com\n"
        "count: 3\n"
        "rows[3]{ID,NAME,NOTE}:\n"
        "  1,alpha,null\n"
        '  2,beta,"has,comma"\n'
        "  3,gamma,x"
    )


@pytest.mark.parametrize(
    "value, cell",
    [
        (None, "null"),
        ("", ""),
        ("plain", "plain"),
        (42, "42"),
        ("has,comma", '"has,comma"'),
        ('has"quote', '"has"quote"'),
        ("has\nnewline", '"has\nnewline"'),
        ("null", "null"),
    ],
)
def test_to_toon_cell_quoting(value, cell):
    out = to_toon(["c"], [[value]], {})
    assert out == f"rows[1]{{c}}:\n  {cell}"


def test_to_toon_no_rows():
    assert to_toon(["a", "b"], [], {"count": 0}) == "count: 0\nrows[0]{a,b}:\n  (no rows)"


def test_to_toon_no_columns_no_rows():
    assert to_toon([], [], {}) == "rows[0]{}:\n  (no rows)"


def test_to_json_shape():
    out = json.loads(to_json(COLUMNS, ROWS, META))
    assert out == {
        **META,
        "columns": COLUMNS,
        "rows": [
            {"ID": 1, "NAME": "alpha", "NOTE": None},
            {"ID": 2, "NAME": "beta", "NOTE": "has,comma"},
            {"ID": 3, "NAME": "gamma", "NOTE": "x"},
        ],
    }


def test_to_json_stringifies_driver_types():
    out = json.loads(to_json(["d", "n"], [[date(2024, 1, 2), Decimal("1.50")]], {}))
    assert out["rows"] == [{"d": "2024-01-02", "n": "1.50"}]


def test_to_json_empty():
    out = json.loads(to_json(["a"], [], {"count": 0}))
    assert out == {"count": 0, "columns": ["a"], "rows": []}


# --- main() error paths ------------------------------------------------------

CONFIG = """
default = "local"

[profiles.local]
driver = "mysql"
dsn    = "mysql://root:pw@127.0.0.1:3306/app"

[profiles.noschema]
driver = "oracle"
dsn    = "scott/tiger@host:1521/ORCL"

[profiles.weird]
driver = "sqlite"
dsn    = "sqlite:///x.db"
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    monkeypatch.setattr(dbq, "CONFIG_PATH", path)
    return path


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(dbq, "CONFIG_PATH", tmp_path / "missing.toml")


def _run(capsys, argv):
    code = dbq.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_main_unknown_profile(config, capsys):
    code, out, err = _run(capsys, ["--profile", "nope", "--sql", "SELECT 1"])
    assert code == 2
    assert out == ""
    assert err == "error: unknown profile 'nope'. known: local, noschema, weird\n"


def test_main_no_driver(no_config, capsys):
    code, out, err = _run(capsys, ["--dsn", "mysql://u:p@h/d", "--sql", "SELECT 1"])
    assert code == 2
    assert err.startswith("error: no driver set")


def test_main_sqlite_direct_dsn(no_config, capsys, tmp_path):
    """sqlite driver works with a direct --dsn pointing at a real file."""
    import sqlite3

    db_path = tmp_path / "test.db"
    sqlite3.connect(str(db_path)).close()
    code, out, err = _run(
        capsys,
        ["--driver", "sqlite", "--dsn", str(db_path), "--sql", "SELECT 1 AS greeting"],
    )
    assert code == 0, f"stderr: {err}"
    assert "greeting" in out
    assert "1" in out


def test_main_libsql_direct_dsn(no_config, capsys, tmp_path):
    """libsql driver works with a direct --dsn pointing at a local file."""
    pytest.importorskip("libsql_client")
    db_path = tmp_path / "test_libsql.db"
    code, out, err = _run(
        capsys,
        [
            "--driver", "libsql",
            "--dsn", f"file://{db_path}",
            "--sql", "SELECT 1 AS greeting",
        ],
    )
    assert code == 0, f"stderr: {err}"
    assert "greeting" in out
    assert "1" in out


@pytest.mark.parametrize("driver", ["oracle", "mysql", "postgres", "sqlserver", "sqlite", "libsql"])
def test_main_accepts_every_driver_name(no_config, capsys, driver):
    # Fails on the schema check, which runs before any connection attempt, so
    # this only proves argparse and resolve_profile accept the driver name.
    code, _, err = _run(
        capsys,
        ["--driver", driver, "--dsn", "u/p@h:1/d", "--sql", "SELECT 1 FROM {{schema}}.t"],
    )
    assert code == 2
    assert err == "error: query uses {{schema}} but the profile sets no schema\n"





def test_main_schema_placeholder_without_schema(config, capsys):
    code, _, err = _run(capsys, ["--profile", "noschema", "--sql", "SELECT * FROM {{schema}}.T"])
    assert code == 2
    assert err == "error: query uses {{schema}} but the profile sets no schema\n"


def test_main_nothing_to_run(config, capsys):
    code, _, err = _run(capsys, [])
    assert code == 2
    assert err.startswith("error: nothing to run")


def test_main_rejects_write_before_connecting(config, capsys):
    code, _, err = _run(capsys, ["--sql", "DELETE FROM t"])
    assert code == 2
    assert err == "error: only SELECT and WITH are allowed, got DELETE\n"


def test_main_missing_env_file(no_config, capsys, tmp_path):
    missing = tmp_path / "nope.env"
    code, _, err = _run(
        capsys,
        ["--driver", "mysql", "--env-file", str(missing), "--env-var", "DSN", "--sql", "SELECT 1"],
    )
    assert code == 2
    assert err == f"error: env file not found: {missing}\n"


def test_main_env_var_missing_from_env_file(no_config, capsys, tmp_path):
    env = tmp_path / "app.env"
    env.write_text("OTHER=1\n")
    code, _, err = _run(
        capsys,
        ["--driver", "mysql", "--env-file", str(env), "--env-var", "DSN", "--sql", "SELECT 1"],
    )
    assert code == 2
    assert err == f"error: DSN not set in {env}\n"


def test_main_list_profiles(config, capsys):
    code, out, err = _run(capsys, ["--list-profiles"])
    assert code == 0
    assert err == ""
    assert out.splitlines() == [
        "count: 3",
        "default: local",
        "rows[3]{profile,driver,schema}:",
        "  local,mysql,",
        "  noschema,oracle,",
        "  weird,sqlite,",
    ]


def test_main_driver_error_exits_1(no_config, capsys):
    # Nothing listens on port 9 (discard); the refusal is immediate.
    code, out, err = _run(
        capsys,
        ["--driver", "mysql", "--dsn", "mysql://u:p@127.0.0.1:9/d", "--timeout", "2", "--sql", "SELECT 1"],
    )
    assert code == 1
    assert out == ""
    assert err.startswith("error: ")
    assert "Traceback" not in err


def test_main_sql_from_file(config, capsys, tmp_path):
    sql = tmp_path / "q.sql"
    sql.write_text("UPDATE t SET a = 1")
    code, _, err = _run(capsys, ["--file", str(sql)])
    assert code == 2
    assert err == "error: only SELECT and WITH are allowed, got UPDATE\n"


def test_config_path_comes_from_env():
    # conftest sets DBQ_CONFIG before dbq is imported.
    assert dbq.CONFIG_PATH == Path("/nonexistent/dbq-tests/config.toml")


# --- ssh tunnel --------------------------------------------------------------

TUNNEL_CONFIG = """
[profiles.pg]
driver = "postgres"
dsn    = "postgres://reader:pw@db.internal:5432/app"

[profiles.pg.ssh_tunnel]
host     = "bastion.example.com"
port     = 2222
user     = "deploy"
key_file = "~/.ssh/id_ed25519"
"""


@pytest.fixture
def tunnel_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(TUNNEL_CONFIG)
    monkeypatch.setattr(dbq, "CONFIG_PATH", path)
    return path


@pytest.fixture
def sqlite_tunnel_config(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    sqlite3.connect(str(db_path)).close()
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[profiles.local]
driver = "sqlite"
dsn    = "{db_path}"

[profiles.local.ssh_tunnel]
host = "bastion.example.com"
"""
    )
    monkeypatch.setattr(dbq, "CONFIG_PATH", path)
    return path


def test_resolve_tunnel_absent_returns_none():
    assert dbq.resolve_tunnel({"name": "p", "driver": "postgres"}) is None
    assert dbq.resolve_tunnel({"name": "p", "driver": "postgres", "ssh_tunnel": {}}) is None


def test_resolve_tunnel_reads_profile():
    profile = {
        "name": "pg",
        "driver": "postgres",
        "ssh_tunnel": {"host": "bastion", "user": "deploy", "key_file": "~/k"},
    }
    got = dbq.resolve_tunnel(profile)
    assert got == {
        "host": "bastion",
        "port": 22,
        "user": "deploy",
        "key_file": str(Path("~/k").expanduser()),
    }


@pytest.mark.parametrize(
    "tunnel, match",
    [
        ({"host": "bastion", "nope": 1}, "unknown ssh_tunnel"),
        ({"user": "deploy"}, "needs a host"),
        ({"host": "deploy@bastion"}, "put the ssh user"),
        ({"host": "bastion", "port": "nope"}, "port must be a number"),
        ({"host": "bastion", "port": 70000}, "between 1 and 65535"),
        ({"host": "bastion", "user": ""}, "user must be a non-empty string"),
        ({"host": "bastion", "key_file": ""}, "key_file must be a path"),
    ],
)
def test_resolve_tunnel_rejects_bad_config(tunnel, match):
    profile = {"name": "p", "driver": "postgres", "ssh_tunnel": tunnel}
    with pytest.raises(DbqError, match=match):
        dbq.resolve_tunnel(profile)


def test_resolve_tunnel_rejects_non_table():
    profile = {"name": "p", "driver": "postgres", "ssh_tunnel": "bastion"}
    with pytest.raises(DbqError, match="must be a table"):
        dbq.resolve_tunnel(profile)


@pytest.mark.parametrize("driver", ["sqlite", "libsql"])
def test_resolve_tunnel_rejects_local_drivers(driver):
    profile = {"name": "p", "driver": driver, "ssh_tunnel": {"host": "bastion"}}
    with pytest.raises(DbqError, match="not supported"):
        dbq.resolve_tunnel(profile)


def test_tunnel_command_forwards_to_dsn_host():
    cmd = dbq.tunnel_command(
        "/usr/bin/ssh",
        {"host": "db.internal", "port": 5432},
        {"host": "bastion", "port": 2222, "user": "deploy", "key_file": "/k"},
        4567,
        30,
    )
    assert cmd[0] == "/usr/bin/ssh"
    assert "-N" in cmd
    assert "BatchMode=yes" in cmd
    assert "ExitOnForwardFailure=yes" in cmd
    assert "ConnectTimeout=30" in cmd
    assert "127.0.0.1:4567:db.internal:5432" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert cmd[cmd.index("-i") + 1] == "/k"
    assert cmd[-2] == "--"
    assert cmd[-1] == "deploy@bastion"


def test_tunnel_command_omits_optional_arguments():
    cmd = dbq.tunnel_command(
        "ssh", {"host": "db.internal", "port": 5432}, {"host": "bastion", "port": 22}, 4567, 5
    )
    assert "-i" not in cmd
    assert cmd[-1] == "bastion"


def test_tunnel_command_brackets_ipv6_database_hosts():
    cmd = dbq.tunnel_command(
        "ssh", {"host": "::1", "port": 5432}, {"host": "bastion", "port": 22}, 4567, 5
    )
    assert "127.0.0.1:4567:[::1]:5432" in cmd


class _FakeProc:
    def __init__(self, returncode=None, stderr=""):
        self.returncode = returncode
        self.pid = 12345
        self.stderr = io.StringIO(stderr)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_start_tunnel_requires_ssh(monkeypatch):
    monkeypatch.setattr(dbq.shutil, "which", lambda _: None)
    with pytest.raises(DbqError, match="no ssh binary"):
        dbq.start_tunnel({"host": "db.internal", "port": 5432}, {"host": "bastion", "port": 22}, 5)


def test_start_tunnel_reports_ssh_failure(monkeypatch):
    proc = _FakeProc(returncode=255, stderr="ssh: Could not resolve hostname bastion")
    monkeypatch.setattr(dbq.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(dbq, "_free_port", lambda: 4567)
    monkeypatch.setattr(dbq.subprocess, "Popen", lambda *a, **k: proc)

    with pytest.raises(DbqError, match="Could not resolve hostname bastion"):
        dbq.start_tunnel(
            {"host": "db.internal", "port": 5432}, {"host": "bastion", "port": 22}, 5
        )
    assert proc.stderr.closed


def test_start_tunnel_times_out(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(dbq.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(dbq, "_free_port", lambda: 4567)
    monkeypatch.setattr(dbq, "_port_open", lambda port: False)
    monkeypatch.setattr(dbq.subprocess, "Popen", lambda *a, **k: proc)

    with pytest.raises(DbqError, match="did not open"):
        dbq.start_tunnel(
            {"host": "db.internal", "port": 5432}, {"host": "bastion", "port": 22}, -1
        )
    assert proc.terminated
    assert proc.stderr.closed


def test_start_tunnel_returns_once_local_port_is_open(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(dbq.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(dbq, "_free_port", lambda: 4567)
    monkeypatch.setattr(dbq, "_port_open", lambda port: True)
    monkeypatch.setattr(dbq.subprocess, "Popen", lambda *a, **k: proc)

    tunnel = dbq.start_tunnel(
        {"host": "db.internal", "port": 5432},
        {"host": "bastion", "port": 22, "user": "deploy"},
        5,
    )
    assert tunnel.local_port == 4567
    assert tunnel.destination == "deploy@bastion"

    tunnel.close()
    assert proc.terminated
    assert proc.stderr.closed


def test_start_tunnel_runs_a_real_process(monkeypatch, tmp_path):
    fake = tmp_path / "ssh"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys, time\n"
        "args = sys.argv[1:]\n"
        'spec = args[args.index("-L") + 1]\n'
        'local = int(spec.split(":")[1])\n'
        "sock = socket.socket()\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        'sock.bind(("127.0.0.1", local))\n'
        "sock.listen()\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(dbq.shutil, "which", lambda _: str(fake))

    tunnel = dbq.start_tunnel(
        {"host": "db.internal", "port": 5432}, {"host": "bastion", "port": 22}, 5
    )
    try:
        assert tunnel.local_port > 0
        assert tunnel.proc.poll() is None
    finally:
        tunnel.close()
    assert tunnel.proc.poll() is not None


class _FakeConn:
    def __init__(self):
        self.rolled = False
        self.closed = False

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True


class _FakeTunnel:
    local_port = 4567
    destination = "deploy@bastion.example.com"

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_main_connects_through_tunnel(tunnel_config, monkeypatch, capsys):
    seen = {}
    conn = _FakeConn()
    tunnel = _FakeTunnel()

    def fake_connect(driver, params, timeout):
        seen.update(params)
        return conn

    monkeypatch.setattr(dbq, "start_tunnel", lambda params, cfg, timeout: tunnel)
    monkeypatch.setattr(dbq, "connect", fake_connect)
    monkeypatch.setattr(
        dbq, "run", lambda conn, driver, sql, max_rows: (["n"], [[1]], False)
    )

    code, out, err = _run(capsys, ["--profile", "pg", "--sql", "SELECT 1"])

    assert code == 0, err
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 4567
    assert "host: db.internal" in out.splitlines()
    assert "tunnel: deploy@bastion.example.com" in out.splitlines()
    assert tunnel.closed
    assert conn.rolled and conn.closed


def test_main_no_ssh_tunnel_flag_skips_tunnel(sqlite_tunnel_config, capsys):
    code, out, err = _run(
        capsys,
        ["--profile", "local", "--no-ssh-tunnel", "--sql", "SELECT 1 AS greeting"],
    )
    assert code == 0, err
    assert "tunnel:" not in out
    assert "greeting" in out


def test_main_rejects_tunnel_for_sqlite(sqlite_tunnel_config, capsys):
    code, out, err = _run(capsys, ["--profile", "local", "--sql", "SELECT 1"])
    assert code == 2
    assert out == ""
    assert "ssh_tunnel is not supported" in err


def test_main_closes_tunnel_when_query_fails(tunnel_config, monkeypatch, capsys):
    conn = _FakeConn()
    tunnel = _FakeTunnel()

    def boom(*args):
        raise DbqError("query failed")

    monkeypatch.setattr(dbq, "start_tunnel", lambda params, cfg, timeout: tunnel)
    monkeypatch.setattr(dbq, "connect", lambda *args: conn)
    monkeypatch.setattr(dbq, "run", boom)

    code, _, err = _run(capsys, ["--profile", "pg", "--sql", "SELECT 1"])

    assert code == 2
    assert "query failed" in err
    assert tunnel.closed
    assert conn.rolled and conn.closed
