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
    """SQLite's PRAGMA query_only = ON prevents writes."""
    driver, dsn, raw = sqlite_db
    params = dbq.parse_dsn(dsn, driver)
    conn = dbq.connect(driver, params, 30)
    try:
        with pytest.raises(Exception):
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
