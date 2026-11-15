import time

import psycopg


def run_worker(dsn):
    conn = psycopg.connect(dsn)
    while True:
        rows = conn.execute("SELECT id FROM jobs WHERE status = 'pending' LIMIT 10").fetchall()
        for row in rows:
            handle(row)
        time.sleep(1)


def handle(row):
    print(row)
