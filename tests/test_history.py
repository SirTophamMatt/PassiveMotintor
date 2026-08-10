"""The state-change journal — the foundation Event Replay stands on.

Every test here is really the same question asked at different moments: what
did Passive Monitor know at time T? The answers that must never blur together
are "it hadn't happened yet", "it was happening" and "it was over".
"""
from datetime import datetime, timedelta

import pytest

from app import history

T0 = datetime(2026, 8, 11, 12, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def fire_state(area=100, status="Going", level="Advice"):
    return {"area_ha": area, "status": status, "warning_level": level,
            "location": "Walwa"}


# --------------------------------------------------------------------------- #
# Change-only recording
# --------------------------------------------------------------------------- #
def test_first_state_is_recorded(db):
    assert history.record_state(history.FIRE, "f1", fire_state(),
                                effective_ts=T0) is True
    assert len(history.entity_history(history.FIRE, "f1")) == 1


def test_an_unchanged_state_is_not_recorded_again(db):
    """The single property that makes replay affordable: at a 60-second poll a
    snapshot table would write ~1,440 rows per entity per day."""
    history.record_state(history.FIRE, "f1", fire_state(), effective_ts=T0)
    for minute in range(1, 11):
        assert history.record_state(history.FIRE, "f1", fire_state(),
                                    effective_ts=at(minute)) is False
    assert len(history.entity_history(history.FIRE, "f1")) == 1


def test_key_order_does_not_count_as_a_change(db):
    """Two dicts describing the same state must hash the same however the
    scraper happened to build them, or every cycle would look like a change."""
    history.record_state(history.FIRE, "f1",
                         {"a": 1, "b": 2, "c": None}, effective_ts=T0)
    assert history.record_state(history.FIRE, "f1",
                                {"c": None, "b": 2, "a": 1},
                                effective_ts=at(5)) is False


def test_nan_is_treated_as_missing_not_as_a_change(db):
    """NaN never equals itself, so an unnormalised float would make every
    comparison a change and defeat the whole journal."""
    history.record_state(history.ROADS, "r1", {"lanes": None}, effective_ts=T0)
    assert history.record_state(history.ROADS, "r1", {"lanes": float("nan")},
                                effective_ts=at(5)) is False


def test_a_real_change_is_recorded(db):
    history.record_state(history.FIRE, "f1", fire_state(area=214), effective_ts=T0)
    assert history.record_state(history.FIRE, "f1", fire_state(area=296),
                                effective_ts=at(30)) is True
    assert len(history.entity_history(history.FIRE, "f1")) == 2


def test_resolution_is_a_change_even_when_nothing_else_moved(db):
    history.record_state(history.FIRE, "f1", fire_state(), effective_ts=T0)
    assert history.record_state(history.FIRE, "f1", fire_state(),
                                effective_ts=at(60), active=False) is True


def test_replaying_the_same_cycle_cannot_double_write(db):
    rows = [{"entity_key": "f1", "state": fire_state(), "active": True}]
    assert history.record_batch(history.FIRE, rows, effective_ts=T0) == 1
    assert history.record_batch(history.FIRE, rows, effective_ts=T0) == 0
    assert len(history.entity_history(history.FIRE, "f1")) == 1


# --------------------------------------------------------------------------- #
# Reconstruction — state_at
# --------------------------------------------------------------------------- #
@pytest.fixture
def fire_lifecycle(db):
    """The lifecycle from the spec: appears 12:00, grows 12:30, escalates
    13:10, resolves 17:30."""
    history.record_state(history.FIRE, "f1", fire_state(214, "Going", "Advice"),
                         effective_ts=T0, latitude=-36.1, longitude=147.7)
    history.record_state(history.FIRE, "f1", fire_state(296, "Going", "Advice"),
                         effective_ts=at(30), latitude=-36.1, longitude=147.7)
    history.record_state(history.FIRE, "f1",
                         fire_state(340, "Going", "Watch and Act"),
                         effective_ts=at(70), latitude=-36.1, longitude=147.7)
    history.record_state(history.FIRE, "f1",
                         fire_state(340, "Safe", "Advice"),
                         effective_ts=at(330), active=False,
                         latitude=-36.1, longitude=147.7)
    return "f1"


def test_before_it_existed_the_entity_is_absent(fire_lifecycle):
    """Not "present with empty values" — absent. It hadn't happened yet."""
    assert history.state_at(history.FIRE, at(-30)).empty


def test_at_its_first_appearance_it_is_present(fire_lifecycle):
    df = history.state_at(history.FIRE, T0)
    assert len(df) == 1
    assert df.iloc[0]["area_ha"] == 214


def test_between_changes_the_earlier_state_holds(fire_lifecycle):
    """Replay at 13:00 must return the 12:30 state, not the 13:10 one."""
    df = history.state_at(history.FIRE, at(60))
    assert df.iloc[0]["area_ha"] == 296
    assert df.iloc[0]["warning_level"] == "Advice"


def test_after_an_escalation_the_new_state_is_returned(fire_lifecycle):
    df = history.state_at(history.FIRE, at(120))
    assert df.iloc[0]["area_ha"] == 340
    assert df.iloc[0]["warning_level"] == "Watch and Act"


def test_a_resolved_entity_disappears_at_the_right_moment(fire_lifecycle):
    assert len(history.state_at(history.FIRE, at(329))) == 1
    assert history.state_at(history.FIRE, at(330)).empty
    assert history.state_at(history.FIRE, at(600)).empty


def test_a_resolved_entity_is_still_known_to_have_existed(fire_lifecycle):
    """Replay at 18:00 should know the incident existed but was no longer
    active — which is a different answer from "never happened"."""
    df = history.state_at(history.FIRE, at(600), include_inactive=True)
    assert len(df) == 1
    assert int(df.iloc[0]["active"]) == 0


@pytest.mark.parametrize("offset,expected_area", [
    (0, 214),      # exactly on the first change
    (29, 214),     # one minute before the second
    (30, 296),     # exactly on the second — boundaries are inclusive
    (31, 296),
    (69, 296),
    (70, 340),
])
def test_timestamp_boundaries_are_inclusive(fire_lifecycle, offset, expected_area):
    df = history.state_at(history.FIRE, at(offset))
    assert df.iloc[0]["area_ha"] == expected_area


# --------------------------------------------------------------------------- #
# Multiple entities
# --------------------------------------------------------------------------- #
def test_entities_are_reconstructed_independently(db):
    history.record_state(history.FIRE, "a", fire_state(10), effective_ts=T0)
    history.record_state(history.FIRE, "b", fire_state(20), effective_ts=at(20))
    history.record_state(history.FIRE, "a", fire_state(50), effective_ts=at(40))
    history.record_state(history.FIRE, "b", fire_state(20),
                         effective_ts=at(50), active=False)

    assert set(history.state_at(history.FIRE, at(10))["entity_key"]) == {"a"}
    assert set(history.state_at(history.FIRE, at(30))["entity_key"]) == {"a", "b"}
    at45 = history.state_at(history.FIRE, at(45))
    assert dict(zip(at45["entity_key"], at45["area_ha"])) == {"a": 50, "b": 20}
    assert set(history.state_at(history.FIRE, at(60))["entity_key"]) == {"a"}


def test_sources_do_not_leak_into_each_other(db):
    history.record_state(history.FIRE, "x", {"v": 1}, effective_ts=T0)
    history.record_state(history.ROADS, "x", {"v": 2}, effective_ts=T0)
    assert history.state_at(history.FIRE, at(1)).iloc[0]["v"] == 1
    assert history.state_at(history.ROADS, at(1)).iloc[0]["v"] == 2


def test_state_keys_never_shadow_journal_columns(db):
    """A source storing its own 'active' or 'latitude' must not overwrite the
    journal's — replay trusts the journal columns."""
    history.record_state(history.ROADS, "r1",
                         {"active": "whatever", "latitude": 999},
                         effective_ts=T0, latitude=-37.8, longitude=144.9)
    row = history.state_at(history.ROADS, at(1)).iloc[0]
    assert int(row["active"]) == 1
    assert row["latitude"] == -37.8


# --------------------------------------------------------------------------- #
# Windows and change points
# --------------------------------------------------------------------------- #
def test_states_between_returns_only_the_window(fire_lifecycle):
    df = history.states_between(history.FIRE, at(10), at(80))
    assert len(df) == 2
    assert list(df["effective_ts"]) == [
        history._stamp(at(30)), history._stamp(at(70))]


def test_change_times_lists_the_moments_the_picture_moved(fire_lifecycle):
    times = history.change_times(history.FIRE, at(-60), at(600))
    assert times == [T0, at(30), at(70), at(330)]


# --------------------------------------------------------------------------- #
# History availability — never pretend
# --------------------------------------------------------------------------- #
def test_recording_stamps_when_the_journal_started(db):
    assert history.history_start(history.FIRE) is None
    history.record_batch(history.FIRE,
                         [{"entity_key": "f1", "state": fire_state()}],
                         effective_ts=T0)
    assert history.history_start(history.FIRE) is not None


def test_the_start_point_never_drifts_later(db):
    """Written once. If it moved with each cycle, Replay's honesty banner would
    keep shrinking the window it claims to cover."""
    history.note_history_start(history.FIRE, when=T0)
    history.note_history_start(history.FIRE, when=at(500))
    assert history.history_start(history.FIRE) == T0


def test_combined_history_start_is_the_latest_source(db):
    """The picture is only complete once EVERY layer was journalling."""
    history.note_history_start(history.FIRE, when=T0)
    history.note_history_start(history.ROADS, when=at(120))
    assert history.history_start() == at(120)


def test_journal_summary_reports_coverage(fire_lifecycle):
    summary = {s["source"]: s for s in history.journal_summary()}
    assert summary[history.FIRE]["rows"] == 4
    assert summary[history.FIRE]["entities"] == 1
    assert summary[history.FIRE]["first_ts"] == T0


# --------------------------------------------------------------------------- #
# Timestamp handling
# --------------------------------------------------------------------------- #
def test_string_and_datetime_timestamps_are_equivalent(db):
    history.record_state(history.FIRE, "f1", fire_state(), effective_ts=T0)
    from_dt = history.state_at(history.FIRE, at(5))
    from_str = history.state_at(history.FIRE, "2026-08-11 12:05:00")
    assert len(from_dt) == len(from_str) == 1


def test_timestamps_are_stored_in_the_apps_local_format(db):
    """The whole app stores naive Melbourne local time and compares timestamps
    as strings; an ISO-T or UTC stamp here would break every BETWEEN."""
    history.record_state(history.FIRE, "f1", fire_state(), effective_ts=T0)
    row = history.entity_history(history.FIRE, "f1").iloc[0]
    assert row["effective_ts"] == "2026-08-11 12:00:00"
    assert "T" not in row["effective_ts"]


def test_batch_records_per_entity_source_times(db):
    history.record_batch(history.ROADS, [
        {"entity_key": "r1", "state": {"status": "Closed"},
         "effective_ts": T0},
        {"entity_key": "r2", "state": {"status": "Open"},
         "effective_ts": at(45)},
    ])
    assert len(history.state_at(history.ROADS, at(10))) == 1
    assert len(history.state_at(history.ROADS, at(50))) == 2
