"""Nightly inventory report (FR3): writes CSV to stdout so it lands in the log pipeline, not on a local disk."""

import csv
import sqlite3
import sys

from app.main import DB_PATH, init_db


def main() -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute("SELECT sku, name, quantity, price FROM items ORDER BY sku").fetchall()
    writer = csv.writer(sys.stdout)
    writer.writerow(["sku", "name", "quantity", "price"])
    writer.writerows(rows)


if __name__ == "__main__":
    main()
