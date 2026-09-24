"""Opt-in alert sounds: what the server reports as currently alertable.

Newness and the choice of sound per tick are decided in the browser
(assets/alert_sounds_ui.js); these tests pin the contract it relies on —
stable keys, a level change being a NEW key on the SAME entity, and which
entities can "resolve".
"""
from app import database, sound_alerts


def _keys(events):
    return {e["key"]: e for e in events}


def _vic(source_id, level, resolved=0):
    database.execute(
        "INSERT INTO fire_incidents (source_id, feed_type, warning_level, resolved) "
        "VALUES (?, 'warning', ?, ?)", [source_id, level, resolved])


def test_nothing_active_means_no_events(db):
    assert sound_alerts.current_events() == []


def test_vicemergency_levels_map_to_sounds(db):
    _vic("a", "Emergency Warning")
    _vic("b", "Watch and Act")
    _vic("c", "Advice")
    _vic("d", "Evacuate")
    _vic("gone", "Emergency Warning", resolved=1)
    ev = _keys(sound_alerts.current_events())
    assert ev["vic:a:emergency warning"]["sound"] == "emergency"
    assert ev["vic:b:watch and act"]["sound"] == "escalation"
    assert ev["vic:c:advice"]["sound"] == "advice"
    assert ev["vic:d:evacuate"]["sound"] == "emergency"
    assert not any(k.startswith("vic:gone") for k in ev)
    assert all(e["resolvable"] for e in ev.values())


def test_escalation_is_a_new_key_on_the_same_entity(db):
    _vic("a", "Advice")
    (before,) = sound_alerts.current_events()
    database.execute("UPDATE fire_incidents SET warning_level = 'Watch and Act'")
    (after,) = sound_alerts.current_events()
    assert before["key"] != after["key"]
    assert before["entity"] == after["entity"] == "vic:a"


def test_bom_warning_and_sews(db):
    database.execute(
        "INSERT INTO weather_warnings (warning_id, title, message, active) VALUES "
        "('IDV1', 'Flood Watch', 'rain expected', 1), "
        "('IDV2', 'Severe Weather', '<p>The Standard Emergency Warning Signal "
        "may be used</p>', 1), "
        "('IDV3', 'Old', 'x', 0)")
    ev = _keys(sound_alerts.current_events())
    assert ev["bom:IDV1:on"]["sound"] == "advice"
    assert ev["bom:IDV2:sews"]["sound"] == "emergency"
    assert not any(k.startswith("bom:IDV3") for k in ev)


def test_flood_gauges_at_or_above_minor_are_keyed_by_class(db):
    database.execute(
        "INSERT INTO flood_levels (station_key, station_name, minor, moderate, major) "
        "VALUES ('river at town', 'River at Town', 2.0, 3.0, 4.0), "
        "('dry creek', 'Dry Creek', 2.0, 3.0, 4.0)")
    database.execute(
        "INSERT INTO flood_observations (event, station_name, height_m, timestamp) VALUES "
        "('live', 'River at Town', 1.5, '2026-09-24 10:00:00'), "
        "('live', 'River at Town', 3.2, '2026-09-24 11:00:00'), "
        "('live', 'Dry Creek', 1.0, '2026-09-24 11:00:00')")
    (ev,) = sound_alerts.current_events()
    assert ev["key"] == "flood:river at town:moderate flooding"
    assert ev["sound"] == "escalation"
    # A gauge dipping back under Minor is not announced as "resolved".
    assert ev["resolvable"] is False


def test_pager_escalations_only(db):
    database.execute(
        "INSERT INTO pager_messages (msg_hash, received_at, message, is_escalation) "
        "VALUES ('h1', '2026-09-24 10:00:00', 'MAKE TANKERS 5', 1), "
        "('h2', '2026-09-24 10:01:00', 'STRUCTURE FIRE', 0)")
    (ev,) = sound_alerts.current_events()
    assert ev["sound"] == "pager"
    assert ev["key"].startswith("pager:")


def test_collector_error_keyed_on_text(db, monkeypatch):
    from app.collector import manager
    status = {"fire": {"running": True, "last_error": "boom"},
              "flood": {"running": True, "last_error": None}}
    monkeypatch.setattr(manager, "status", lambda: status)
    (first,) = sound_alerts.current_events()
    assert first["sound"] == "system" and first["entity"] == "sys:fire"
    (again,) = sound_alerts.current_events()
    assert again["key"] == first["key"]      # same failure -> same key, one sound
    status["fire"]["last_error"] = "different"
    (changed,) = sound_alerts.current_events()
    assert changed["key"] != first["key"]


def test_one_broken_source_does_not_silence_the_rest(db, monkeypatch):
    _vic("a", "Emergency Warning")

    def broken():
        raise RuntimeError("source down")
    monkeypatch.setattr(sound_alerts, "SOURCES", (broken, sound_alerts._vicemergency))
    (ev,) = sound_alerts.current_events()
    assert ev["sound"] == "emergency"
