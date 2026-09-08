#!/usr/bin/env python3
"""
Flight radar web page -- host process.

Serves the ambient-mode page in static/ plus the JSON API that page polls.
Standard library only, deliberately: the target is a Raspberry Pi 3B running
whatever Python 3 its distribution shipped, and building wheels on a 900 MB
single-board computer is a place where installs go to die.

Upstream data is gathered by background pollers rather than on request. Two
reasons. Every browser refresh is then answered out of memory, and the number
of requests we make to the free aggregators stays flat no matter how many
screens end up pointed at this box -- airplanes.live rate limits to one
request per second, and a kiosk rotation reloads a page hard and often.
"""

import gzip
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from flightradar import alerts, enrich, geo, net, overlays         # noqa: E402

STATIC_DIR = os.path.join(HERE, "static")
USER_AGENT = "esp32-flight-radar-web/1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,

    # Where the radar is standing.
    "lat": 51.4700,
    "lon": -0.4543,

    # Scope radius in nautical miles: both the query radius and the ring drawn
    # on the map.
    "range_nm": 20,

    "poll_interval_s": 5,
    "stale_after_s": 60,
    "max_aircraft": 150,

    # How many of the nearest aircraft get a callsign/route label. The rest
    # stay as bare silhouettes so the picture does not turn into soup.
    "label_count": 10,

    "units": "aviation",              # aviation (ft/kt/nm) | metric (m/kmh/km)
    "temperature_unit": "celsius",    # celsius | fahrenheit

    # IANA name, e.g. "Europe/London". null uses the browser's own
    # clock, which is the right answer whenever the screen showing this page
    # is on the same network as the radar.
    "timezone": None,

    "show_weather": True,
    "show_precipitation": True,
    "precipitation_opacity": 0.5,

    # Basemap. Anything in {z}/{x}/{y} form works; CARTO dark is what the
    # page was designed against.
    "basemap_url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",

    "route_lookup": True,

    # Aircraft details and photos from adsbdb, and airline logos. Separate
    # from route_lookup because they are a different request budget against
    # the same free service.
    "aircraft_lookup": True,
    "show_photos": True,
    "show_logos": True,

    # Local ADS-B receiver (readsb on this Pi). When present, aircraft
    # this antenna actually hears get a "HEARD HERE" badge and a
    # reception readout (signal, message count) in the detail card.
    # This is the one thing the upstream aggregators cannot provide.
    "show_radio": True,
    "radio_json_path": "/run/readsb/aircraft.json",
    "radio_max_age_s": 60,

    # Positions kept per aircraft for the trail behind it. 12 matches the
    # firmware; 0 turns trails off.
    "trail_points": 12,

    # Optional overlays. The first two need nothing; the last two need a free
    # key from the service and stay completely dormant without one -- their
    # poller threads are never even started, so an overlay nobody has keyed
    # costs nothing.
    "show_iss": True,
    "show_sondes": True,
    "show_ships": True,          # needs aisstream_key
    "show_airspace": True,       # needs openaip_key
    "sonde_range_nm": 135,       # 250 km, as the firmware uses

    "aisstream_key": "",
    "openaip_key": "",

    # Watchlist: comma-separated callsign or registration prefixes, e.g.
    # "RYR, N12, SP-". Matches are highlighted gold and alerted.
    "watchlist": "",
    "alert_emergency": True,
    "alert_watchlist": True,
    "alert_military": True,
    "alert_notable": True,

    # Where alerts go, if anywhere. All optional and independent.
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "webhook_url": "",
    "mqtt_uri": "",              # mqtt://user:pass@host:1883 or mqtts://
    "mqtt_topic": "flightradar/alert",

    # Optional. When set, the panel and the endpoints that expose settings or
    # history ask for it first. The radar page itself is never gated -- it is
    # the thing on the wall.
    "panel_password": "",

    "cache_dir": HERE,
}

# Settings the panel is allowed to write. Everything else -- host, cache_dir --
# is deployment, not preference, and letting a browser change it is how a
# display talks itself out of starting.
EDITABLE_KEYS = (
    "lat", "lon", "range_nm", "label_count", "units", "temperature_unit",
    "timezone", "poll_interval_s", "max_aircraft", "trail_points",
    "show_weather", "show_precipitation", "precipitation_opacity",
    "basemap_url", "route_lookup", "aircraft_lookup", "show_photos",
    "show_logos", "show_iss", "show_sondes", "show_ships", "show_airspace",
    "sonde_range_nm", "aisstream_key", "openaip_key", "watchlist",
    "alert_emergency", "alert_watchlist", "alert_military", "alert_notable",
    "ntfy_server", "ntfy_topic", "webhook_url", "mqtt_uri", "mqtt_topic",
    "panel_password",
)

# Never sent back to the browser. The panel is told only whether each one is
# set, and an empty submission means "leave it alone".
#
# The first attempt sent a row of bullet characters as a placeholder and
# treated that exact string as "unchanged". It survived a browser fine and was
# mangled by everything else -- a test client re-encoded the bullets, the
# comparison failed, and the stored key was overwritten with the mojibake.
# A secret that can be destroyed by an encoding difference is the wrong
# design, so nothing is round-tripped now.
SECRET_KEYS = ("aisstream_key", "openaip_key", "panel_password")

# The one way to deliberately empty a secret, since blank means unchanged.
CLEAR_SECRET = "CLEAR"

