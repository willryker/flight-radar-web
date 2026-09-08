# Flight Radar web

An ambient flight-tracking page for a wall display. Live ADS-B traffic around
one fixed location on a Leaflet map, with tap-to-inspect detail cards, weather
and radar overlays, and configurable alerts.

It is the companion to
[esp32-flight-radar](https://github.com/willryker/esp32-flight-radar), which
puts the same idea on a round 480×480 touchscreen. This one targets a browser
and a Raspberry Pi.

---

## Design constraints

**Standard library only.** The target is a Raspberry Pi 3B running whatever
Python 3 its distribution shipped. Building wheels on a 900 MB single-board
computer is a place where installs go to die, so there is nothing to install
beyond Python itself. Leaflet is vendored in `static/vendor/`.

**Background pollers, not per-request fetches.** Upstream data is gathered on
a timer and every browser refresh is answered out of memory. Two reasons: the
page stays fast, and the number of requests made to the free aggregators stays
flat no matter how many screens point at the box. `airplanes.live` rate limits
to one request per second, and a kiosk rotation reloads a page hard and often.

## Layout

    server.py              host process: static files + the JSON API
    flightradar/
      net.py               upstream fetching and rate limiting
      geo.py               distance, bearing, coordinate maths
      enrich.py            route / aircraft / timezone / media caches
      overlays.py          ISS, radiosondes, weather radar
      alerts.py            alert rules and the JSONL logs
    static/                the page itself, plus vendored Leaflet
    tools/build_airports.py  regenerates the airport overlay
    deploy/                systemd unit and installer

## Upstream services

All free, none requiring a key except where noted.

| Service | Used for |
|---|---|
| `airplanes.live`, `adsb.lol`, `adsb.fi` | live aircraft positions |
| `adsbdb.com` | route and aircraft type lookup |
| `open-meteo.com` | weather |
| `rainviewer.com` | precipitation radar |
| `wheretheiss.at` | ISS position |
| `sondehub.org` | radiosonde tracking |
| `openAIP` | airspace (needs a free key) |
| `ntfy.sh` | alert push notifications |

## Configuration

Everything that identifies one install lives in `config.json`, which is not in
this repo. That means the home coordinates, the API keys, the panel password
and the ntfy topic. The service reads it from `FLIGHT_RADAR_CONFIG`, and the deployment puts
it at `/etc/flight-radar/config.json`.

Settings are edited through the panel at `/panel.html` rather than by hand.
`panel_password` gates it; leave it empty and the panel is open, which is only
appropriate on a network you control.

## Airport overlay

`static/airports.json` and `static/airports/*.jpg` are not committed. They are
built for one location, and the aerial thumbnails come from Esri World Imagery,
which is free to fetch but not ours to redistribute. Build your own set:

    python3 tools/build_airports.py              # uses the location in config.json
    python3 tools/build_airports.py --lat 51.47 --lon -0.00 --radius 60

It is self-contained and stdlib-only, and pulls airport data from OurAirports,
street addresses from OpenStreetMap Nominatim, and aerials from Esri. The page
works without it; the airport overlay is simply empty.

## Install

    sudo deploy/install.sh

Installs to `/opt/flight-radar`, runs as a dedicated `flightradar` user, and
enables the systemd unit. The unit is deliberately confined: `ProtectSystem=strict`,
`PrivateDevices`, no new privileges, and write access to exactly two
directories. It is a static file server that talks to public APIs and has no
business anywhere else on the disk.

    systemctl status flight-radar
    journalctl -u flight-radar -f

## Licence

MIT. See `LICENSE`. Vendored Leaflet is BSD-2-Clause and carries its own terms.
