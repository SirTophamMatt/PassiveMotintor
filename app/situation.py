"""Statewide situation counts shared by the Console layout's status strip, the
console Overview and the wall display (UI-free, like `briefing`/`replay`).

Every open browser polls the strip on the shell's 20 s tick, and the wall is
meant to be left running on a screen all day, so the model is computed at most
once per `CACHE_SECONDS` for ALL viewers rather than once per viewer per tick.
That matters: the gauge breakdown groups the whole of `flood_observations`
(~1.4M rows in production) and must not become a per-request cost — the same
lesson as the 2026-08-09 projection-cycle postmortem.

Warning levels are kept as separate chips and never summed, for the same reason
as the Overview cards and the briefing: an Emergency Warning and an Advice ask
for different actions.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

CACHE_SECONDS = 45

_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}


@dataclass
class Chip:
    """One figure on the status strip / wall. `value` is None when the source
    could not be read, which renders as "—" — *we could not look* is a
    different answer from *nothing is happening*."""
    key: str
    label: str
    value: int = None
    href: str = "/"
    # emergency | watch | advice | alert — applied only while value > 0, so a
    # quiet statewide picture reads as quiet rather than as a wall of colour.
    tone: str = None

    @property
    def display(self):
        if self.value is None:
            return "—"
        return f"{int(self.value):,}"

    @property
    def active_tone(self):
        return self.tone if self.value else None


@dataclass
class Situation:
    generated_at: datetime
    chips: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # briefing.SourceStatus

    def chip(self, key):
        return next((c for c in self.chips if c.key == key), None)

    @property
    def stale_sources(self):
        return [s for s in self.sources if not s.is_healthy]

    @property
    def sources_label(self):
        total = len(self.sources)
        if not total:
            return "Sources unknown"
        healthy = total - len(self.stale_sources)
        return f"{healthy}/{total} sources live"


def _safe(fn, what):
    try:
        return fn()
    except Exception:
        log.exception("Situation: %s unavailable", what)
        return None


def _counts():
    """Raw figures, each isolated so one broken module shows "—" alone."""
    from app.modules.fire import data as fire_data
    from app.modules.flood import data as flood_data
    from app.modules.power import data as power_data
    from app.modules.roads import data as roads_data
    from app.modules.storm import data as storm_data
    from app.modules.weather import data as weather_data

    fire = _safe(fire_data.latest_counts, "fire counts") or {}
    flood = _safe(flood_data.flooding_breakdown, "flood breakdown") or {}
    power = _safe(power_data.latest_totals, "power totals") or {}
    roads = _safe(roads_data.latest_counts, "road counts") or {}
    storm = _safe(storm_data.latest_counts, "storm counts") or {}
    bom = _safe(weather_data.warning_counts, "BoM warning counts") or {}
    return {
        "emergency": fire.get("emergency"),
        "watch_act": fire.get("watch_act"),
        "advice": fire.get("advice"),
        "fires": fire.get("active_fires"),
        "gauges_minor": flood.get("minor"),
        "gauges_major": flood.get("major"),
        "bom": bom.get("total"),
        "customers_off": power.get("customers_off"),
        "road_closures": roads.get("closures"),
        "storm_strong": storm.get("strong"),
    }


def _int_or_none(value):
    try:
        if value is None or value != value:   # NaN from pandas
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def build_chips(counts):
    """Counts -> ordered chips. Pure, so the ordering/tone rules are testable
    without a database."""
    c = {k: _int_or_none(v) for k, v in counts.items()}
    return [
        Chip("emergency", "Emergency Warning", c.get("emergency"), "/fire", "emergency"),
        Chip("watch_act", "Watch & Act", c.get("watch_act"), "/fire", "watch"),
        Chip("advice", "Advice", c.get("advice"), "/fire", "advice"),
        Chip("fires", "Fires", c.get("fires"), "/fire", "alert"),
        Chip("gauges_minor", "Gauges ≥ Minor", c.get("gauges_minor"), "/flood", "watch"),
        Chip("bom", "BoM warnings", c.get("bom"), "/weather", None),
        Chip("customers_off", "Customers off", c.get("customers_off"), "/power", None),
        Chip("road_closures", "Road closures", c.get("road_closures"), "/roads", None),
        Chip("storm_strong", "Strong storm cells", c.get("storm_strong"), "/storm", "alert"),
    ]


def _sources(now):
    from app import briefing
    from app.config import load_config
    return briefing._sources(load_config(), now)


def compute(now=None):
    now = now or datetime.now()
    return Situation(generated_at=now,
                     chips=build_chips(_counts()),
                     sources=_safe(lambda: _sources(now), "source freshness") or [])


def current(max_age=CACHE_SECONDS):
    """The cached situation, recomputed when older than `max_age` seconds.
    The lock means a burst of viewers on a cold cache computes it once."""
    with _lock:
        fresh = (_cache["value"] is not None
                 and time.monotonic() - _cache["at"] < max_age)
        if not fresh:
            _cache["value"] = compute()
            _cache["at"] = time.monotonic()
        return _cache["value"]


def reset_cache():
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None
