#!/bin/sh
#
# Install the flight radar web page as a systemd service.
#
# Safe to re-run: an existing /etc/flight-radar/config.json is left alone
# unless --force is given, so pulling down a new version of the page does not
# quietly move the radar back to Heathrow.
#
# Usage:
#   sudo ./deploy/install.sh --lat 51.4700 --lon -0.0015 --range 20 \
#                            --timezone Europe/London --temp-unit celsius
#
set -eu

PREFIX=/opt/flight-radar
STATE=/var/lib/flight-radar
CONFDIR=/etc/flight-radar
SERVICE=flight-radar

LAT=51.4700
LON=-0.4543
RANGE=20
TIMEZONE=null
TEMP_UNIT=celsius
UNITS=aviation
PORT=8080
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --lat)       LAT=$2; shift 2 ;;
        --lon)       LON=$2; shift 2 ;;
        --range)     RANGE=$2; shift 2 ;;
        --timezone)  TIMEZONE=\"$2\"; shift 2 ;;
        --temp-unit) TEMP_UNIT=$2; shift 2 ;;
        --units)     UNITS=$2; shift 2 ;;
        --port)      PORT=$2; shift 2 ;;
        --force)     FORCE=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }

# The payload root is the directory above this script.
SRC=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$SRC/server.py" ] || { echo "server.py not found beside $SRC" >&2; exit 1; }

echo "==> installing from $SRC"

if ! command -v python3 >/dev/null 2>&1; then
    echo "==> installing python3"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends python3
fi

if ! id flightradar >/dev/null 2>&1; then
    echo "==> creating the flightradar user"
    useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin flightradar
fi

echo "==> copying files to $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX" "$STATE" "$CONFDIR"
cp -r "$SRC/server.py" "$SRC/flightradar" "$SRC/static" "$PREFIX/"

# Byte-compiled leftovers from a development machine would shadow the modules
# we just copied if their timestamps happen to look current.
find "$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root "$PREFIX"

# Do not inherit the source tree's modes. The payload is usually copied from a
# Windows machine, which has no POSIX permission bits to preserve, so scp
# invents them -- in practice 0700 on directories. Everything then installs
# looking correct while the unprivileged flightradar user cannot read
# static/, and the page answers 404 for every file while the API still works.
find "$PREFIX" -type d -exec chmod 755 {} +
find "$PREFIX" -type f -exec chmod 644 {} +
chown -R flightradar:flightradar "$STATE"

if [ ! -f "$CONFDIR/config.json" ] || [ "$FORCE" = "1" ]; then
    echo "==> writing $CONFDIR/config.json"
    cat > "$CONFDIR/config.json" <<EOF
{
  "port": $PORT,
  "lat": $LAT,
  "lon": $LON,
  "range_nm": $RANGE,
  "units": "$UNITS",
  "temperature_unit": "$TEMP_UNIT",
  "timezone": $TIMEZONE,
  "label_count": 10,
  "poll_interval_s": 5,
  "show_weather": true,
  "show_precipitation": true,
  "precipitation_opacity": 0.5,
  "route_lookup": true,
  "cache_dir": "$STATE"
}
EOF
else
    echo "==> keeping the existing $CONFDIR/config.json (pass --force to overwrite)"
fi
# Owned by the service user because the settings panel writes it back.
chown flightradar:flightradar "$CONFDIR" "$CONFDIR/config.json"
chmod 644 "$CONFDIR/config.json"

echo "==> installing the systemd unit"
cp "$SRC/deploy/flight-radar.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "==> running: http://${IP:-localhost}:$PORT/"
else
    echo "==> FAILED to start; last log lines:" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi
