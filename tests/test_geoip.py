"""Visitor geolocation: truncation, caching, and the never-block guarantee."""
import datetime

import pytest

from app import analytics, database, geoip


@pytest.fixture
def cfg():
    return {"geo": {"enabled": True, "timeout_seconds": 5,
                    "provider_url": "http://example.test/json/{ip}"}}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --------------------------------------------------------------- truncation --
@pytest.mark.parametrize("raw,expected", [
    ("203.0.113.47", "203.0.113.0"),
    ("203.0.113.255", "203.0.113.0"),
    ("8.8.8.8", "8.8.8.0"),
    ("  1.2.3.4  ", "1.2.3.0"),
    ("2001:db8:abcd:1234:5678::1", "2001:db8:abcd::"),
])
def test_truncate_removes_the_host_part(raw, expected):
    assert geoip.truncate(raw) == expected


@pytest.mark.parametrize("junk", [
    "", None, "not-an-address", "999.1.1.1", "1.2.3.4; DROP TABLE page_views",
    "<script>alert(1)</script>",
])
def test_truncate_rejects_anything_that_is_not_an_address(junk):
    """X-Forwarded-For is attacker-controlled, so this is the boundary that
    stops arbitrary text reaching the database or a provider URL."""
    assert geoip.truncate(junk) == ""


def test_truncation_is_lossy_by_design():
    """Two hosts on one /24 must be indistinguishable after truncation —
    that is the whole privacy property."""
    assert geoip.truncate("203.0.113.7") == geoip.truncate("203.0.113.208")


@pytest.mark.parametrize("addr,public", [
    ("81.2.69.0", True),
    ("8.8.8.0", True),
    ("192.168.1.0", False),
    ("10.0.0.0", False),
    ("127.0.0.0", False),
    ("172.16.0.0", False),
    ("169.254.0.0", False),
    # RFC 5737 documentation range. Fine for illustrating truncation, but
    # Python calls it reserved, so it is never looked up -- which is why the
    # resolution tests below use a genuinely routable address instead.
    ("203.0.113.0", False),
    ("garbage", False),
])
def test_is_public(addr, public):
    assert geoip.is_public(addr) is public


# ------------------------------------------------------------------ lookup --
def test_lookup_maps_and_caches_the_provider_response(db, cfg, monkeypatch):
    monkeypatch.setattr(geoip.requests, "get", lambda url, timeout: FakeResponse({
        "status": "success", "country": "Australia", "countryCode": "AU",
        "regionName": "Victoria", "city": "Melbourne", "lat": -37.81,
        "lon": 144.96, "org": "Example Telecom"}))

    fields = geoip.lookup("203.0.113.0", cfg=cfg)
    assert fields["city"] == "Melbourne"
    assert fields["region"] == "Victoria"

    row = geoip.cached("203.0.113.0")
    assert row["status"] == "ok"
    assert row["country_code"] == "AU"
    assert row["latitude"] == pytest.approx(-37.81)


def test_lookup_sends_only_the_truncated_address(db, cfg, monkeypatch):
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return FakeResponse({"status": "success", "country": "Australia"})

    monkeypatch.setattr(geoip.requests, "get", fake_get)
    geoip.lookup(geoip.truncate("203.0.113.47"), cfg=cfg)
    assert "203.0.113.0" in seen["url"]
    assert "203.0.113.47" not in seen["url"]


def test_provider_miss_is_recorded_as_failed(db, cfg, monkeypatch):
    """ip-api.com reports a miss in the body with HTTP 200, so an
    error-code-only check would cache the miss as a success."""
    monkeypatch.setattr(geoip.requests, "get", lambda url, timeout: FakeResponse(
        {"status": "fail", "message": "reserved range"}))
    assert geoip.lookup("203.0.113.0", cfg=cfg) is None
    assert geoip.cached("203.0.113.0")["status"] == "failed"


def test_network_error_is_recorded_not_raised(db, cfg, monkeypatch):
    def boom(url, timeout):
        raise geoip.requests.RequestException("connection refused")

    monkeypatch.setattr(geoip.requests, "get", boom)
    assert geoip.lookup("203.0.113.0", cfg=cfg) is None
    assert geoip.cached("203.0.113.0")["status"] == "failed"


def test_failed_lookups_expire_but_successes_do_not(db, cfg, monkeypatch):
    monkeypatch.setattr(geoip.requests, "get", lambda url, timeout: FakeResponse(
        {"status": "fail"}))
    geoip.lookup("203.0.113.0", cfg=cfg)

    row = geoip.cached("203.0.113.0")
    assert geoip._is_fresh(row) is True  # just failed; not retried yet

    stale = dict(row, looked_up_at=(
        datetime.datetime.now()
        - datetime.timedelta(hours=geoip.RETRY_AFTER_HOURS + 1)
    ).isoformat(sep=" ", timespec="seconds"))
    assert geoip._is_fresh(stale) is False

    old_success = dict(stale, status="ok")
    assert geoip._is_fresh(old_success) is True