# Changing these starts, stops or repoints a background thread, which only
# happens at startup. The panel says so rather than silently doing nothing.
RESTART_KEYS = ("port", "show_ships", "show_airspace", "aisstream_key",
                "openaip_key", "poll_interval_s", "sonde_range_nm")

# Panel sessions, in memory only: a restart logs everyone out, which for a
# thing on a home network is a feature rather than a defect.
SESSIONS = set()

# Config keys that are booleans, so a "--show-photos false" or an environment
# override does not turn into the string "false", which is truthy.
BOOL_KEYS = ("show_weather", "show_precipitation", "route_lookup",
             "aircraft_lookup", "show_photos", "show_logos",
             "show_iss", "show_sondes", "show_ships", "show_airspace",
             "alert_emergency", "alert_watchlist", "alert_military",
             "alert_notable")
INT_KEYS = ("port", "range_nm", "poll_interval_s", "stale_after_s",
            "max_aircraft", "label_count", "trail_points", "sonde_range_nm")
FLOAT_KEYS = ("lat", "lon", "precipitation_opacity")


def log(msg):
    sys.stderr.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    cfg = dict(DEFAULTS)
    path = os.environ.get("FLIGHT_RADAR_CONFIG", os.path.join(HERE, "config.json"))
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
            log("config: loaded %s" % path)
        except Exception as exc:
            log("config: %s is unreadable (%s), using defaults" % (path, exc))
    else:
        log("config: no %s, using defaults -- the map will sit over Heathrow" % path)

    # Environment and argv win, so a systemd drop-in or a quick test run can
    # override without editing the file.
    for key in list(DEFAULTS):
        env = os.environ.get("FLIGHT_RADAR_" + key.upper())
        if env:
            cfg[key] = env
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--") and "=" in arg:
            key, _, val = arg[2:].partition("=")
            cfg[key.replace("-", "_")] = val
        elif arg.startswith("--") and i + 1 < len(argv):
            cfg[arg[2:].replace("-", "_")] = argv[i + 1]
            i += 1
        i += 1

    for key in INT_KEYS:
        cfg[key] = int(float(cfg[key]))
    for key in FLOAT_KEYS:
        cfg[key] = float(cfg[key])
    for key in BOOL_KEYS:
        cfg[key] = _as_bool(cfg[key])
    return cfg


# ---------------------------------------------------------------------------
# Geometry -- the same formulae as main/geo.c, so the scope and the page agree
# ---------------------------------------------------------------------------

distance_nm = geo.distance_nm
bearing_deg = geo.bearing_deg


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

# One implementation, in flightradar/net.py, because it has to handle servers
# that gzip whether or not you asked -- see the note there.
fetch_json = net.fetch_json


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.aircraft = []
        self.source = None
        self.last_ok = 0.0
        self.failures = 0
        self.weather = None
        self.weather_at = 0.0
        self.radar = None
        self.radar_at = 0.0
        # hex -> {"points": [[lat, lon], ...], "seen": epoch}
        self.trails = {}
        self.iss = None
        self.sondes = []
        self.airspace = []
        # hex -> {"rssi": dBFS, "messages": int, "seen": s} from the
        # local readsb receiver, when one is running on this Pi.
        self.radio = {}
        self.radio_at = 0.0


STATE = State()
CFG = {}
ROUTES = None
AIRCRAFT = None
TIMEZONES = None
MEDIA = None
SHIPS = None
ALERTER = None
ALERT_LOG = None
SPOT_LOG = None
STARTED_AT = time.time()
SHUTDOWN = threading.Event()


# ---------------------------------------------------------------------------
# Aircraft poller
# ---------------------------------------------------------------------------

# Tried in this order on every poll rather than sticking with whichever
# answered last. Coverage differs by region, and adsb.lol answers 200 with an
# empty list over the western US instead of erroring -- so pinning to the last
# source that succeeded can quietly strand the display on the worst feed for
# where it happens to be standing. adsb.fi sits ahead of adsb.lol for the same
# reason. This mirrors main/adsb.c.
SOURCES = [
    ("airplanes.live",
     "https://api.airplanes.live/v2/point/%(lat).4f/%(lon).4f/%(range)d"),
    ("adsb.fi",
     "https://opendata.adsb.fi/api/v2/lat/%(lat).4f/lon/%(lon).4f/dist/%(range)d"),
    ("adsb.lol",
     "https://api.adsb.lol/v2/point/%(lat).4f/%(lon).4f/%(range)d"),
]

EMERGENCY_SQUAWKS = ("7500", "7600", "7700")

# Below this the aircraft has not really moved and a trail point would only
# add jitter to the line.
TRAIL_MIN_STEP_NM = 0.05
TRAIL_FORGET_S = 300


