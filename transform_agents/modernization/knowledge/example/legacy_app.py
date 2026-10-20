# ruff: noqa
# BEFORE: excerpt of a legacy ticketing monolith (same smells as typical 2010s Flask apps).
DB_PATH = "/var/lib/tickets/tickets.db"
API_KEY = "letmein"


@app.route("/api/events", methods=["GET"])
def list_events():
    q = request.args.get("q")
    if q:
        rows = get_db().execute("SELECT * FROM events WHERE title LIKE '%" + q + "%'").fetchall()
    else:
        rows = get_db().execute("SELECT * FROM events").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/events", methods=["POST"])
def create_event():
    data = request.get_json()
    cur = get_db().execute(
        "INSERT INTO events (code, title, seats, price) VALUES (?, ?, ?, ?)",
        (data["code"], data["title"], data.get("seats", 0), data.get("price", 0)),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/events/<int:event_id>", methods=["GET", "DELETE"])
def event(event_id):
    db = get_db()
    if request.method == "GET":
        row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return (jsonify(dict(row)), 200) if row else (jsonify({"error": "not found"}), 404)
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"error": "forbidden"}), 403
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    data = request.get_json()
    db = get_db()
    row = db.execute("SELECT seats FROM events WHERE id = ?", (data["event_id"],)).fetchone()
    if row is None or row["seats"] < data["seats"]:
        return jsonify({"error": "sold out"}), 409
    db.execute("UPDATE events SET seats = seats - ? WHERE id = ?", (data["seats"], data["event_id"]))
    cur = db.execute(
        "INSERT INTO bookings (event_id, seats, created_at) VALUES (?, ?, ?)",
        (data["event_id"], data["seats"], datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"booking_id": cur.lastrowid}), 201


@app.route("/reports/nightly")  # -> becomes the CronJob `python -m app.report` (not ported as a route)
def nightly_report(): ...
