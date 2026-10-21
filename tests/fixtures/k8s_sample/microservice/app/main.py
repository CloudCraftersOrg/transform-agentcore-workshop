"""inventory-service: the legacy Inventory & Orders JSON API as a stateless-ish microservice.

Same API contract as the monolith (FR1). Config comes from the environment; logs are JSON on stdout (NFR5).
The sample keeps SQLite (DB_PATH) so it runs anywhere; production points DB_PATH-equivalent config at RDS.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request

DB_PATH = os.getenv("DB_PATH", "/tmp/inventory.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                                  quantity INTEGER NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES items(id),
                                   quantity INTEGER NOT NULL, created_at TEXT NOT NULL);
"""
SEED = [
    ("SKU-1001", "Industrial bolt M8", 1200, 0.35),
    ("SKU-1002", "Hex nut M8", 950, 0.12),
    ("SKU-2001", "Hydraulic pump HP-20", 7, 1299.00),
    ("SKU-2002", "Pressure valve PV-3", 42, 89.90),
    ("SKU-3001", "Safety gloves (pair)", 5, 14.50),
]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": self.formatTime(record), "level": record.levelname, "msg": record.getMessage()})


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("inventory-service")
log.addHandler(handler)
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        db.executemany("INSERT OR IGNORE INTO items (sku, name, quantity, price) VALUES (?, ?, ?, ?)", SEED)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=5)
        g.db.row_factory = sqlite3.Row
    return g.db


app = Flask(__name__)
init_db()


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/readyz")
def readyz():
    get_db().execute("SELECT 1").fetchone()
    return jsonify(status="ready")


@app.get("/api/items")
def list_items():
    search = request.args.get("search")
    if search:  # parameterized: fixes the legacy SQL injection (SEC5)
        rows = get_db().execute("SELECT * FROM items WHERE name LIKE ?", (f"%{search}%",)).fetchall()
    else:
        rows = get_db().execute("SELECT * FROM items ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/items")
def create_item():
    data = request.get_json(force=True)
    db = get_db()
    cur = db.execute("INSERT INTO items (sku, name, quantity, price) VALUES (?, ?, ?, ?)",
                     (data["sku"], data["name"], int(data.get("quantity", 0)), float(data.get("price", 0))))
    db.commit()
    log.info("created item %s", data["sku"])
    return jsonify(id=cur.lastrowid), 201


@app.route("/api/items/<int:item_id>", methods=["GET", "PUT", "DELETE"])
def item(item_id: int):
    db = get_db()
    if request.method == "GET":
        row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return (jsonify(dict(row)), 200) if row else (jsonify(error="not found"), 404)
    if request.method == "PUT":
        data = request.get_json(force=True)
        db.execute("UPDATE items SET quantity = ?, price = ? WHERE id = ?", (data["quantity"], data["price"], item_id))
        db.commit()
        return jsonify(ok=True)
    if not ADMIN_TOKEN or request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify(error="forbidden"), 403
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/orders")
def create_order():
    data = request.get_json(force=True)
    item_id, quantity = int(data["item_id"]), int(data["quantity"])
    db = get_db()
    # Atomic check-and-decrement: never oversell (FR4).
    cur = db.execute("UPDATE items SET quantity = quantity - ? WHERE id = ? AND quantity >= ?", (quantity, item_id, quantity))
    if cur.rowcount == 0:
        db.rollback()
        return jsonify(error="insufficient stock"), 409
    order = db.execute("INSERT INTO orders (item_id, quantity, created_at) VALUES (?, ?, ?)",
                       (item_id, quantity, datetime.now(timezone.utc).isoformat()))
    db.commit()
    log.info("order %s: item %s x%s", order.lastrowid, item_id, quantity)
    return jsonify(order_id=order.lastrowid), 201