def _num(value, fallback=0.0):
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def parse_aircraft(node, home_lat, home_lon):
    lat = node.get("lat")
    lon = node.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None

    # alt_baro is the string "ground" for aircraft on the surface and a number
    # otherwise; alt_geom covers the case where the barometric altitude is
    # missing entirely.
    alt_raw = node.get("alt_baro")
    on_ground = alt_raw == "ground"
    alt_ft = 0.0 if on_ground else _num(alt_raw, _num(node.get("alt_geom")))

    squawk = _text(node.get("squawk"))

    dst = node.get("dst")
    dirn = node.get("dir")
    if isinstance(dst, (int, float)) and isinstance(dirn, (int, float)):
        # The point endpoints hand these back already computed against the
        # query origin; only do the trigonometry when they are absent.
        dist, brg = float(dst), float(dirn)
    else:
        dist = distance_nm(home_lat, home_lon, lat, lon)
        brg = bearing_deg(home_lat, home_lon, lat, lon)

    return {
        "hex": _text(node.get("hex")),
        "flight": _text(node.get("flight")),
        "reg": _text(node.get("r")),
        "type": _text(node.get("t")),
        "desc": _text(node.get("desc")),
        "squawk": squawk,
        "category": _text(node.get("category")),
        "lat": float(lat),
        "lon": float(lon),
        "alt_ft": alt_ft,
        "gs_kt": _num(node.get("gs")),
        "track_deg": _num(node.get("track"), _num(node.get("true_heading"))),
        "baro_rate": _num(node.get("baro_rate"), _num(node.get("geom_rate"))),
        "dist_nm": dist,
        "bearing_deg": brg,
        "on_ground": on_ground,
        "military": bool(int(_num(node.get("dbFlags"))) & 1),
        "emergency": squawk in EMERGENCY_SQUAWKS,
        # Seconds since this position was last updated, per the feed. The page
        # dead-reckons forward from it so targets glide instead of jumping
        # once per poll.
        "pos_age_s": _num(node.get("seen_pos"), _num(node.get("seen"))),
    }


def fetch_source(fmt):
    url = fmt % {"lat": CFG["lat"], "lon": CFG["lon"], "range": CFG["range_nm"]}
    doc = fetch_json(url, timeout=10)

    # airplanes.live and adsb.lol use "ac"; adsb.fi uses "aircraft".
    nodes = doc.get("ac")
    if not isinstance(nodes, list):
        nodes = doc.get("aircraft")
    if not isinstance(nodes, list):
        raise ValueError("no aircraft array in response")

    home_lat, home_lon = CFG["lat"], CFG["lon"]
    out = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        parsed = parse_aircraft(node, home_lat, home_lon)
        if parsed is not None:
            out.append(parsed)

    out.sort(key=lambda a: a["dist_nm"])
    return out[:CFG["max_aircraft"]]


def update_trails(aircraft, now):
    """Extend each aircraft's trail, and forget the ones that have gone."""
    keep = CFG["trail_points"]
    trails = STATE.trails
    if keep <= 0:
        trails.clear()
        return

    for ac in aircraft:
        key = ac["hex"]
        if not key:
            continue
        entry = trails.get(key)
        point = [round(ac["lat"], 5), round(ac["lon"], 5)]
        if entry is None:
            trails[key] = {"points": [point], "seen": now}
            continue
        last = entry["points"][-1]
        if distance_nm(last[0], last[1], point[0], point[1]) >= TRAIL_MIN_STEP_NM:
            entry["points"].append(point)
            del entry["points"][:-keep]
        entry["seen"] = now

    for key in [k for k, v in trails.items() if now - v["seen"] > TRAIL_FORGET_S]:
        del trails[key]


def aircraft_poller():
    while not SHUTDOWN.is_set():
        started = time.time()
        got = None
        for name, fmt in SOURCES:
            try:
                got = fetch_source(fmt)
            except Exception as exc:
                log("adsb: %s failed (%s)" % (name, exc))
                continue
            with STATE.lock:
                STATE.aircraft = got
                STATE.source = name
                STATE.last_ok = time.time()
                STATE.failures = 0
                update_trails(got, STATE.last_ok)
            break

        if got is None:
            with STATE.lock:
                STATE.failures += 1
                failures = STATE.failures
            log("adsb: every source failed (%d in a row)" % failures)

        SHUTDOWN.wait(max(1.0, CFG["poll_interval_s"] - (time.time() - started)))


# ---------------------------------------------------------------------------
# Weather and precipitation
# ---------------------------------------------------------------------------

def weather_poller():
    while not SHUTDOWN.is_set():
        if CFG["show_weather"]:
            params = urllib.parse.urlencode({
                "latitude": "%.4f" % CFG["lat"],
                "longitude": "%.4f" % CFG["lon"],
                "current": "temperature_2m,weather_code,wind_speed_10m,"
                           "wind_direction_10m,is_day",
                "temperature_unit": CFG["temperature_unit"],
                "wind_speed_unit": "kn" if CFG["units"] == "aviation" else "kmh",
            })
            try:
                doc = fetch_json(
                    "https://api.open-meteo.com/v1/forecast?" + params, timeout=10)
                current = doc.get("current") or {}
                with STATE.lock:
                    STATE.weather = {
                        "temp": current.get("temperature_2m"),
                        "code": current.get("weather_code"),
                        "wind_speed": current.get("wind_speed_10m"),
                        "wind_dir": current.get("wind_direction_10m"),
                        "is_day": bool(current.get("is_day", 1)),
                        "temp_unit": "F" if CFG["temperature_unit"] == "fahrenheit" else "C",
                        "wind_unit": "kt" if CFG["units"] == "aviation" else "km/h",
                    }
                    STATE.weather_at = time.time()
            except Exception as exc:
                log("weather: %s" % exc)
        SHUTDOWN.wait(600)


def radar_poller():
    """RainViewer publishes a new radar composite every ten minutes."""
    while not SHUTDOWN.is_set():
        if CFG["show_precipitation"]:
            try:
                doc = fetch_json(
                    "https://api.rainviewer.com/public/weather-maps.json", timeout=10)
                past = (doc.get("radar") or {}).get("past") or []
                if past:
                    latest = past[-1]
                    with STATE.lock:
                        STATE.radar = {
                            "host": doc.get("host", "https://tilecache.rainviewer.com"),
                            "path": latest.get("path"),
                            "time": latest.get("time"),
                        }
                        STATE.radar_at = time.time()
            except Exception as exc:
                log("rainviewer: %s" % exc)
        SHUTDOWN.wait(300)


