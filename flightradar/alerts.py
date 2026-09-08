"""
Watchlist, alerts, the spotting log, and the ways out to other systems.

The rule everywhere here is that an alert fires once per aircraft per visit,
not once per poll. Traffic sits in range for minutes and the poller runs every
five seconds, so anything naive would send sixty notifications for one 747.
A hex that has already fired is remembered until it has been gone long enough
to count as a new arrival.
"""

import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request

from .net import USER_AGENT

# An aircraft has to be absent this long before coming back counts as a new
# arrival worth announcing again.
REARM_AFTER_S = 3600

ALERT_LOG_CAP = 2000
SPOT_LOG_CAP = 20000


def parse_watchlist(value):
    """
    "RYR, N12, SP-" -> ["RYR", "N12", "SP-"]

    Accepts a comma-separated string or a list, because this arrives from a
    config file, an environment variable and a form field.
    """
    if not value:
        return []
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    return [p.strip().upper() for p in parts if str(p).strip()]


def matches_watchlist(ac, prefixes):
    if not prefixes:
        return False
    callsign = (ac.get("flight") or "").upper()
    reg = (ac.get("reg") or "").upper()
    for prefix in prefixes:
        if (callsign and callsign.startswith(prefix)) or \
           (reg and reg.startswith(prefix)):
            return True
    return False


# ---------------------------------------------------------------------------
# Append-only logs
# ---------------------------------------------------------------------------

class JsonlLog:
    """
    A capped append-only log of dicts, one JSON object per line.

    JSON Lines rather than a single JSON array so that appending is a write
    and not a read-modify-write of the whole file, and so a truncated line
    after a power cut costs one record instead of the lot -- which matters on
    a device whose normal shutdown is someone pulling the plug.
    """

    def __init__(self, path, cap, log=None):
        self.path = path
        self.cap = cap
        self.log = log or (lambda msg: None)
        self.lock = threading.Lock()

    def append(self, record):
        line = json.dumps(record, separators=(",", ":"))
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                self.log("log: cannot write %s (%s)" % (self.path, exc))
                return
            self._trim_locked()

    def _trim_locked(self):
        # Only pay for the rewrite occasionally, not on every append.
        try:
            if os.path.getsize(self.path) < self.cap * 200:
                return
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) <= self.cap:
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-self.cap:])
            os.replace(tmp, self.path)
        except OSError as exc:
            self.log("log: cannot trim %s (%s)" % (self.path, exc))

    def read(self, limit=500):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # a torn last line after a power cut
        out.reverse()             # newest first
        return out


def to_csv(rows, columns):
    """Rows of dicts as CSV. Quoting is minimal and correct: double the quotes."""
    def cell(value):
        text = "" if value is None else str(value)
        if any(c in text for c in ',"\n'):
            return '"' + text.replace('"', '""') + '"'
        return text

    out = [",".join(columns)]
    for row in rows:
        out.append(",".join(cell(row.get(c)) for c in columns))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Outbound notifications
# ---------------------------------------------------------------------------