# --------------------------------------------------------------- resolution --
def test_resolve_async_never_calls_the_provider_inline(db, cfg, monkeypatch):
    """The render path must not contain an HTTP request. A cache miss queues
    work; it does not wait for it."""
    def explode(*a, **k):
        raise AssertionError("resolve_async made a blocking provider call")

    monkeypatch.setattr(geoip.requests, "get", explode)
    monkeypatch.setattr(geoip, "_ensure_worker", lambda: None)
    assert geoip.resolve_async("81.2.69.0", cfg=cfg) == {}
    # Queued for the worker, not silently discarded.
    assert geoip._queue.get_nowait() == "81.2.69.0"


def test_resolve_async_returns_a_cached_location(db, cfg, monkeypatch):
    monkeypatch.setattr(geoip.requests, "get", lambda url, timeout: FakeResponse({
        "status": "success", "country": "Australia", "countryCode": "AU",
        "regionName": "Victoria", "city": "Geelong"}))
    geoip.lookup("81.2.69.0", cfg=cfg)

    monkeypatch.setattr(geoip, "_ensure_worker", lambda: None)
    assert geoip.resolve_async("81.2.69.0", cfg=cfg)["city"] == "Geelong"


def test_private_addresses_are_cached_not_looked_up(db, cfg, monkeypatch):
    monkeypatch.setattr(geoip, "_ensure_worker", lambda: None)
    assert geoip.resolve_async("192.168.1.0", cfg=cfg) == {}
    # Remembered, so a LAN-only deployment stops re-asking on every view.
    assert geoip.cached("192.168.1.0")["status"] == "private"


def test_disabling_geolocation_resolves_nothing(db, monkeypatch):
    monkeypatch.setattr(geoip, "_ensure_worker", lambda: None)
    off = {"geo": {"enabled": False}}
    assert geoip.resolve_async("81.2.69.0", cfg=off) == {}
    assert geoip.cached("81.2.69.0") is None


def test_missing_provider_url_fails_cleanly(db):
    assert geoip.lookup("203.0.113.0", cfg={"geo": {"provider_url": ""}}) is None
    assert geoip.cached("203.0.113.0")["status"] == "failed"


# ------------------------------------------------------- analytics wiring --
def _view(monkeypatch, ip, ua="Mozilla/5.0", path="/flood"):
    """Record one page view as if it arrived from `ip`."""
    monkeypatch.setattr(analytics, "client_ip", lambda: ip)
    monkeypatch.setattr(analytics.flask, "request",
                        type("R", (), {"headers": {"User-Agent": ua}})())
    analytics.record_view(path)


def test_page_views_store_the_truncated_address_only(db, monkeypatch):
    monkeypatch.setattr(geoip, "resolve_async", lambda prefix, cfg=None: {})
    _view(monkeypatch, "203.0.113.47")

    rows = database.read_df("SELECT ip_prefix FROM page_views")
    assert rows.iloc[0]["ip_prefix"] == "203.0.113.0"
    # The full address must appear nowhere in the table.
    dump = database.read_df("SELECT * FROM page_views").to_string()
    assert "203.0.113.47" not in dump


def test_page_views_carry_a_cached_location(db, monkeypatch):
    monkeypatch.setattr(geoip, "resolve_async", lambda prefix, cfg=None: {
        "country": "Australia", "country_code": "AU", "region": "Victoria",
        "city": "Ballarat"})
    _view(monkeypatch, "203.0.113.47")

    row = database.read_df("SELECT * FROM page_views").iloc[0]
    assert row["city"] == "Ballarat"
    assert row["country"] == "Australia"


def test_a_geolocation_failure_never_breaks_navigation(db, monkeypatch):
    def boom(prefix, cfg=None):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(geoip, "resolve_async", boom)
    _view(monkeypatch, "203.0.113.47")  # must not raise
    # The view is lost rather than the page — analytics is never load-bearing.
    assert database.read_df("SELECT * FROM page_views").empty


def test_location_aggregates(db, monkeypatch):
    monkeypatch.setattr(geoip, "resolve_async", lambda prefix, cfg=None: {
        "country": "Australia", "region": "Victoria", "city": "Bendigo"})
    _view(monkeypatch, "203.0.113.47")
    _view(monkeypatch, "203.0.113.48", ua="Other/1.0")

    countries = analytics.by_country(days=30)
    assert countries.iloc[0]["country"] == "Australia"
    assert int(countries.iloc[0]["views"]) == 2

    cities = analytics.by_city(days=30)
    assert cities.iloc[0]["city"] == "Bendigo"

    cov = analytics.location_coverage(days=30)
    assert cov["total"] == 2 and cov["located"] == 2


def test_unresolved_views_are_reported_not_dropped(db, monkeypatch):
    """A chart that silently omits unlocated traffic overstates how much of the
    audience it actually accounts for."""
    monkeypatch.setattr(geoip, "resolve_async", lambda prefix, cfg=None: {})
    _view(monkeypatch, "203.0.113.47")

    assert analytics.by_country(days=30).iloc[0]["country"] == "Unknown"
    assert analytics.location_coverage(days=30)["located"] == 0