# ---------------------------------------------------------------------------
# Overlay pollers
# ---------------------------------------------------------------------------

def iss_poller():
    while not SHUTDOWN.is_set():
        try:
            found = overlays.fetch_iss(CFG["lat"], CFG["lon"])
            with STATE.lock:
                STATE.iss = found
        except Exception as exc:
            log("iss: %s" % exc)
        SHUTDOWN.wait(overlays.ISS_REFRESH_S)


def sonde_poller():
    while not SHUTDOWN.is_set():
        try:
            found = overlays.fetch_sondes(CFG["lat"], CFG["lon"],
                                          float(CFG["sonde_range_nm"]))
            with STATE.lock:
                STATE.sondes = found
        except Exception as exc:
            log("sondes: %s" % exc)
        SHUTDOWN.wait(overlays.SONDE_REFRESH_S)


def airspace_poller():
    """
    Airspace does not move. Fetched once, then only re-checked daily in case
    a chart cycle changed something under us.
    """
    while not SHUTDOWN.is_set():
        try:
            found = overlays.fetch_airspace(CFG["lat"], CFG["lon"],
                                            CFG["range_nm"], CFG["openaip_key"])
            with STATE.lock:
                STATE.airspace = found
            log("airspace: %d outlines" % len(found))
        except Exception as exc:
            log("airspace: %s" % exc)
        SHUTDOWN.wait(24 * 3600)


def ships_runner():
    SHIPS.run(CFG["aisstream_key"], CFG["lat"], CFG["lon"],
              max(CFG["range_nm"] * 2, 40), SHUTDOWN)


def alert_poller():
    """
    Decide alerts once per poll rather than once per browser request.

    It works from the same decorated picture the page is served, so a
    watchlist match, a military flag and a notable type mean exactly what they
    mean on screen.
    """
    SHUTDOWN.wait(8)                      # let the first poll and enrichment land
    while not SHUTDOWN.is_set():
        try:
            now = time.time()
            with STATE.lock:
                snapshot = [dict(ac) for ac in STATE.aircraft]
                trails = {k: list(v["points"]) for k, v in STATE.trails.items()}
            ALERTER.evaluate([decorate(ac, now, trails) for ac in snapshot], now)
        except Exception as exc:
            log("alerts: %s" % exc)
        SHUTDOWN.wait(max(5, CFG["poll_interval_s"]))


def radio_poller():
    """Index the local readsb receiver's JSON by hex.

    readsb runs on this same Pi and writes every aircraft its antenna
    decodes to a JSON file. This is the one thing the upstream aggregators
    cannot give us: proof of local reception, with this receiver's own
    signal strength and message count. A local file read; never the network.
    """
    while not SHUTDOWN.is_set():
        radio = {}
        if CFG["show_radio"]:
            try:
                with open(CFG["radio_json_path"]) as fh:
                    doc = json.load(fh)
                for node in doc.get("aircraft", []):
                    hexcode = (node.get("hex") or "").lower().lstrip("~")
                    rssi = node.get("rssi")
                    # -49.5 is readsb's sentinel for "decoded, no signal yet".
                    if not hexcode or not isinstance(rssi, (int, float)) or rssi <= -49.4:
                        continue
                    radio[hexcode] = {
                        "rssi": float(rssi),
                        "messages": int(node.get("messages") or 0),
                        "seen": float(node.get("seen") or 0.0),
                    }
            except FileNotFoundError:
                pass  # readsb stopped or absent -- simply no local data.
            except Exception as exc:
                log("radio: could not read %s (%s)" % (CFG["radio_json_path"], exc))
        with STATE.lock:
            STATE.radio = radio
            STATE.radio_at = time.time()
        SHUTDOWN.wait(max(2.0, CFG["poll_interval_s"]))


# ---------------------------------------------------------------------------
# Enrichment poller
# ---------------------------------------------------------------------------
#
# One thread, one request at a time, nearest aircraft first. adsbdb is free and
# unauthenticated; there is no reason to hammer it when a scope full of traffic
# still resolves inside a minute and then stays cached for a day.

ADSBDB_GAP_S = 1.2


def _resolve(cache, key, describe):
    """Look one thing up, caching misses as answers. True if a request was made."""
    try:
        cache.put(key, cache.lookup(key))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            # 404: adsbdb has never heard of it. 400: it will not accept the
            # identifier at all. Both are answers, and caching them is the
            # whole point -- otherwise every unknown aircraft in the area is
            # re-asked forever, ten seconds apart.
            cache.put(key, None)
        else:
            log("%s: %s -> HTTP %s" % (describe, key, exc.code))
            SHUTDOWN.wait(10)
    except Exception as exc:
        log("%s: %s -> %s" % (describe, key, exc))
        SHUTDOWN.wait(10)
    return True