def _post(url, body, headers=None, timeout=8):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class MqttPublisher:
    """
    Just enough MQTT 3.1.1 to publish a retained-free QoS 0 message.

    A connection is opened per alert rather than held. Alerts are occasional,
    a held connection needs keepalive pings and reconnect logic, and the whole
    point of this file is that it should not be able to break the radar.

    Accepts mqtt://user:pass@host:port and mqtts:// for TLS.
    """

    def __init__(self, uri, log=None):
        self.uri = uri
        self.log = log or (lambda msg: None)
        parsed = urllib.parse.urlparse(uri)
        self.tls = parsed.scheme == "mqtts"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (8883 if self.tls else 1883)
        self.user = parsed.username or ""
        self.password = parsed.password or ""

    @staticmethod
    def _remaining_length(n):
        out = bytearray()
        while True:
            byte = n % 128
            n //= 128
            if n:
                byte |= 0x80
            out.append(byte)
            if not n:
                return bytes(out)

    @staticmethod
    def _string(text):
        raw = text.encode("utf-8")
        return struct.pack(">H", len(raw)) + raw

    def publish(self, topic, payload):
        if not self.host:
            return False
        sock = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=8)
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.host)

            flags = 0x02                                   # clean session
            body = self._string("MQTT") + bytes([4])       # protocol level 3.1.1
            if self.user:
                flags |= 0x80
            if self.password:
                flags |= 0x40
            body += bytes([flags]) + struct.pack(">H", 30)  # keepalive, seconds
            body += self._string("flight-radar")
            if self.user:
                body += self._string(self.user)
            if self.password:
                body += self._string(self.password)
            sock.sendall(bytes([0x10]) + self._remaining_length(len(body)) + body)

            sock.settimeout(8)
            ack = sock.recv(4)
            if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                raise IOError("connection refused (code %s)" %
                              (ack[3] if len(ack) > 3 else "?"))

            pub = self._string(topic) + payload.encode("utf-8")
            sock.sendall(bytes([0x30]) + self._remaining_length(len(pub)) + pub)
            sock.sendall(bytes([0xE0, 0x00]))              # DISCONNECT
            return True
        except Exception as exc:
            self.log("mqtt: %s" % exc)
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


class Notifier:
    """Fans one alert out to whichever of ntfy, a webhook and MQTT are set."""

    def __init__(self, cfg, log=None):
        self.log = log or (lambda msg: None)
        self.ntfy_server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = cfg.get("ntfy_topic") or ""
        self.webhook_url = cfg.get("webhook_url") or ""
        self.mqtt_topic = cfg.get("mqtt_topic") or "flightradar/alert"
        self.mqtt = MqttPublisher(cfg["mqtt_uri"], log) if cfg.get("mqtt_uri") else None

    @property
    def enabled(self):
        return bool(self.ntfy_topic or self.webhook_url or self.mqtt)

    def send(self, alert):
        """Never raises. A broken notifier must not stop the radar."""
        if self.ntfy_topic:
            try:
                _post("%s/%s" % (self.ntfy_server, self.ntfy_topic),
                      alert["message"].encode("utf-8"),
                      {"Title": alert["title"],
                       "Priority": "urgent" if alert["kind"] == "emergency" else "default",
                       "Tags": alert["kind"]})
            except Exception as exc:
                self.log("ntfy: %s" % exc)

        if self.webhook_url:
            try:
                _post(self.webhook_url,
                      json.dumps(alert).encode("utf-8"),
                      {"Content-Type": "application/json"})
            except Exception as exc:
                self.log("webhook: %s" % exc)

        if self.mqtt:
            self.mqtt.publish(self.mqtt_topic, json.dumps(alert))


# ---------------------------------------------------------------------------
# The alerter
# ---------------------------------------------------------------------------

