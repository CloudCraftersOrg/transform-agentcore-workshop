"""Ticketing JSON API ported to the pattern. Paths, methods, status codes and JSON keys are unchanged."""

import logging

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import execute, fetch_all, fetch_one, transaction
from .web import float_field, int_field, json_body, require_admin_token, utc_now_iso

bp = Blueprint("api", __name__)
log = logging.getLogger(__name__)


@bp.get("/api/events")
def list_events():
    q = request.args.get("q")
    if q:
        # SQL injection fix: the search term is a bound parameter, never part of the SQL text.
        # LOWER() keeps SQLite's case-insensitive LIKE behaviour on PostgreSQL.
        rows = fetch_all("SELECT * FROM events WHERE LOWER(title) LIKE LOWER(:pattern) ORDER BY id", pattern=f"%{q}%")
    else:
        rows = fetch_all("SELECT * FROM events ORDER BY id")
    return jsonify(rows)


@bp.post("/api/events")
def create_event():
    data = json_body("code", "title")
    params = {
        "code": data["code"],
        "title": data["title"],
        "seats": int_field(data, "seats", default=0),
        "price": float_field(data, "price", default=0),
    }
    try:
        with transaction() as conn:
            # RETURNING id works on SQLite and PostgreSQL (cursor.lastrowid does not on PostgreSQL).
            new_id = conn.execute(
                text(
                    "INSERT INTO events (code, title, seats, price) VALUES (:code, :title, :seats, :price) RETURNING id"
                ),
                params,
            ).scalar_one()
    except IntegrityError:
        abort(409, "code already exists")
    log.info("created event", extra={"code": data["code"]})
    return jsonify({"id": new_id}), 201


@bp.get("/api/events/<int:event_id>")
def get_event(event_id: int):
    row = fetch_one("SELECT * FROM events WHERE id = :id", id=event_id)
    return (jsonify(row), 200) if row else (jsonify({"error": "not found"}), 404)


@bp.delete("/api/events/<int:event_id>")
def delete_event(event_id: int):
    require_admin_token("X-Api-Key")  # the key now comes from the ADMIN_TOKEN secret
    try:
        execute("DELETE FROM events WHERE id = :id", id=event_id)
    except IntegrityError:
        abort(409, "event has bookings")
    return jsonify({"ok": True})


@bp.post("/api/bookings")
def create_booking():
    data = json_body("event_id", "seats")
    event_id, seats = int_field(data, "event_id", minimum=1), int_field(data, "seats", minimum=1)
    with transaction() as conn:
        # Atomic conditional decrement: never oversells, even with concurrent requests across many pods.
        updated = conn.execute(
            text("UPDATE events SET seats = seats - :seats WHERE id = :event_id AND seats >= :seats"),
            {"seats": seats, "event_id": event_id},
        ).rowcount
        if updated != 1:
            return jsonify({"error": "sold out"}), 409
        booking_id = conn.execute(
            text(
                "INSERT INTO bookings (event_id, seats, created_at) "
                "VALUES (:event_id, :seats, :created_at) RETURNING id"
            ),
            {"event_id": event_id, "seats": seats, "created_at": utc_now_iso()},
        ).scalar_one()
    log.info("booking created", extra={"booking_id": booking_id, "event_id": event_id, "seats": seats})
    return jsonify({"booking_id": booking_id}), 201