def enrichment_poller():
    last_save = time.time()
    while not SHUTDOWN.is_set():
        with STATE.lock:
            snapshot = [(ac["hex"], ac["flight"]) for ac in STATE.aircraft]

        worked = False

        # Routes first: they are what the page actually shows.
        if CFG["route_lookup"]:
            for _, callsign in snapshot:
                if (callsign and enrich.AIRLINE_CALLSIGN.match(callsign)
                        and not ROUTES.known(callsign)):
                    worked = _resolve(ROUTES, callsign, "routes")
                    break

        # Then the destination's clock, so arrival can be shown in local time.
        if not worked:
            for _, callsign in snapshot:
                route = ROUTES.get(callsign) if callsign else None
                dest = (route or {}).get("to") or {}
                if dest.get("lat") is None:
                    continue
                key = dest.get("icao") or ("%.2f,%.2f" % (dest["lat"], dest["lon"]))
                if TIMEZONES.known(key):
                    continue
                try:
                    TIMEZONES.put(key, TIMEZONES.lookup(dest["lat"], dest["lon"]))
                except Exception as exc:
                    log("timezone: %s -> %s" % (key, exc))
                    TIMEZONES.put(key, None)
                worked = True
                break

        # Then registrations, types and photos.
        if not worked and CFG["aircraft_lookup"]:
            for hexcode, _ in snapshot:
                # A leading tilde marks a TIS-B or ADS-R relay rather than a
                # real ICAO address. There is nothing to look up, and adsbdb
                # rejects it outright.
                if hexcode and not hexcode.startswith("~") and not AIRCRAFT.known(hexcode):
                    worked = _resolve(AIRCRAFT, hexcode, "aircraft")
                    break

        SHUTDOWN.wait(ADSBDB_GAP_S if worked else 5)

        if time.time() - last_save > 30:
            ROUTES.save()
            AIRCRAFT.save()
            TIMEZONES.save()
            last_save = time.time()


# ---------------------------------------------------------------------------
# Building the picture
# ---------------------------------------------------------------------------

# How far the trip through the aircraft's actual position may exceed the
# direct origin-to-destination distance before the route is disbelieved.
ROUTE_DETOUR_FACTOR = 1.25
ROUTE_DETOUR_SLACK_NM = 50.0


def build_route(route, ac, now):
    """
    Turn a cached route record into what the page shows, or reject it.

    Airlines reuse a callsign across the legs of a day and adsbdb answers with
    one of them, so a lookup can hand back a leg this aircraft is not flying.
    An aeroplane visibly overhead reported as Philadelphia to Chicago is the
    obvious case, and showing that route is worse than showing none.

    The test is geometric: if going origin -> here -> destination is far
    further than going origin -> destination, "here" is not on this route.
    Real routing wanders around weather and airways, but not by half.

    The airline survives rejection. It comes from the three-letter prefix of
    the callsign, which is correct whichever leg is being flown.
    """
    origin = route.get("from") or {}
    dest = route.get("to") or {}
    out = {
        "airline": route.get("airline") or {},
        "flight_iata": route.get("flight_iata") or "",
    }

    if origin.get("lat") is None or dest.get("lat") is None:
        # No coordinates to check against, so take it at face value -- this is
        # how the page behaved before any of the geometry existed.
        out["from"] = origin
        out["to"] = dest
        return out

    progress, remaining, total = geo.route_progress(
        origin["lat"], origin["lon"], dest["lat"], dest["lon"],
        ac["lat"], ac["lon"])
    direct = geo.distance_nm(origin["lat"], origin["lon"],
                             dest["lat"], dest["lon"])

    if total > direct * ROUTE_DETOUR_FACTOR + ROUTE_DETOUR_SLACK_NM:
        out["rejected"] = True
        return out

    out["from"] = origin
    out["to"] = dest
    out["progress"] = round(progress, 4)
    out["remaining_nm"] = round(remaining, 1)
    out["total_nm"] = round(total, 1)

    gs = ac.get("gs_kt") or 0.0
    if gs > 40.0 and remaining > 0.0:
        eta_min = remaining / gs * 60.0
        out["eta_min"] = round(eta_min, 1)
        out["arrival_epoch"] = now + eta_min * 60.0
        key = dest.get("icao") or ("%.2f,%.2f" % (dest["lat"], dest["lon"]))
        tz = TIMEZONES.get(key) if TIMEZONES else None
        if tz:
            out["arrival_offset_s"] = tz.get("offset_s")
            out["arrival_tz"] = tz.get("tz") or ""
    return out


def decorate(ac, now, trails, radio=None):
    """Attach everything the feed did not carry. Never touches the network."""
    hexcode = ac.get("hex") or ""
    callsign = ac.get("flight") or ""

    info = AIRCRAFT.get(hexcode) if (AIRCRAFT and hexcode) else None
    if info:
        ac["reg"] = ac.get("reg") or info.get("reg") or ""
        ac["type"] = ac.get("type") or info.get("type") or ""
        ac["type_long"] = info.get("type_long") or ""
        ac["manufacturer"] = info.get("manufacturer") or ""
        ac["owner"] = info.get("owner") or ""
        ac["has_photo"] = bool(info.get("photo") or info.get("photo_thumb"))
        ac["reg_country"] = info.get("country") or ""

    if not ac.get("reg_country"):
        ac["reg_country"] = enrich.registration_country(ac.get("reg"))

    route = ROUTES.get(callsign) if (ROUTES and callsign) else None
    if route:
        built = build_route(route, ac, now)
        ac["route"] = built
        if not built.get("rejected"):
            # Kept flat as well: the ambient labels have shown ORIGIN>DEST
            # since before any of the rest of this existed.
            origin = built.get("from") or {}
            dest = built.get("to") or {}
            ac["origin"] = origin.get("iata") or origin.get("icao") or ""
            ac["destination"] = dest.get("iata") or dest.get("icao") or ""

    # Where to look, and whether it is coming this way.
    elev = geo.elevation_deg(ac.get("alt_ft"), ac.get("dist_nm"))
    ac["spotter"] = {
        "az_deg": round(ac.get("bearing_deg") or 0.0, 1),
        "compass": geo.compass_point(ac.get("bearing_deg")),
        "elev_deg": round(elev, 1) if elev is not None else None,
    }

    cpa = geo.closest_approach(ac.get("dist_nm"), ac.get("bearing_deg"),
                               ac.get("track_deg"), ac.get("gs_kt"))
    if cpa is not None:
        minutes, miss = cpa
        if minutes <= 30.0:
            ac["cpa"] = {"minutes": round(minutes, 1), "miss_nm": round(miss, 2)}

    ac["notable"] = enrich.is_notable(ac.get("type"))
    ac["rotor"] = enrich.is_rotorcraft(ac.get("type"), ac.get("category"),
                                       ac.get("desc") or ac.get("type_long"))
    if ALERTER and alerts.matches_watchlist(ac, ALERTER.watchlist):
        ac["watch"] = True
    ac["interesting"] = bool(ac.get("military") or ac["notable"] or
                             ac.get("emergency") or ac.get("watch"))

    heard = (radio or {}).get(hexcode.lower()) if hexcode else None
    if heard and heard["seen"] <= CFG["radio_max_age_s"]:
        # This aircraft was received by our own antenna, not just the feed.
        ac["radio"] = {
            "rssi": round(heard["rssi"], 1),
            "messages": heard["messages"],
            "age_s": round(heard["seen"], 1),
        }

    trail = trails.get(hexcode)
    if trail and len(trail) > 1:
        ac["trail"] = trail
    return ac