class Alerter:
    """
    Decides what is worth saying, once per aircraft per visit.

    Also keeps the spotting log, which is a different thing: every aircraft
    seen at all, recorded once, so the history tab can answer "what came over
    on Tuesday" rather than only "what set off an alarm".
    """

    KINDS = ("emergency", "watchlist", "military", "notable")

    def __init__(self, cfg, alert_log, spot_log, notifier, log=None):
        self.log = log or (lambda msg: None)
        self.alert_log = alert_log
        self.spot_log = spot_log
        self.notifier = notifier
        self.lock = threading.Lock()
        self.fired = {}          # (hex, kind) -> last fired
        self.seen = {}           # hex -> last seen, for the spotting log
        self.counts = dict((k, 0) for k in self.KINDS)
        self.reload(cfg)

    def reload(self, cfg):
        self.watchlist = parse_watchlist(cfg.get("watchlist"))
        self.enabled = {
            "emergency": bool(cfg.get("alert_emergency", True)),
            "watchlist": bool(cfg.get("alert_watchlist", True)),
            "military": bool(cfg.get("alert_military", True)),
            "notable": bool(cfg.get("alert_notable", True)),
        }

    @staticmethod
    def _describe(ac, kind):
        who = ac.get("flight") or ac.get("reg") or (ac.get("hex") or "").upper()
        what = ac.get("type_long") or ac.get("desc") or ac.get("type") or ""
        where = "%.1f nm %s" % (ac.get("dist_nm") or 0.0,
                                (ac.get("spotter") or {}).get("compass") or "")
        if kind == "emergency":
            title = "Emergency squawk %s" % ac.get("squawk")
        elif kind == "watchlist":
            title = "Watchlist: %s" % who
        elif kind == "military":
            title = "Military: %s" % who
        else:
            title = "Notable: %s" % who
        return title, "%s %s, %s, %s" % (
            who, ("(" + what + ")") if what else "", where,
            "%d ft" % (ac.get("alt_ft") or 0))

    def evaluate(self, aircraft, now=None):
        """
        Run over the current picture. Returns the alerts that fired, and marks
        each aircraft with `watch` so the page can highlight it.
        """
        now = now or time.time()
        fresh = []

        with self.lock:
            for ac in aircraft:
                hexcode = ac.get("hex") or ""
                if not hexcode:
                    continue

                if matches_watchlist(ac, self.watchlist):
                    ac["watch"] = True
                    ac["interesting"] = True

                # The spotting log: one line the first time an aircraft is
                # seen, then nothing until it has been away for an hour.
                if now - self.seen.get(hexcode, 0) > REARM_AFTER_S:
                    self.spot_log.append({
                        "t": int(now),
                        "hex": hexcode,
                        "callsign": ac.get("flight") or "",
                        "reg": ac.get("reg") or "",
                        "type": ac.get("type") or "",
                        "airline": ((ac.get("route") or {}).get("airline") or {}).get("name", ""),
                        "dist_nm": round(ac.get("dist_nm") or 0.0, 1),
                    })
                self.seen[hexcode] = now

                for kind in self.KINDS:
                    if not self.enabled[kind]:
                        continue
                    if kind == "emergency" and not ac.get("emergency"):
                        continue
                    if kind == "watchlist" and not ac.get("watch"):
                        continue
                    if kind == "military" and not ac.get("military"):
                        continue
                    if kind == "notable" and not ac.get("notable"):
                        continue

                    key = (hexcode, kind)
                    if now - self.fired.get(key, 0) <= REARM_AFTER_S:
                        continue
                    self.fired[key] = now
                    self.counts[kind] += 1

                    title, message = self._describe(ac, kind)
                    fresh.append({
                        "t": int(now),
                        "kind": kind,
                        "title": title,
                        "message": message,
                        "hex": hexcode,
                        "callsign": ac.get("flight") or "",
                        "reg": ac.get("reg") or "",
                        "type": ac.get("type") or "",
                        "squawk": ac.get("squawk") or "",
                        "dist_nm": round(ac.get("dist_nm") or 0.0, 1),
                        "alt_ft": round(ac.get("alt_ft") or 0.0),
                    })

            # Forget aircraft long gone, so neither dict grows without bound
            # on a display that runs for months.
            for key in [k for k, t in self.seen.items() if now - t > 6 * REARM_AFTER_S]:
                del self.seen[key]
            for key in [k for k, t in self.fired.items() if now - t > 6 * REARM_AFTER_S]:
                del self.fired[key]

        for alert in fresh:
            self.alert_log.append(alert)
        if fresh and self.notifier and self.notifier.enabled:
            # Off the poller thread: a slow webhook must not delay the next
            # aircraft poll.
            threading.Thread(
                target=self._notify_all, args=(list(fresh),), daemon=True).start()
        return fresh

    def _notify_all(self, alerts):
        for alert in alerts:
            self.notifier.send(alert)
