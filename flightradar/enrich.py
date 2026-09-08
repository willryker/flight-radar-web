"""
Everything ADS-B does not carry.

A transponder broadcasts who and where, never where to, who operates it, what
it looks like or where it is registered. All of that is looked up separately,
cached hard on disk, and -- crucially -- cached on misses too. Without that,
every unknown private aircraft in the area is re-asked forever.

Sources, all free and unauthenticated:
  adsbdb.com          routes with full airport records, and aircraft details
                      including the planespotters.net photo URL
  esp32flight-logos   90x90 airline logo PNGs by ICAO code
  Open-Meteo          UTC offset at an airport, for arrival in local time
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .net import fetch_json as _fetch_json

USER_AGENT = "esp32-flight-radar-web/1.0"

ADSBDB = "https://api.adsbdb.com/v0"
LOGO_URL = "https://raw.githubusercontent.com/theqkash/esp32flight-logos/main/logos/%s.png"

# Only airline callsigns have routes. A registration like N841MC is a private
# aircraft and asking about it spends a request that could have answered
# something. Three letters, then a digit, then up to three more.
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,3}$")

# Always worth a second look, regardless of watchlist. Types that turn heads:
# the very large, the very rare and the military transports.
NOTABLE_TYPES = {
    "A388",                                          # A380
    "A124", "AN22", "A225",                          # Antonov heavy lift
    "B741", "B742", "B743", "B744", "B748", "BLCF",  # 747 in any form
    "C5M", "C17", "C130", "C30J", "A400",            # military transport
    "B52", "B1", "B2", "K35R", "KC46", "E3TF", "E6",
    "B703", "DC10", "MD11", "L101", "A342", "A343", "A345", "A346",
    "CONC", "SR71", "U2",
}

HIT_TTL_S = 24 * 3600
MISS_TTL_S = 6 * 3600
AIRCRAFT_HIT_TTL_S = 30 * 24 * 3600   # a registration does not change
AIRCRAFT_MISS_TTL_S = 24 * 3600
TZ_TTL_S = 12 * 3600                  # re-asked so daylight saving is picked up
CACHE_CAP = 4000

# Fallback only. adsbdb reports the country of registration directly for most
# aircraft; this covers the ones it does not know. Longest prefix wins, so
# "9H" is tested before "9".
REG_PREFIXES = (
    ("VH-", "AU"), ("ZK-", "NZ"), ("VT-", "IN"), ("9V-", "SG"), ("9M-", "MY"),
    ("HS-", "TH"), ("PK-", "ID"), ("RP-", "PH"), ("VN-", "VN"), ("XU-", "KH"),
    ("XY-", "MM"), ("XZ-", "MM"), ("8Q-", "MV"), ("4R-", "LK"), ("S2-", "BD"),
    ("AP-", "PK"), ("EP-", "IR"), ("YI-", "IQ"), ("YK-", "SY"), ("OD-", "LB"),
    ("JY-", "JO"), ("4X-", "IL"), ("A6-", "AE"), ("A7-", "QA"), ("A9C", "BH"),
    ("A4O", "OM"), ("9K-", "KW"), ("HZ-", "SA"), ("70-", "YE"),
    ("SU-", "EG"), ("7T-", "DZ"), ("CN-", "MA"), ("TS-", "TN"), ("5A-", "LY"),
    ("ZS-", "ZA"), ("5Y-", "KE"), ("ET-", "ET"), ("TU-", "CI"), ("5N-", "NG"),
    ("9G-", "GH"), ("D2-", "AO"), ("C9-", "MZ"), ("9J-", "ZM"), ("5H-", "TZ"),
    ("5X-", "UG"), ("3B-", "MU"), ("6V-", "SN"), ("TN-", "CG"), ("TT-", "TD"),
    ("9H-", "MT"), ("5B-", "CY"), ("9A-", "HR"), ("S5-", "SI"), ("E7-", "BA"),
    ("Z3-", "MK"), ("ZA-", "AL"), ("YU-", "RS"), ("4O-", "ME"), ("LZ-", "BG"),
    ("YR-", "RO"), ("HA-", "HU"), ("OM-", "SK"), ("OK-", "CZ"), ("SP-", "PL"),
    ("YL-", "LV"), ("LY-", "LT"), ("ES-", "EE"), ("OH-", "FI"), ("SE-", "SE"),
    ("LN-", "NO"), ("OY-", "DK"), ("TF-", "IS"), ("EI-", "IE"), ("EJ-", "IE"),
    ("CS-", "PT"), ("EC-", "ES"), ("OO-", "BE"), ("PH-", "NL"), ("OE-", "AT"),
    ("HB-", "CH"), ("LX-", "LU"), ("3A-", "MC"), ("T7-", "SM"),
    ("RA-", "RU"), ("RF-", "RU"), ("UR-", "UA"), ("EW-", "BY"), ("4K-", "AZ"),
    ("EK-", "AM"), ("4L-", "GE"), ("UP-", "KZ"), ("EY-", "TJ"), ("EX-", "KG"),
    ("UK-", "UZ"), ("EZ-", "TM"), ("JU-", "MN"),
    ("XA-", "MX"), ("XB-", "MX"), ("XC-", "MX"), ("TG-", "GT"), ("YS-", "SV"),
    ("HR-", "HN"), ("YN-", "NI"), ("TI-", "CR"), ("HP-", "PA"), ("CU-", "CU"),
    ("HI-", "DO"), ("HH-", "HT"), ("6Y-", "JM"), ("9Y-", "TT"), ("VP-", "GB"),
    ("PP-", "BR"), ("PR-", "BR"), ("PT-", "BR"), ("PS-", "BR"), ("PU-", "BR"),
    ("LV-", "AR"), ("CC-", "CL"), ("CP-", "BO"), ("OB-", "PE"), ("HC-", "EC"),
    ("HK-", "CO"), ("YV-", "VE"), ("ZP-", "PY"), ("CX-", "UY"), ("8R-", "GY"),
    ("D-", "DE"), ("F-", "FR"), ("G-", "GB"), ("I-", "IT"), ("C-", "CA"),
    ("N", "US"), ("JA", "JP"), ("HL", "KR"), ("B-", "CN"), ("M-", "IM"),
    ("2-", "GG"), ("Z-", "ZW"),
)


def registration_country(reg):
    """ISO 3166-1 alpha-2 for a registration, or "" when the prefix is unknown."""
    reg = (reg or "").upper().strip()
    if not reg:
        return ""
    for prefix, iso in sorted(REG_PREFIXES, key=lambda kv: -len(kv[0])):
        if reg.startswith(prefix):
            return iso
    return ""


def is_notable(type_code):
    return (type_code or "").upper() in NOTABLE_TYPES


# ICAO type designators for rotorcraft. The ADS-B emitter category A7 is the
# primary signal and is usually present; this covers the aircraft that
# broadcast no category at all, which is most of the older fleet.
ROTORCRAFT_TYPES = {
    "R22", "R44", "R66", "R100",
    "B06", "B06T", "B47G", "B222", "B230", "B407", "B412", "B427", "B429",
    "B430", "B505", "B525",
    "AS50", "AS55", "AS65", "AS32", "AS3B", "S330", "S332", "S365", "SA34",
    "H125", "H130", "H135", "H140", "H145", "H155", "H160", "H175",
    "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75",
    "A109", "A119", "A139", "A169", "A189",
    "S61", "S64", "S76", "S92", "H500", "H600", "MD52", "MD60", "MD90",
    "EN28", "EN48", "BK17", "GAZL", "LYNX", "PUMA", "MERL",
    "H60", "UH60", "UH1", "H64", "H47", "CH47", "CH53", "V22", "H6",
    "MI8", "MI17", "MI24", "MI26", "MI2", "KA32", "KA52",
    "EXPL", "EXEC", "SCOR", "HUCO",
}

# Words that only appear in a rotorcraft's description.
ROTOR_WORDS = ("HELICOPTER", "ROTORCRAFT", "GYROCOPTER", "AUTOGYRO")


def is_rotorcraft(type_code, category="", description=""):
    """
    True for anything with a rotor rather than a wing.

    Three signals, cheapest first: the ADS-B emitter category (A7 is
    "rotorcraft" and is authoritative when broadcast), the ICAO type
    designator, then the free-text description as a last resort.
    """
    if (category or "").upper() == "A7":
        return True
    if (type_code or "").upper() in ROTORCRAFT_TYPES:
        return True
    upper = (description or "").upper()
    return any(word in upper for word in ROTOR_WORDS)


# ---------------------------------------------------------------------------
# Disk-backed caches
# ---------------------------------------------------------------------------

class JsonCache:
    """
    A dict persisted as one JSON file, with per-entry TTL and a cap.

    Hits and misses get different lifetimes: a route that resolved is good for
    a day, a callsign nobody has heard of is worth re-asking sooner in case it
    was simply a new flight number.
    """

    hit_ttl = HIT_TTL_S
    miss_ttl = MISS_TTL_S
    cap = CACHE_CAP

    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda msg: None)
        self.lock = threading.Lock()
        self.entries = {}
        self.dirty = False
        self.load()

    def _fresh(self, entry, now):
        ttl = self.hit_ttl if entry.get("value") else self.miss_ttl
        return now - entry.get("stamp", 0) <= ttl

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.entries = {k: v for k, v in loaded.items()
                                if isinstance(v, dict) and "stamp" in v}
            self.log("cache: %d entries from %s" % (len(self.entries), self.path))
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.log("cache: %s unreadable (%s), starting empty" % (self.path, exc))

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            snapshot = dict(self.entries)
            self.dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, self.path)
        except Exception as exc:
            self.log("cache: could not write %s (%s)" % (self.path, exc))

    def known(self, key):
        with self.lock:
            entry = self.entries.get(key)
            return bool(entry) and self._fresh(entry, time.time())

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry and self._fresh(entry, time.time()):
                return entry.get("value")
            return None

    def put(self, key, value):
        with self.lock:
            self.entries[key] = {"value": value, "stamp": time.time()}
            self.dirty = True
            if len(self.entries) > self.cap:
                oldest = sorted(self.entries.items(),
                                key=lambda kv: kv[1].get("stamp", 0))
                for dead, _ in oldest[:len(self.entries) - self.cap]:
                    del self.entries[dead]


class RouteCache(JsonCache):
    """Callsign -> airline and both airports, or None for a known miss."""

    @staticmethod
    def _airport(node):
        if not isinstance(node, dict):
            return None
        lat = node.get("latitude")
        lon = node.get("longitude")
        return {
            "iata": node.get("iata_code") or "",
            "icao": node.get("icao_code") or "",
            "name": node.get("name") or "",
            "city": node.get("municipality") or "",
            "country": node.get("country_iso_name") or "",
            "lat": float(lat) if isinstance(lat, (int, float)) else None,
            "lon": float(lon) if isinstance(lon, (int, float)) else None,
        }

    def lookup(self, callsign):
        doc = _fetch_json("%s/callsign/%s" % (ADSBDB, urllib.parse.quote(callsign)))
        route = (doc.get("response") or {}).get("flightroute") or {}
        airline = route.get("airline") or {}
        origin = self._airport(route.get("origin"))
        dest = self._airport(route.get("destination"))
        if not origin and not dest:
            return None
        return {
            "airline": {
                "name": airline.get("name") or "",
                "icao": airline.get("icao") or "",
                "iata": airline.get("iata") or "",
                "country": airline.get("country_iso") or "",
            },
            "flight_iata": route.get("callsign_iata") or "",
            "from": origin,
            "to": dest,
        }


class AircraftCache(JsonCache):
    """ICAO hex -> registration, type, owner and photo URLs."""

    hit_ttl = AIRCRAFT_HIT_TTL_S
    miss_ttl = AIRCRAFT_MISS_TTL_S

    def lookup(self, hexcode):
        doc = _fetch_json("%s/aircraft/%s" % (ADSBDB, urllib.parse.quote(hexcode)))
        ac = (doc.get("response") or {}).get("aircraft") or {}
        if not ac:
            return None
        return {
            "reg": ac.get("registration") or "",
            "type": ac.get("icao_type") or "",
            "type_long": ac.get("type") or "",
            "manufacturer": ac.get("manufacturer") or "",
            "owner": ac.get("registered_owner") or "",
            "country": ac.get("registered_owner_country_iso_name") or "",
            "operator_flag": ac.get("registered_owner_operator_flag_code") or "",
            "photo": ac.get("url_photo") or "",
            "photo_thumb": ac.get("url_photo_thumbnail") or "",
        }


class TimezoneCache(JsonCache):
    """
    Airport -> UTC offset, so an arrival time can be shown on the destination's
    own clock. Re-asked twice a day; the offset itself moves with daylight
    saving and a cached-forever answer is wrong for half the year.
    """

    hit_ttl = TZ_TTL_S
    miss_ttl = TZ_TTL_S
    cap = 600

    def lookup(self, lat, lon):
        params = urllib.parse.urlencode({
            "latitude": "%.3f" % lat,
            "longitude": "%.3f" % lon,
            "current": "temperature_2m",
            "timezone": "auto",
        })
        doc = _fetch_json("https://api.open-meteo.com/v1/forecast?" + params)
        offset = doc.get("utc_offset_seconds")
        if not isinstance(offset, (int, float)):
            return None
        return {"offset_s": int(offset), "tz": doc.get("timezone") or ""}


# ---------------------------------------------------------------------------
# Airline logos and aircraft photos
# ---------------------------------------------------------------------------

class MediaCache:
    """
    Logos and photos, fetched once and then served off local disk.

    The page could point an <img> straight at GitHub and planespotters, but
    then every kiosk refresh re-fetches them from the public internet, and a
    screen that loses its uplink loses its logos. Proxying also means the
    browser only ever talks to this box, which is one less thing to explain
    when something is blocked.
    """

    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.lock = threading.Lock()
        self.inflight = {}
        self.misses = {}
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            self.log("media: cannot create %s (%s)" % (self.dir, exc))

    def _path(self, key):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return os.path.join(self.dir, safe)

    def get(self, key, url, timeout=15):
        """Cached bytes for key, fetching from url on first use. None on failure."""
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            pass

        now = time.time()
        with self.lock:
            # Do not retry a 404 on every single page load.
            if now - self.misses.get(key, 0) < 3600:
                return None
            lock = self.inflight.get(key)
            if lock is None:
                lock = self.inflight[key] = threading.Lock()

        # One fetch per key even when several browsers ask at once.
        with lock:
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                pass
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise IOError("HTTP %s" % resp.status)
                    body = resp.read()
            except Exception as exc:
                with self.lock:
                    self.misses[key] = now
                if not isinstance(exc, urllib.error.HTTPError) or exc.code != 404:
                    self.log("media: %s -> %s" % (url, exc))
                return None

            tmp = path + ".tmp"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(body)
                os.replace(tmp, path)
            except OSError as exc:
                self.log("media: could not store %s (%s)" % (key, exc))
            return body

    def logo(self, icao):
        icao = (icao or "").upper()
        if not re.match(r"^[A-Z0-9]{2,4}$", icao):
            return None
        return self.get("logo-%s.png" % icao, LOGO_URL % icao)

    def photo(self, hexcode, url):
        if not url:
            return None
        return self.get("photo-%s.jpg" % (hexcode or "").lower(), url)