def build_state():
    now = time.time()
    with STATE.lock:
        aircraft = [dict(ac) for ac in STATE.aircraft]
        source = STATE.source
        last_ok = STATE.last_ok
        failures = STATE.failures
        weather = dict(STATE.weather) if STATE.weather else None
        weather_at = STATE.weather_at
        radar = dict(STATE.radar) if STATE.radar else None
        # Copied inside the lock: the poller appends to these lists in place,
        # and several browsers can be serialising a response at once.
        trails = {k: list(v["points"]) for k, v in STATE.trails.items()}
        iss = dict(STATE.iss) if STATE.iss else None
        sondes = [dict(s) for s in STATE.sondes]
        airspace = [dict(a) for a in STATE.airspace]
        radio = dict(STATE.radio)

    aircraft = [decorate(ac, now, trails, radio) for ac in aircraft]

    if weather:
        weather["age_s"] = round(now - weather_at, 1)

    age = (now - last_ok) if last_ok else None
    return {
        "now": now,
        "config": {
            "lat": CFG["lat"],
            "lon": CFG["lon"],
            "range_nm": CFG["range_nm"],
            "units": CFG["units"],
            "timezone": CFG["timezone"],
            "label_count": CFG["label_count"],
            "poll_interval_s": CFG["poll_interval_s"],
            "basemap_url": CFG["basemap_url"],
            "show_precipitation": CFG["show_precipitation"],
            "precipitation_opacity": CFG["precipitation_opacity"],
            "show_photos": CFG["show_photos"],
            "show_logos": CFG["show_logos"],
        },
        "aircraft": aircraft,
        "weather": weather,
        "radar": radar,
        "iss": iss,
        "sondes": sondes,
        "ships": SHIPS.snapshot(now) if SHIPS else [],
        "airspace": airspace,
        "health": {
            "source": source,
            "count": len(aircraft),
            "last_ok_age_s": round(age, 1) if age is not None else None,
            "failures": failures,
            "stale": age is None or age > CFG["stale_after_s"],
        },
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

# /api/state carries trails and full route records now, which compresses by
# roughly ten to one. Worth doing when a kiosk asks for it every five seconds.
GZIP_MIN_BYTES = 2048


def config_path():
    return os.environ.get("FLIGHT_RADAR_CONFIG", os.path.join(HERE, "config.json"))


def public_config():
    """The settings. Secrets come back empty; see SECRET_KEYS for why."""
    out = {}
    for key in EDITABLE_KEYS:
        out[key] = "" if key in SECRET_KEYS else CFG.get(key)
    return out


def secrets_set():
    """Which secrets have a value, so the panel can say "set" without one."""
    return dict((key, bool(CFG.get(key))) for key in SECRET_KEYS)


def save_config(updates):
    """
    Merge into the config file on disk and into the running process.

    An empty secret means "unchanged" -- otherwise opening the settings tab and
    pressing save would wipe every key on the box. Sending the literal string
    CLEAR is how you actually empty one.
    """
    clean = {}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            continue
        if key in SECRET_KEYS:
            if value == "" or value is None:
                continue
            if value == CLEAR_SECRET:
                value = ""
        clean[key] = value

    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}

    existing.update(clean)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)

    # Apply to the running process as far as it goes. Types first, or a form
    # field's "20" becomes a string and every comparison against it is wrong.
    CFG.update(clean)
    for key in INT_KEYS:
        CFG[key] = int(float(CFG[key]))
    for key in FLOAT_KEYS:
        CFG[key] = float(CFG[key])
    for key in BOOL_KEYS:
        CFG[key] = _as_bool(CFG[key])
    if ALERTER:
        ALERTER.reload(CFG)
    return sorted(clean)


