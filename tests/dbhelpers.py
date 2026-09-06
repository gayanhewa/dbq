"""Raw driver access for the integration tests: autocommit connections that
set up and inspect fixtures outside dbq's read-only transaction."""

import time

import dbq

ENV_VARS = {
    "oracle": "DBQ_TEST_ORACLE",
    "mysql": "DBQ_TEST_MYSQL",
    "postgres": "DBQ_TEST_POSTGRES",
    "sqlserver": "DBQ_TEST_SQLSERVER",
}

# Token the server puts in its error when a write hits the read-only transaction.
READONLY_ERROR_TOKEN = {
    "oracle": "ORA-01456",
    "mysql": "1792",
    "postgres": "25006",
}


def connect_autocommit(driver, dsn, timeout=10):
    p = dbq.parse_dsn(dsn, driver)
    if driver == "oracle":
        import oracledb

        conn = oracledb.connect(
            user=p["user"],
            password=p["password"],
            dsn=f"{p['host']}:{p['port']}/{p['database']}",
            tcp_connect_timeout=timeout,
        )
        conn.autocommit = True
        return conn
    if driver == "mysql":
        import pymysql

        return pymysql.connect(
            host=p["host"],
            port=p["port"],
            user=p["user"],
            password=p["password"],
            database=p["database"] or None,
            connect_timeout=timeout,
            autocommit=True,
        )
    if driver == "postgres":
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(
            user=p["user"],
            password=p["password"],
            host=p["host"],
            port=p["port"],
            database=p["database"] or None,
            timeout=timeout,
        )
        conn.autocommit = True
        return conn
    if driver == "sqlserver":
        import pytds

        return pytds.connect(
            dsn=p["host"],
            port=p["port"],
            database=p["database"] or None,
            user=p["user"],
            password=p["password"],
            login_timeout=timeout,
            autocommit=True,
        )
    raise ValueError(driver)


def wait_for_db(driver, dsn, deadline_s=180):
    """CI service containers (Oracle, SQL Server especially) accept TCP long
    before they accept logins, so retry the whole connect until it works."""
    start = time.monotonic()
    while True:
        try:
            return connect_autocommit(driver, dsn)
        except Exception as e:  # noqa: BLE001 - each driver raises its own family
            if time.monotonic() - start > deadline_s:
                raise RuntimeError(f"{driver} not reachable after {deadline_s}s: {e}") from e
            time.sleep(3)


def execute(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
    finally:
        cur.close()


def fetchall(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def create_widget(driver, conn):
    try:
        execute(conn, "DROP TABLE dbq_widget")
    except Exception:  # noqa: BLE001 - leftover from an aborted run, or absent
        pass
    execute(conn, "CREATE TABLE dbq_widget (id int, name varchar(50), note varchar(50))")
    # One statement per row: Oracle has no multi-row VALUES.
    for values in ("1, 'alpha', NULL", "2, 'beta', 'has,comma'", "3, 'gamma', 'x'"):
        execute(conn, f"INSERT INTO dbq_widget (id, name, note) VALUES ({values})")
    if driver == "oracle":
        # A READ ONLY transaction snapshots at an SCN whose time mapping is
        # ~3 s coarse; querying a table created inside that window raises
        # ORA-01466 "table definition has changed".
        time.sleep(5)


def drop_widget(conn):
    execute(conn, "DROP TABLE dbq_widget")
