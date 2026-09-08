"""
The other things in the sky, and one on the sea.

All four are optional and independent. Two need no key and are on by default;
two need a free key from the service and stay completely dormant without one,
including their poller threads -- an overlay nobody has keyed should cost
nothing at all, not even a timer.

  ISS        wheretheiss.at, keyless, 30 s
  Radiosondes SondeHub, keyless, 4 min
  Airspace   openAIP, own key, fetched once for the location
  Ships      aisstream.io, own key, a websocket held open
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request

from . import geo
from .net import fetch_json as _fetch_json

USER_AGENT = "esp32-flight-radar-web/1.0"

ISS_URL = "https://api.wheretheiss.at/v1/satellites/25544"
SONDEHUB_URL = "https://api.v2.sondehub.org/sondes/telemetry"
OPENAIP_URL = "https://api.core.openaip.net/api/airspaces"
AISSTREAM_HOST = "stream.aisstream.io"

ISS_REFRESH_S = 30
SONDE_REFRESH_S = 240
SONDE_RANGE_NM = 135.0          # 250 km, the range the firmware uses
SHIP_TTL_S = 600
AIRSPACE_MAX = 28
AIRSPACE_MAX_POINTS = 48


# ---------------------------------------------------------------------------
# ISS
# ---------------------------------------------------------------------------

def fetch_iss(home_lat, home_lon):
    doc = _fetch_json(ISS_URL, timeout=10)
    lat = doc.get("latitude")
    lon = doc.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None

    alt_km = float(doc.get("altitude") or 0.0)
    dist = geo.distance_nm(home_lat, home_lon, lat, lon)
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt_km": round(alt_km, 1),
        "speed_kmh": round(float(doc.get("velocity") or 0.0)),
        "dist_nm": round(dist, 1),
        "az_deg": round(geo.bearing_deg(home_lat, home_lon, lat, lon), 1),
        "compass": geo.compass_point(geo.bearing_deg(home_lat, home_lon, lat, lon)),
        "elev_deg": round(geo.satellite_elevation_deg(dist, alt_km) or -90.0, 1),
        # "eclipsed" means it is in the earth's shadow and invisible even when
        # it is above the horizon, which is most of the reason a pass fails.
        "sunlit": doc.get("visibility") == "daylight",
        "stamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Radiosondes
# ---------------------------------------------------------------------------

def fetch_sondes(home_lat, home_lon, range_nm=SONDE_RANGE_NM):
    """
    Weather balloons currently transmitting nearby.

    The distance filter is applied here rather than trusted to the API: asking
    SondeHub for sondes near a given point came back with one on another
    continent, so whatever those parameters do it is not what the names suggest.
    """
    params = urllib.parse.urlencode({
        "duration": "3h",
        "lat": "%.3f" % home_lat,
        "lon": "%.3f" % home_lon,
        "distance": int(range_nm * 1852),
    })
    doc = _fetch_json(SONDEHUB_URL + "?" + params, timeout=20)
    if not isinstance(doc, dict):
        return []

    out = []
    for serial, frames in doc.items():
        if not isinstance(frames, dict) or not frames:
            continue
        # Keys are ISO timestamps, so the newest sorts last.
        latest = frames[sorted(frames)[-1]]
        if not isinstance(latest, dict):
            continue
        lat, lon = latest.get("lat"), latest.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        dist = geo.distance_nm(home_lat, home_lon, lat, lon)
        if dist > range_nm:
            continue
        out.append({
            "serial": str(serial),
            "type": latest.get("subtype") or latest.get("type") or "",
            "lat": float(lat),
            "lon": float(lon),
            "alt_m": round(float(latest.get("alt") or 0.0)),
            "vel_v": round(float(latest.get("vel_v") or 0.0), 1),
            "dist_nm": round(dist, 1),
        })
    out.sort(key=lambda s: s["dist_nm"])
    return out


# ---------------------------------------------------------------------------
# Airspace (openAIP, needs a free key)
# ---------------------------------------------------------------------------

# openAIP numbers its airspace types; these are the ones worth drawing.
AIRSPACE_TYPES = {
    0: "OTHER", 1: "R", 2: "D", 3: "P", 4: "CTR", 5: "TMZ", 6: "RMZ",
    7: "TMA", 8: "TRA", 9: "TSA", 10: "FIR", 11: "UIR", 12: "ADIZ",
    13: "ATZ", 14: "MATZ", 21: "MTMA", 22: "MCTR",
}

DRAW_TYPES = {"R", "D", "P", "CTR", "TMA", "ATZ", "TRA", "MCTR", "MTMA", "MATZ"}


def _decimate(points, limit=AIRSPACE_MAX_POINTS):
    """Thin a polygon to at most `limit` points, keeping the ends."""
    if len(points) <= limit:
        return points
    step = len(points) / float(limit - 1)
    out = [points[int(i * step)] for i in range(limit - 1)]
    out.append(points[-1])
    return out


def fetch_airspace(home_lat, home_lon, range_nm, api_key):
    if not api_key:
        return []
    params = urllib.parse.urlencode({
        "pos": "%.4f,%.4f" % (home_lat, home_lon),
        "dist": int(range_nm * 1852 * 2),      # a little beyond the ring
        "limit": 100,
    })
    doc = _fetch_json(OPENAIP_URL + "?" + params, timeout=25,
                      headers={"x-openaip-api-key": api_key})

    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return []

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = AIRSPACE_TYPES.get(item.get("type"), "OTHER")
        if kind not in DRAW_TYPES:
            continue
        geom = (item.get("geometry") or {})
        coords = geom.get("coordinates")
        # GeoJSON polygons nest one level for the outer ring.
        if isinstance(coords, list) and coords and isinstance(coords[0], list):
            ring = coords[0]
        else:
            continue
        points = [[p[1], p[0]] for p in ring
                  if isinstance(p, list) and len(p) >= 2]
        if len(points) < 3:
            continue
        out.append({
            "name": item.get("name") or "",
            "type": kind,
            "points": _decimate(points),
        })
        if len(out) >= AIRSPACE_MAX:
            break
    return out


# ---------------------------------------------------------------------------
# Ships (aisstream.io, needs a free key)
# ---------------------------------------------------------------------------
#
# aisstream only speaks websocket, and the standard library has no websocket
# client. What follows is the minimum that talks to it: an HTTP upgrade, and
# enough of RFC 6455 to read text frames and answer pings. No compression
# extension is negotiated, so no permessage-deflate to implement.

AIS_TYPES = {
    30: "FISHING", 31: "TUG", 32: "TUG", 35: "MILITARY", 36: "SAILING",
    37: "PLEASURE", 50: "PILOT", 51: "SAR", 52: "TUG", 53: "TENDER",
    55: "PATROL", 60: "PASSENGER", 61: "PASSENGER", 62: "PASSENGER",
    63: "PASSENGER", 64: "PASSENGER", 69: "PASSENGER",
    70: "CARGO", 71: "CARGO", 72: "CARGO", 73: "CARGO", 74: "CARGO",
    79: "CARGO", 80: "TANKER", 81: "TANKER", 82: "TANKER", 83: "TANKER",
    84: "TANKER", 89: "TANKER",
}


class WebSocket:
    """A read-mostly websocket client. Text frames in, text frames out."""

    def __init__(self, host, path="/v0", port=443, timeout=30):
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(
            raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self.buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: %s\r\n\r\n" % (path, host, key, USER_AGENT)).encode())

        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("connection closed during handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise IOError("upgrade refused: %s" % status.decode("ascii", "replace"))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])          # FIN + text
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        # Client-to-server frames must be masked; the server's are not.
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        """Next text frame as a string, or None for a control frame."""
        first, second = self._read(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        if second & 0x80:
            mask = self._read(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(length)))
        else:
            data = self._read(length)

        if opcode == 0x8:
            raise IOError("server closed the websocket")
        if opcode == 0x9:                    # ping -> pong, same payload
            self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            return None
        if opcode in (0x1, 0x2):
            return data.decode("utf-8", "replace")
        return None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class ShipTracker:
    """
    Ships within range, kept in a dict and aged out.

    aisstream pushes position reports continuously rather than answering
    queries, so this holds a connection open and updates as they arrive. Ships
    are dropped after ten minutes without a report -- an AIS target that has
    stopped transmitting has usually gone out of range, not sunk.
    """

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.lock = threading.Lock()
        self.ships = {}
        self.connected = False

    def snapshot(self, now=None):
        now = now or time.time()
        with self.lock:
            fresh = [dict(s) for s in self.ships.values()
                     if now - s["stamp"] <= SHIP_TTL_S]
            for mmsi in [k for k, v in self.ships.items()
                         if now - v["stamp"] > SHIP_TTL_S]:
                del self.ships[mmsi]
        fresh.sort(key=lambda s: s["dist_nm"])
        return fresh

    def _bounds(self, lat, lon, range_nm):
        # A degree of latitude is 60 nm; longitude shrinks with the cosine.
        import math
        dlat = range_nm / 60.0
        dlon = range_nm / max(1.0, 60.0 * math.cos(math.radians(lat)))
        return [[lat - dlat, lon - dlon], [lat + dlat, lon + dlon]]

    def run(self, api_key, home_lat, home_lon, range_nm, shutdown):
        backoff = 5
        while not shutdown.is_set():
            ws = None
            try:
                ws = WebSocket(AISSTREAM_HOST)
                ws.send(json.dumps({
                    "APIKey": api_key,
                    "BoundingBoxes": [self._bounds(home_lat, home_lon, range_nm)],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                self.connected = True
                backoff = 5
                self.log("ships: connected to aisstream")

                while not shutdown.is_set():
                    raw = ws.recv()
                    if raw:
                        self._ingest(raw, home_lat, home_lon)
            except Exception as exc:
                if not shutdown.is_set():
                    self.log("ships: %s (retrying in %ds)" % (exc, backoff))
            finally:
                self.connected = False
                if ws:
                    ws.close()
            shutdown.wait(backoff)
            backoff = min(300, backoff * 2)

    def _ingest(self, raw, home_lat, home_lon):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        meta = msg.get("MetaData") or {}
        mmsi = meta.get("MMSI") or meta.get("MMSI_String")
        if not mmsi:
            return
        mmsi = str(mmsi)
        kind = msg.get("MessageType")
        body = (msg.get("Message") or {}).get(kind) or {}

        with self.lock:
            ship = self.ships.setdefault(mmsi, {
                "mmsi": mmsi, "name": "", "dest": "", "stype": "",
                "lat": None, "lon": None, "sog_kt": 0.0, "cog_deg": 0.0,
                "dist_nm": 0.0, "stamp": 0.0,
            })

            name = (meta.get("ShipName") or "").strip()
            if name:
                ship["name"] = name

            if kind == "PositionReport":
                lat = body.get("Latitude")
                lon = body.get("Longitude")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    ship["lat"] = float(lat)
                    ship["lon"] = float(lon)
                    ship["dist_nm"] = round(
                        geo.distance_nm(home_lat, home_lon, lat, lon), 2)
                    ship["sog_kt"] = round(float(body.get("Sog") or 0.0), 1)
                    ship["cog_deg"] = round(float(body.get("Cog") or 0.0), 1)
                    ship["stamp"] = time.time()
            elif kind == "ShipStaticData":
                dest = (body.get("Destination") or "").strip()
                if dest:
                    ship["dest"] = dest
                ship["stype"] = AIS_TYPES.get(body.get("Type"), ship["stype"])

            if ship["lat"] is None:
                del self.ships[mmsi]
