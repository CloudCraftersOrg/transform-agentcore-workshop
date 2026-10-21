"""Contract tests for the ported API (fixtures from conftest.py: client, seeded, admin_token)."""

from concurrent.futures import ThreadPoolExecutor


def test_list_returns_seed_rows_with_legacy_keys(client, seeded):
    rows = client.get("/api/events").get_json()
    assert len(rows) == 3
    assert set(rows[0]) == {"id", "code", "title", "seats", "price"}


def test_search_is_case_insensitive(client, seeded):
    rows = client.get("/api/events", query_string={"q": "kubernetes"}).get_json()
    assert [r["code"] for r in rows] == ["EV-2"]


def test_search_sql_injection_is_inert(client, seeded):
    response = client.get("/api/events", query_string={"q": "' OR '1'='1"})
    assert response.status_code == 200 and response.get_json() == []


def test_create_then_get(client):
    response = client.post("/api/events", json={"code": "EV-9", "title": "Demo", "seats": 10, "price": 5})
    assert response.status_code == 201
    new_id = response.get_json()["id"]
    assert client.get(f"/api/events/{new_id}").get_json()["title"] == "Demo"


def test_create_validates_input(client):
    assert client.post("/api/events", json={"title": "no code"}).status_code == 400
    assert client.post("/api/events", json={"code": "X", "title": "t", "seats": "many"}).status_code == 400


def test_duplicate_code_is_a_conflict(client, seeded):
    assert client.post("/api/events", json={"code": "EV-1", "title": "dup"}).status_code == 409


def test_get_unknown_returns_404(client):
    response = client.get("/api/events/999")
    assert response.status_code == 404 and response.get_json() == {"error": "not found"}


def test_delete_requires_admin_token(client, seeded, admin_token):
    assert client.delete("/api/events/3").status_code == 403
    assert client.delete("/api/events/3", headers={"X-Api-Key": "wrong"}).status_code == 403
    response = client.delete("/api/events/3", headers={"X-Api-Key": admin_token})
    assert response.status_code == 200 and response.get_json() == {"ok": True}


def test_booking_decrements_seats(client, seeded):
    response = client.post("/api/bookings", json={"event_id": 2, "seats": 2})
    assert response.status_code == 201 and "booking_id" in response.get_json()
    assert client.get("/api/events/2").get_json()["seats"] == 1


def test_booking_never_oversells(client, seeded):
    response = client.post("/api/bookings", json={"event_id": 2, "seats": 4})
    assert response.status_code == 409 and response.get_json() == {"error": "sold out"}
    assert client.get("/api/events/2").get_json()["seats"] == 3
    assert client.post("/api/bookings", json={"event_id": 2, "seats": -1}).status_code == 400


def test_concurrent_bookings_never_oversell(app, seeded):
    def book(_):
        return app.test_client().post("/api/bookings", json={"event_id": 2, "seats": 1}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(book, range(8)))
    assert codes.count(201) == 3 and codes.count(409) == 5
    assert app.test_client().get("/api/events/2").get_json()["seats"] == 0
