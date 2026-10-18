"""End-to-end against real databases. Each driver runs only when its
DBQ_TEST_<DRIVER> env var holds a DSN; otherwise its cases skip."""

import json
import os

import pytest

import dbq
import dbhelpers

# Server-based drivers only: sqlite and libsql have their own fixtures below.
_SERVER_DRIVERS = ["oracle", "mysql", "postgres", "sqlserver"]
DRIVERS = _SERVER_DRIVERS


@pytest.fixture(scope="module", params=DRIVERS)
def db(request):
    driver = request.param
    dsn = os.environ.get(dbhelpers.ENV_VARS[driver])
    if not dsn:
        pytest.skip(f"{dbhelpers.ENV_VARS[driver]} not set")
    conn = dbhelpers.wait_for_db(driver, dsn)
    dbhelpers.create_widget(driver, conn)
    yield driver, dsn, conn
    try:
        dbhelpers.drop_widget(conn)
    finally:
        conn.close()


def run_dbq(capsys, driver, dsn, *args):
    code = dbq.main(["--driver", driver, "--dsn", dsn, *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


SELECT_ALL = "SELECT id, name, note FROM dbq_widget ORDER BY id"


def test_select_toon(db, capsys):
    driver, dsn, _ = db
    code, out, err = run_dbq(capsys, driver, dsn, "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == "profile: <ad-hoc>"
    assert lines[1] == f"driver: {driver}"
    assert lines[2].startswith("host: ")
    assert lines[3] == "count: 3"
    # Oracle upper-cases identifiers, postgres lower-cases; the shape is what matters.
    assert lines[4].lower() == "rows[3]{id,name,note}:"
    assert lines[5:] == ["  1,alpha,null", '  2,beta,"has,comma"', "  3,gamma,x"]


def test_select_json(db, capsys):
    driver, dsn, _ = db
    code, out, err = run_dbq(capsys, driver, dsn, "--json", "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert doc["driver"] == driver
    assert doc["count"] == 3
    assert [c.lower() for c in doc["columns"]] == ["id", "name", "note"]
    rows = [{k.lower(): v for k, v in r.items()} for r in doc["rows"]]
    assert rows == [
        {"id": 1, "name": "alpha", "note": None},
        {"id": 2, "name": "beta", "note": "has,comma"},
        {"id": 3, "name": "gamma", "note": "x"},
    ]


def test_no_rows(db, capsys):
    driver, dsn, _ = db
    code, out, _ = run_dbq(capsys, driver, dsn, "--sql", "SELECT id FROM dbq_widget WHERE id = 99")
    assert code == 0
    assert "count: 0" in out.splitlines()
    assert out.splitlines()[-1] == "  (no rows)"


def test_tables_lists_widget(db, capsys):
    driver, dsn, _ = db
    code, out, err = run_dbq(capsys, driver, dsn, "--tables")
    assert (code, err) == (0, "")
    names = [line.strip().lower() for line in out.splitlines()[5:]]
    assert "dbq_widget" in names


def test_describe_widget(db, capsys):
    driver, dsn, _ = db
    code, out, err = run_dbq(capsys, driver, dsn, "--describe", "dbq_widget")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert "count: 3" in lines
    header = next(l for l in lines if l.startswith("rows["))
    assert header.startswith("rows[3]{")
    body = lines[lines.index(header) + 1 :]
    columns = [line.strip().split(",")[0].lower() for line in body]
    assert columns == ["id", "name", "note"]
    for line in body:
        assert len(line.strip().split(",")) == 3, line


def test_max_rows_truncates_with_header(db, capsys):
    driver, dsn, _ = db
    code, out, err = run_dbq(capsys, driver, dsn, "--max-rows", "2", "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert "count: 2" in lines
    assert "truncated: true, showing first 2" in lines
    assert lines[-2:] == ["  1,alpha,null", '  2,beta,"has,comma"']


def test_max_rows_exact_fit_is_not_truncated(db, capsys):
    driver, dsn, _ = db
    code, out, _ = run_dbq(capsys, driver, dsn, "--max-rows", "3", "--sql", SELECT_ALL)
    assert code == 0
    assert "truncated" not in out


def test_write_is_rejected_by_check_before_connecting(db, capsys):
    driver, dsn, conn = db
    code, _, err = run_dbq(capsys, driver, dsn, "--sql", "DELETE FROM dbq_widget WHERE id = 1")
    assert code == 2
    assert err == "error: only SELECT and WITH are allowed, got DELETE\n"
    assert dbhelpers.fetchall(conn, "SELECT count(*) FROM dbq_widget")[0][0] == 3


def test_connection_is_read_only(db):
    """The real guarantee: a write through the connection dbq opens never lands,
    even when the SQL check is bypassed."""
    driver, dsn, raw = db
    params = dbq.parse_dsn(dsn, driver)
    conn = dbq.connect(driver, params, 30)
    try:
        cur = conn.cursor()
        if driver == "sqlserver":
            # No read-only transaction mode; dbq relies on rolling back the
            # explicit transaction connect() opened.
            cur.execute("DELETE FROM dbq_widget WHERE id = 1")
        else:
            with pytest.raises(Exception) as exc:
                cur.execute("DELETE FROM dbq_widget WHERE id = 1")
            assert dbhelpers.READONLY_ERROR_TOKEN[driver] in str(exc.value)
    finally:
        if hasattr(conn, "rollback"):
            conn.rollback()
        conn.close()

    assert dbhelpers.fetchall(raw, "SELECT id FROM dbq_widget WHERE id = 1") == [(1,)]


# --- sqlite (stdlib, no container needed) -----------------------------------


@pytest.fixture(scope="module")
def sqlite_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("dbq") / "test.db"
    dsn = str(path)
    conn = dbhelpers.connect_autocommit("sqlite", dsn)
    dbhelpers.create_widget("sqlite", conn)
    yield "sqlite", dsn, conn
    try:
        dbhelpers.drop_widget(conn)
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_sqlite_select_toon(sqlite_db, capsys):
    driver, dsn, _ = sqlite_db
    code, out, err = run_dbq(capsys, driver, dsn, "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    assert out.splitlines()[3] == "count: 3"
    assert "1,alpha,null" in out


def test_sqlite_select_json(sqlite_db, capsys):
    driver, dsn, _ = sqlite_db
    code, out, err = run_dbq(capsys, driver, dsn, "--json", "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert doc["count"] == 3
    assert len(doc["rows"]) == 3


def test_sqlite_tables(sqlite_db, capsys):
    driver, dsn, _ = sqlite_db
    code, out, err = run_dbq(capsys, driver, dsn, "--tables")
    assert (code, err) == (0, "")
    assert "dbq_widget" in out


def test_sqlite_describe(sqlite_db, capsys):
    driver, dsn, _ = sqlite_db
    code, out, err = run_dbq(capsys, driver, dsn, "--describe", "dbq_widget")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert "count: 3" in lines


def test_sqlite_connection_is_read_only(sqlite_db):
    """SQLite's mode=ro URI prevents writes."""
    driver, dsn, raw = sqlite_db
    params = dbq.parse_dsn(dsn, driver)
    conn = dbq.connect(driver, params, 30)
    try:
        with pytest.raises(Exception, match="readonly database"):
            conn.execute("DELETE FROM dbq_widget WHERE id = 1")
    finally:
        conn.close()
    assert dbhelpers.fetchall(raw, "SELECT id FROM dbq_widget WHERE id = 1") == [(1,)]


# --- libsql (local file, no Turso server required) --------------------------


@pytest.fixture(scope="module")
def libsql_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("dbq") / "test_libsql.db"
    dsn = f"file://{path}"
    conn = dbhelpers.connect_autocommit("libsql", dsn)
    dbhelpers.create_widget("libsql", conn)
    yield "libsql", dsn, conn
    try:
        dbhelpers.drop_widget(conn)
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_libsql_select_toon(libsql_db, capsys):
    driver, dsn, _ = libsql_db
    code, out, err = run_dbq(capsys, driver, dsn, "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    assert out.splitlines()[3] == "count: 3"
    assert "1,alpha,null" in out


def test_libsql_select_json(libsql_db, capsys):
    driver, dsn, _ = libsql_db
    code, out, err = run_dbq(capsys, driver, dsn, "--json", "--sql", SELECT_ALL)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert doc["count"] == 3
    assert len(doc["rows"]) == 3


def test_libsql_tables(libsql_db, capsys):
    driver, dsn, _ = libsql_db
    code, out, err = run_dbq(capsys, driver, dsn, "--tables")
    assert (code, err) == (0, "")
    assert "dbq_widget" in out


def test_libsql_describe(libsql_db, capsys):
    driver, dsn, _ = libsql_db
    code, out, err = run_dbq(capsys, driver, dsn, "--describe", "dbq_widget")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert "count: 3" in lines


# --- ssh tunnel (needs a bastion container) ---------------------------------

SSH_TUNNEL_ENV = "DBQ_TEST_SSH_TUNNEL"
SSH_KEY_ENV = "DBQ_TEST_SSH_KEY"
POSTGRES_TUNNEL_ENV = "DBQ_TEST_POSTGRES_TUNNEL"


@pytest.fixture(scope="module")
def ssh_tunnel_db(tmp_path_factory):
    """Postgres through a real sshd. CI adds the bastion host key to
    ~/.ssh/known_hosts before running the suite."""
    target = os.environ.get(SSH_TUNNEL_ENV)
    key_file = os.environ.get(SSH_KEY_ENV)
    tunnel_dsn = os.environ.get(POSTGRES_TUNNEL_ENV)
    direct_dsn = os.environ.get(dbhelpers.ENV_VARS["postgres"])

    missing = [
        name
        for name, value in (
            (SSH_TUNNEL_ENV, target),
            (SSH_KEY_ENV, key_file),
            (POSTGRES_TUNNEL_ENV, tunnel_dsn),
            (dbhelpers.ENV_VARS["postgres"], direct_dsn),
        )
        if not value
    ]
    if missing:
        pytest.skip(f"{', '.join(missing)} not set")

    user, _, host_port = target.rpartition("@")
    host, _, port = host_port.rpartition(":")
    if not (user and host and port.isdigit()):
        pytest.skip(f"{SSH_TUNNEL_ENV} must be user@host:port")

    config = tmp_path_factory.mktemp("dbq-ssh") / "config.toml"
    config.write_text(
        f"""
[profiles.tunnel]
driver = "postgres"
dsn = {json.dumps(tunnel_dsn)}

[profiles.tunnel.ssh_tunnel]
host = {json.dumps(host)}
port = {int(port)}
user = {json.dumps(user)}
key_file = {json.dumps(key_file)}
"""
    )

    conn = dbhelpers.wait_for_db("postgres", direct_dsn)
    dbhelpers.create_widget("postgres", conn)
    try:
        yield config, dbq.parse_dsn(tunnel_dsn, "postgres")["host"], f"{user}@{host}"
    finally:
        try:
            dbhelpers.drop_widget(conn)
        finally:
            conn.close()


def test_postgres_over_ssh_tunnel(ssh_tunnel_db, monkeypatch, capsys):
    config, db_host, ssh_dest = ssh_tunnel_db
    monkeypatch.setattr(dbq, "CONFIG_PATH", config)

    code = dbq.main(["--profile", "tunnel", "--sql", SELECT_ALL])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    lines = captured.out.splitlines()
    assert f"host: {db_host}" in lines
    assert f"tunnel: {ssh_dest}" in lines
    assert lines[-1] == "  3,gamma,x"
