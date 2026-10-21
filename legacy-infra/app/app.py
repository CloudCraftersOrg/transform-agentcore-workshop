# Legacy Inventory & Orders - monolith
# Originally written 2017, "temporarily" deployed on a single EC2 box.
# Run: python3 app.py   (systemd unit in ops/legacy-inventory.service)
import csv
import datetime
import logging
import os
import sqlite3

from flask import Flask, g, jsonify, redirect, render_template, request

# --- configuration (TODO: move to a config file some day) ---
DB_PATH = "/opt/legacy-inventory/data/inventory.db"
REPORT_DIR = "/opt/legacy-inventory/reports"
LOG_FILE = "/var/log/legacy-inventory/app.log"
SECRET_KEY = "s3cr3t-legacy-key-2017"
ADMIN_TOKEN = "admin123"
LOW_STOCK_THRESHOLD = 10

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("legacy-inventory")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    here = os.path.dirname(os.path.abspath(__file__))
    db = sqlite3.connect(DB_PATH)
    with open(os.path.join(here, "schema.sql")) as f:
        db.executescript(f.read())
    if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        with open(os.path.join(here, "seed.sql")) as f:
            db.executescript(f.read())
    db.commit()
    db.close()


# ---------------- web UI ----------------
@app.route("/")
def index():
    items = get_db().execute("SELECT * FROM items ORDER BY name").fetchall()
    orders = get_db().execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10").fetchall()
    return render_template("index.html", items=items, orders=orders, threshold=LOW_STOCK_THRESHOLD,
                           hostname=os.uname()[1])


@app.route("/items/new", methods=["POST"])
def new_item_form():
    db = get_db()
    db.execute("INSERT INTO items (sku, name, quantity, price) VALUES (?, ?, ?, ?)",
               (request.form["sku"], request.form["name"], int(request.form["quantity"]), float(request.form["price"])))
    db.commit()
    return redirect("/")


# ---------------- JSON API ----------------
@app.route("/api/items", methods=["GET"])
def list_items():
    search = request.args.get("search")
    if search:
        # quick search added in 2019
        rows = get_db().execute("SELECT * FROM items WHERE name LIKE '%" + search + "%'").fetchall()
    else:
        rows = get_db().execute("SELECT * FROM items").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/items", methods=["POST"])
def create_item():
    data = request.get_json()
    db = get_db()
    cur = db.execute("INSERT INTO items (sku, name, quantity, price) VALUES (?, ?, ?, ?)",
                     (data["sku"], data["name"], data.get("quantity", 0), data.get("price", 0)))
    db.commit()
    log.info("created item %s", data["sku"])
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/items/<int:item_id>", methods=["GET", "PUT", "DELETE"])
def item(item_id):
    db = get_db()
    if request.method == "GET":
        row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return (jsonify(dict(row)), 200) if row else (jsonify({"error": "not found"}), 404)
    if request.method == "PUT":
        data = request.get_json()
        db.execute("UPDATE items SET quantity = ?, price = ? WHERE id = ?", (data["quantity"], data["price"], item_id))
        db.commit()
        return jsonify({"ok": True})
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json()
    db = get_db()
    row = db.execute("SELECT quantity FROM items WHERE id = ?", (data["item_id"],)).fetchone()
    if row is None or row["quantity"] < data["quantity"]:
        return jsonify({"error": "insufficient stock"}), 409
    db.execute("UPDATE items SET quantity = quantity - ? WHERE id = ?", (data["quantity"], data["item_id"]))
    cur = db.execute("INSERT INTO orders (item_id, quantity, created_at) VALUES (?, ?, ?)",
                     (data["item_id"], data["quantity"], datetime.datetime.utcnow().isoformat()))
    db.commit()
    log.info("order %s: item %s x%s", cur.lastrowid, data["item_id"], data["quantity"])
    return jsonify({"order_id": cur.lastrowid}), 201


# ---------------- reports (called by cron every night) ----------------
@app.route("/reports/daily")
def daily_report():
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "inventory-%s.csv" % datetime.date.today().isoformat())
    rows = get_db().execute("SELECT sku, name, quantity, price FROM items").fetchall()
    with open(path, "w") as f:
        writer = csv.writer(f)
        writer.writerow(["sku", "name", "quantity", "price"])
        for r in rows:
            writer.writerow([r["sku"], r["name"], r["quantity"], r["price"]])
    return "report written to %s\n" % path


@app.route("/status")
def status():
    return "OK"


if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
