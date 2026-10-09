"""When journal rows are stamped — the faults behind a replay that kept
incidents too long and lost warnings.

The feed's `updated` time does not move when an entity merely drops out of the
feed, or reappears after a blip. Stamping those changes with it backdated every
resolution to the entity's last edit, and a reappearance collided with its own
earlier row on the unique index and was silently dropped — a live warning stayed
"over" in the journal until its next reissue.
"""
from datetime import datetime, timedelta

from app import database, history

T0 = datetime(2026, 10, 3, 12, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _rows(key):
    return database.read_df(
        "SELECT effective_ts, active FROM entity_state_history "
        "WHERE entity_key = ? ORDER BY id", [key])


def test_a_drop_out_is_stamped_when_noticed_not_at_the_last_edit(db, monkeypatch):
    history.record_state(history.FIRE, "w1", {"level": "Advice"}, effective_ts=at(0))
    monkeypatch.setattr(history, "_stamp", _clock(at(240)))
    history.record_state(history.FIRE, "w1", {"level": "Advice"},
                         effective_ts=at(0), active=False)
    assert list(_rows("w1")["effective_ts"]) == [stamp(at(0)), stamp(at(240))]


def test_a_reappearance_after_a_blip_is_recorded(db, monkeypatch):
    real = history._stamp
    history.record_state(history.FIRE, "w1", {"level": "Advice"}, effective_ts=at(0))
    monkeypatch.setattr(history, "_stamp", _clock(at(30)))
    history.record_state(history.FIRE, "w1", {"level": "Advice"},
                         effective_ts=at(0), active=False)
    monkeypatch.setattr(history, "_stamp", _clock(at(33)))
    assert history.record_state(history.FIRE, "w1", {"level": "Advice"},
                                effective_ts=at(0))
    monkeypatch.setattr(history, "_stamp", real)
    assert list(_rows("w1")["active"]) == [1, 0, 1]
    assert len(history.state_at(history.FIRE, at(60))) == 1


def test_batch_writer_applies_the_same_rule(db, monkeypatch):
    history.record_batch(history.FIRE, [{"entity_key": "i1", "state": {"s": "Going"},
                                         "effective_ts": at(0)}])
    monkeypatch.setattr(history, "_stamp", _clock(at(90)))
    history.record_batch(history.FIRE, [{"entity_key": "i1", "state": {"s": "Going"},
                                         "active": False, "effective_ts": at(0)}])
    assert list(_rows("i1")["effective_ts"])[-1] == stamp(at(90))


def test_a_genuine_source_time_is_still_used(db):
    history.record_state(history.FIRE, "i1", {"s": "Going"}, effective_ts=at(0))
    history.record_state(history.FIRE, "i1", {"s": "Contained"}, effective_ts=at(50))
    assert list(_rows("i1")["effective_ts"]) == [stamp(at(0)), stamp(at(50))]


def test_existing_backdated_tombstones_are_read_at_their_recorded_time(db):
    """Rows written before the fix: a tombstone carrying the last edit's time.
    Replay must keep the entity on the map until we actually saw it go."""
    database.insert_rows("entity_state_history", [
        {"source": "fire", "entity_key": "old", "effective_ts": stamp(at(0)),
         "recorded_at": stamp(at(1)), "active": 1, "state_json": "{}",
         "state_hash": "a"},
        {"source": "fire", "entity_key": "old", "effective_ts": stamp(at(0)),
         "recorded_at": stamp(at(300)), "active": 0, "state_json": "{}",
         "state_hash": "b"},
    ])
    assert len(history.state_at(history.FIRE, at(120))) == 1
    assert history.state_at(history.FIRE, at(301)).empty
    between = history.states_between(history.FIRE, at(100), at(400))
    assert list(between["effective_ts"]) == [stamp(at(300))]


def test_a_tombstone_with_its_own_source_time_is_left_alone(db):
    database.insert_rows("entity_state_history", [
        {"source": "fire", "entity_key": "safe", "effective_ts": stamp(at(0)),
         "recorded_at": stamp(at(1)), "active": 1, "state_json": "{}",
         "state_hash": "a"},
        {"source": "fire", "entity_key": "safe", "effective_ts": stamp(at(100)),
         "recorded_at": stamp(at(500)), "active": 0, "state_json": "{}",
         "state_hash": "b"},
    ])
    assert history.state_at(history.FIRE, at(150)).empty


def _clock(when):
    real = history._stamp

    def fake(value=None):
        return real(when) if value is None else real(value)
    return fake
