# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Light or dark basemap for the wall displays.

Each wall has a "Map" option (per browser, like its other options):

* **Light** — OpenStreetMap tiles, what every map here has always used.
* **Dark** — CARTO Dark Matter, token-free like OSM (the replay player
  already offers it).
* **Follow the page theme** — dark map while the ☾ theme is on.
* **Melbourne sunrise / sunset** — dark from sunset to sunrise, worked out
  for Melbourne's own date, so a wall screen dims for the night shift by
  itself. The walls re-draw their map on their normal refresh, so the switch
  lands within a refresh of the moment.

Sun times use the NOAA solar-position equations (official sunrise: the
sun's upper limb on the horizon with standard refraction, zenith 90.833°),
good to about a minute — plenty for choosing map tiles, and no dependency.
"""
import math
from datetime import datetime, time, timedelta, timezone

LIGHT_STYLE = "open-street-map"
DARK_STYLE = "carto-darkmatter"

LIGHT, DARK, THEME, SUN = "light", "dark", "theme", "sun"
DEFAULT_MODE = LIGHT

MELBOURNE = (-37.8136, 144.9631)

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Australia/Melbourne")
except Exception:          # no tz database (bare Windows): server-local time
    _TZ = None


def melbourne_now():
    if _TZ is not None:
        return datetime.now(_TZ)
    return datetime.now().astimezone()


def _sun_utc(day, lat, lon):
    """(sunrise, sunset) for `day` at lat/lon as aware UTC datetimes."""
    # Julian century at local solar noon of the day.
    jd = day.toordinal() + 1721424.5 + 0.5 - lon / 360.0
    t = (jd - 2451545.0) / 36525.0
    rad, deg = math.radians, math.degrees

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(rad(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(rad(2 * m)) * (0.019993 - 0.000101 * t)
         + math.sin(rad(3 * m)) * 0.000289)
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(rad(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(rad(omega))
    decl = math.asin(math.sin(rad(eps)) * math.sin(rad(lam)))

    y = math.tan(rad(eps) / 2) ** 2
    eq_time = 4 * deg(
        y * math.sin(2 * rad(l0))
        - 2 * e * math.sin(rad(m))
        + 4 * e * y * math.sin(rad(m)) * math.cos(2 * rad(l0))
        - 0.5 * y * y * math.sin(4 * rad(l0))
        - 1.25 * e * e * math.sin(2 * rad(m)))           # minutes

    cos_ha = (math.cos(rad(90.833)) / (math.cos(rad(lat)) * math.cos(decl))
              - math.tan(rad(lat)) * math.tan(decl))
    ha = deg(math.acos(max(-1.0, min(1.0, cos_ha))))
    noon = 720 - 4 * lon - eq_time                         # minutes after 00:00 UTC
    base = datetime.combine(day, time(0), tzinfo=timezone.utc)
    return (base + timedelta(minutes=noon - 4 * ha),
            base + timedelta(minutes=noon + 4 * ha))


def sun_times(day=None, lat=MELBOURNE[0], lon=MELBOURNE[1]):
    """Melbourne-local (sunrise, sunset) for a Melbourne date (default today)."""
    now = melbourne_now()
    day = day or now.date()
    tz = now.tzinfo
    rise, sset = _sun_utc(day, lat, lon)
    return rise.astimezone(tz), sset.astimezone(tz)


def is_night(now=None):
    """True between Melbourne sunset and sunrise."""
    tz = melbourne_now().tzinfo
    now = now or datetime.now(tz)
    if now.tzinfo is None:             # naive = Melbourne wall time
        now = now.replace(tzinfo=tz)
    local = now.astimezone(tz)
    rise, sset = sun_times(local.date())
    return not (rise <= local < sset)


def is_dark(mode, theme_dark, now=None):
    """Whether the map should be dark for this mode right now."""
    if mode == DARK:
        return True
    if mode == THEME:
        return bool(theme_dark)
    if mode == SUN:
        return is_night(now)
    return False


def style(mode, theme_dark, now=None):
    return DARK_STYLE if is_dark(mode, theme_dark, now) else LIGHT_STYLE


def options():
    """RadioItems options; the sunrise/sunset row quotes today's times."""
    try:
        rise, sset = sun_times()
        sun = (f" Melbourne sunrise / sunset (today {rise:%H:%M} / "
               f"{sset:%H:%M})")
    except Exception:
        sun = " Melbourne sunrise / sunset"
    return [{"label": " Light", "value": LIGHT},
            {"label": " Dark", "value": DARK},
            {"label": " Follow the page theme", "value": THEME},
            {"label": sun, "value": SUN}]