def build_metrics():
    """
    Prometheus text exposition.

    Deliberately small: how many aircraft are in view, whether the feed is
    healthy, and how many alerts have fired since boot. That is enough to
    graph the display's health and to notice when it quietly stops seeing
    anything, which is the failure that otherwise goes unnoticed for weeks.
    """
    state = build_state()
    health = state["health"]
    lines = [
        "# HELP flightradar_aircraft Aircraft currently in range.",
        "# TYPE flightradar_aircraft gauge",
        "flightradar_aircraft %d" % health["count"],
        "# HELP flightradar_feed_stale Whether the aircraft feed is stale.",
        "# TYPE flightradar_feed_stale gauge",
        "flightradar_feed_stale %d" % (1 if health["stale"] else 0),
        "# HELP flightradar_poll_failures Consecutive failures of every source.",
        "# TYPE flightradar_poll_failures gauge",
        "flightradar_poll_failures %d" % health["failures"],
        "# HELP flightradar_last_ok_age_seconds Age of the last successful poll.",
        "# TYPE flightradar_last_ok_age_seconds gauge",
        "flightradar_last_ok_age_seconds %s" % (health["last_ok_age_s"] or 0),
        "# HELP flightradar_uptime_seconds Time since this process started.",
        "# TYPE flightradar_uptime_seconds counter",
        "flightradar_uptime_seconds %d" % int(time.time() - STARTED_AT),
        "# HELP flightradar_sondes Radiosondes in range.",
        "# TYPE flightradar_sondes gauge",
        "flightradar_sondes %d" % len(state["sondes"]),
        "# HELP flightradar_ships Ships in range.",
        "# TYPE flightradar_ships gauge",
        "flightradar_ships %d" % len(state["ships"]),
        "# HELP flightradar_alerts_total Alerts fired since start, by kind.",
        "# TYPE flightradar_alerts_total counter",
    ]
    counts = ALERTER.counts if ALERTER else {}
    for kind in alerts.Alerter.KINDS:
        lines.append('flightradar_alerts_total{kind="%s"} %d'
                     % (kind, counts.get(kind, 0)))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "flight-radar-web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence the per-request access log. A kiosk polling every five
        # seconds fills the journal with nothing worth reading; anything that
        # actually fails goes through log() instead.
        pass

    def handle_one_request(self):
        # A kiosk that reloads mid-response, or a browser tab being closed,
        # drops the socket while we are still writing to it. That is normal
        # and uninteresting, but the default handler prints a full traceback
        # for it -- which buries the failures that do matter.
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _send(self, code, body, content_type, cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")

        encoding = None
        if (len(body) >= GZIP_MIN_BYTES and
                "gzip" in self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, 6)
            encoding = "gzip"

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, code=200):
        self._send(code, json.dumps(payload), "application/json")

    # -- panel authentication ---------------------------------------------
    #
    # A shared password over plain HTTP on a home network. It keeps the
    # settings tab from being opened by whoever wanders past a kiosk; it is
    # not protection against anyone who can see the wire, and pretending
    # otherwise would be worse than saying so.

    def _authorised(self):
        if not CFG.get("panel_password"):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fr_session" and value in SESSIONS:
                return True
        return False

    def _require_auth(self):
        if self._authorised():
            return True
        self._send(401, json.dumps({"error": "password required"}),
                   "application/json")
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 256 * 1024)) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace")) or {}
        except ValueError:
            self._send_json({"error": "malformed JSON"}, 400)
            return

        if path == "/api/login":
            wanted = CFG.get("panel_password") or ""
            if not wanted or str(body.get("password", "")) == wanted:
                token = os.urandom(18).hex()
                SESSIONS.add(token)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "16")
                self.send_header("Set-Cookie",
                                 "fr_session=%s; Path=/; Max-Age=86400; SameSite=Lax"
                                 % token)
                self.end_headers()
                self.wfile.write(b'{"ok":true}    ')
            else:
                self._send_json({"error": "wrong password"}, 403)
            return

        if not self._require_auth():
            return

        if path == "/api/config":
            try:
                changed = save_config(body)
            except Exception as exc:
                log("config: save failed (%s)" % exc)
                self._send_json({"error": str(exc)}, 500)
                return
            log("config: saved %s" % (", ".join(changed) or "nothing"))
            self._send_json({"ok": True, "changed": changed})
            return

        if path == "/api/restart":
            # systemd has Restart=always, so exiting is how this process asks
            # to be restarted -- it has no privileges to do it any other way.
            self._send_json({"ok": True})
            log("restart requested from the panel")
            threading.Thread(
                target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
            return

        self._send_json({"error": "not found"}, 404)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/config":
            if not self._require_auth():
                return
            self._send_json({
                "config": public_config(),
                "secrets_set": secrets_set(),
                "clear_secret_with": CLEAR_SECRET,
                "needs_restart_for": RESTART_KEYS,
                "editable": list(EDITABLE_KEYS),
                "secret": list(SECRET_KEYS),
            })
            return
        if path == "/api/session":
            self._send_json({"protected": bool(CFG.get("panel_password")),
                             "authorised": self._authorised()})
            return
        if path in ("/api/alerts", "/api/alerts.csv", "/api/log", "/api/log.csv"):
            if not self._require_auth():
                return

        if path == "/api/state":
            self._send_json(build_state())
            return
        if path == "/api/health":
            self._send_json(build_state()["health"])
            return
        if path in ("/api/alerts", "/api/alerts.csv"):
            rows = ALERT_LOG.read(500) if ALERT_LOG else []
            if path.endswith(".csv"):
                self._send(200, alerts.to_csv(rows, (
                    "t", "kind", "title", "hex", "callsign", "reg", "type",
                    "squawk", "dist_nm", "alt_ft")),
                    "text/csv; charset=utf-8")
            else:
                self._send_json(rows)
            return
        if path in ("/api/log", "/api/log.csv"):
            rows = SPOT_LOG.read(2000) if SPOT_LOG else []
            if path.endswith(".csv"):
                self._send(200, alerts.to_csv(rows, (
                    "t", "hex", "callsign", "reg", "type", "airline", "dist_nm")),
                    "text/csv; charset=utf-8")
            else:
                self._send_json(rows)
            return
        if path == "/metrics":
            self._send(200, build_metrics(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path.startswith("/api/logo/"):
            self._serve_logo(path[len("/api/logo/"):])
            return
        if path.startswith("/api/photo/"):
            self._serve_photo(path[len("/api/photo/"):])
            return
        if path.startswith("/api/"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if path == "/":
            path = "/index.html"
        elif path in ("/panel", "/panel/"):
            path = "/panel.html"

        # Static files, confined to static/. Resolve first and then check the
        # result is still inside the directory, so neither ".." nor a symlink
        # can walk out of it.
        target = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        root = os.path.realpath(STATIC_DIR)
        if target != root and not target.startswith(root + os.sep):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        if not os.path.isfile(target):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return

        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return

        # Vendored libraries never change under a running install. Everything
        # else must not be cached, or a kiosk keeps showing yesterday's page
        # after an update.
        cache = "public, max-age=604800" if "/vendor/" in path else "no-store"
        ext = os.path.splitext(target)[1].lower()
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"), cache)

    # -- media -------------------------------------------------------------

    def _serve_logo(self, name):
        icao = os.path.splitext(name)[0].upper()
        body = MEDIA.logo(icao) if (MEDIA and CFG["show_logos"]) else None
        if not body:
            self._send(404, "no logo", "text/plain; charset=utf-8")
            return
        self._send(200, body, "image/png", "public, max-age=604800")

    def _serve_photo(self, name):
        hexcode = os.path.splitext(name)[0].lower()
        info = AIRCRAFT.get(hexcode) if (AIRCRAFT and CFG["show_photos"]) else None
        url = (info or {}).get("photo_thumb") or (info or {}).get("photo") or ""
        body = MEDIA.photo(hexcode, url) if (MEDIA and url) else None
        if not body:
            self._send(404, "no photo", "text/plain; charset=utf-8")
            return
        self._send(200, body, "image/jpeg", "public, max-age=604800")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global CFG, ROUTES, AIRCRAFT, TIMEZONES, MEDIA, SHIPS
    global ALERTER, ALERT_LOG, SPOT_LOG
    CFG = load_config()

    cache_dir = CFG["cache_dir"]
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        cache_dir = HERE

    ROUTES = enrich.RouteCache(os.path.join(cache_dir, "routes-cache.json"), log)
    AIRCRAFT = enrich.AircraftCache(os.path.join(cache_dir, "aircraft-cache.json"), log)
    TIMEZONES = enrich.TimezoneCache(os.path.join(cache_dir, "timezone-cache.json"), log)
    MEDIA = enrich.MediaCache(os.path.join(cache_dir, "media"), log)

    ALERT_LOG = alerts.JsonlLog(os.path.join(cache_dir, "alerts.jsonl"),
                                alerts.ALERT_LOG_CAP, log)
    SPOT_LOG = alerts.JsonlLog(os.path.join(cache_dir, "spotting.jsonl"),
                               alerts.SPOT_LOG_CAP, log)
    notifier = alerts.Notifier(CFG, log)
    ALERTER = alerts.Alerter(CFG, ALERT_LOG, SPOT_LOG, notifier, log)
    if ALERTER.watchlist:
        log("watchlist: %s" % ", ".join(ALERTER.watchlist))
    if notifier.enabled:
        log("alerts: notifying %s" % ", ".join(
            [n for n, on in (("ntfy", notifier.ntfy_topic),
                             ("webhook", notifier.webhook_url),
                             ("mqtt", notifier.mqtt)) if on]))

    threads = [aircraft_poller, weather_poller, radar_poller, enrichment_poller,
               alert_poller]
    if CFG["show_radio"]:
        threads.append(radio_poller)

    # Only start an overlay that can actually produce something. A key-gated
    # poller with no key would spend the life of the process waking up to
    # discover it still has no key.
    if CFG["show_iss"]:
        threads.append(iss_poller)
    if CFG["show_sondes"]:
        threads.append(sonde_poller)
    if CFG["show_airspace"] and CFG["openaip_key"]:
        threads.append(airspace_poller)
    elif CFG["show_airspace"]:
        log("airspace: no openaip_key set, overlay stays off")
    if CFG["show_ships"] and CFG["aisstream_key"]:
        SHIPS = overlays.ShipTracker(log)
        threads.append(ships_runner)
    elif CFG["show_ships"]:
        log("ships: no aisstream_key set, overlay stays off")

    for target in threads:
        threading.Thread(target=target, name=target.__name__, daemon=True).start()

    httpd = Server((CFG["host"], CFG["port"]), Handler)

    def save_all():
        for cache in (ROUTES, AIRCRAFT, TIMEZONES):
            if cache:
                cache.save()

    def stop(signum, frame):
        log("shutting down")
        SHUTDOWN.set()
        save_all()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log("flight radar on http://%s:%d/  (%.4f, %.4f  %d nm)" %
        (CFG["host"], CFG["port"], CFG["lat"], CFG["lon"], CFG["range_nm"]))
    try:
        httpd.serve_forever()
    finally:
        SHUTDOWN.set()
        save_all()


if __name__ == "__main__":
    main()
