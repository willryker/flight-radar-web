/*
 * Ambient flight radar.
 *
 * Leaflet draws the basemap and the precipitation overlay and then gets out
 * of the way -- it is never panned or zoomed. Aircraft, labels and the range
 * ring are drawn onto one canvas above it. A marker per aircraft would mean
 * creating and destroying a few hundred DOM nodes every poll, forever, on a
 * screen nobody is watching closely enough to notice it degrade.
 */

'use strict';

// Past this the aircraft has probably turned and extrapolating it further
// does more harm than good. Same cut-off as the firmware.
var MAX_EXTRAPOLATE_S = 25;

var FRAME_MS = 80;            // ~12 fps; the targets move a pixel a second
var STALE_TRACK_MS = 300000;  // forget smoothing state after five minutes
var PLANE_SCALE = 13;
var RADAR_MAX_NATIVE_ZOOM = 7;

// The card follows whatever is closest to the centre, and shows nothing at
// all when the nearest aircraft is further out than this. A card about
// something twelve miles away is not about anything you can see.
var DETAIL_RANGE_NM = 5;

// A challenger has to be this much closer before it takes the card over, so
// two aircraft at similar range do not trade it back and forth every frame.
var DETAIL_STICKY_NM = 0.4;

// How long a click holds the card on one aircraft: long enough to finish
// looking at the sky.
var PIN_MS = 60000;

var GOLD = '#ffcf4d';         // military, notable heavies, watchlist matches

// Altitude bands, straight from altitude_colour() in main/ui.c.
function altitudeColour(ac) {
  if (ac.on_ground) { return '#9aa6a2'; }
  var ft = ac.alt_ft;
  if (ft < 5000)  { return '#e8443a'; }
  if (ft < 10000) { return '#f08c2e'; }
  if (ft < 20000) { return '#f2d43c'; }
  if (ft < 30000) { return '#6fd44e'; }
  if (ft < 40000) { return '#35c1e0'; }
  return '#8a93ff';
}

// Classic plan-view airliner, nose at -y, drawn in unit space and scaled.
var PLANE = [
  [0.00, -1.00], [0.09, -0.74], [0.10, -0.20], [0.98, 0.24], [0.98, 0.42],
  [0.10, 0.20], [0.10, 0.62], [0.36, 0.84], [0.36, 0.98], [0.00, 0.84],
  [-0.36, 0.98], [-0.36, 0.84], [-0.10, 0.62], [-0.10, 0.20],
  [-0.98, 0.42], [-0.98, 0.24], [-0.10, -0.20], [-0.09, -0.74]
];

// Helicopter, same unit space and same nose-at--y convention: a cabin, a thin
// tail boom, and a horizontal stabiliser at the end of it.
var HELI = [
  [0.00, -0.80], [0.22, -0.55], [0.28, -0.10], [0.26, 0.30], [0.14, 0.52],
  [0.09, 0.62], [0.09, 1.10], [0.26, 1.14], [0.26, 1.30],
  [-0.26, 1.30], [-0.26, 1.14], [-0.09, 1.10], [-0.09, 0.62],
  [-0.14, 0.52], [-0.26, 0.30], [-0.28, -0.10], [-0.22, -0.55]
];
var HELI_ROTOR_R = 1.22;   // blade radius, wider than the cabin is long
var HELI_HUB_Y = 0.02;     // hub sits just behind the cabin's midpoint
var HELI_BLADES = [34, 124];

var EARTH_RADIUS_NM = 3440.065;
var DEG = Math.PI / 180;

/* Forward projection along a great circle -- geo_project_forward() in main/geo.c. */
function projectForward(lat, lon, bearing, distNm) {
  var d = distNm / EARTH_RADIUS_NM;
  var t = bearing * DEG;
  var p1 = lat * DEG;
  var l1 = lon * DEG;
  var sinP2 = Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(t);
  var p2 = Math.asin(sinP2);
  var l2 = l1 + Math.atan2(Math.sin(t) * Math.sin(d) * Math.cos(p1),
                           Math.cos(d) - Math.sin(p1) * sinP2);
  return [p2 / DEG, l2 / DEG];
}

/*
 * Points along the great circle between two airports.
 *
 * A straight line between them on a Mercator map is not the route; for
 * anything transcontinental it is visibly and embarrassingly wrong. Same
 * interpolation as great_circle_points() in flightradar/geo.py.
 */
function greatCirclePoints(lat1, lon1, lat2, lon2, segments) {
  var p1 = lat1 * DEG, l1 = lon1 * DEG;
  var p2 = lat2 * DEG, l2 = lon2 * DEG;
  var d = 2 * Math.asin(Math.sqrt(
    Math.pow(Math.sin((p2 - p1) / 2), 2) +
    Math.cos(p1) * Math.cos(p2) * Math.pow(Math.sin((l2 - l1) / 2), 2)));
  if (!d) { return [[lat1, lon1]]; }

  var out = [];
  for (var i = 0; i <= segments; i++) {
    var f = i / segments;
    var a = Math.sin((1 - f) * d) / Math.sin(d);
    var b = Math.sin(f * d) / Math.sin(d);
    var x = a * Math.cos(p1) * Math.cos(l1) + b * Math.cos(p2) * Math.cos(l2);
    var y = a * Math.cos(p1) * Math.sin(l1) + b * Math.cos(p2) * Math.sin(l2);
    var z = a * Math.sin(p1) + b * Math.sin(p2);
    out.push([Math.atan2(z, Math.hypot(x, y)) / DEG, Math.atan2(y, x) / DEG]);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function isMetric() { return config && config.units === 'metric'; }

function fmtAlt(ac) {
  if (ac.on_ground) { return 'ground'; }
  if (!ac.alt_ft) { return '—'; }
  return isMetric()
    ? Math.round(ac.alt_ft * 0.3048).toLocaleString() + ' m'
    : Math.round(ac.alt_ft).toLocaleString() + ' ft';
}

function fmtSpeed(kt) {
  if (!kt) { return '—'; }
  return isMetric() ? Math.round(kt * 1.852) + ' km/h' : Math.round(kt) + ' kt';
}

function fmtDist(nm) {
  if (nm === null || nm === undefined) { return '—'; }
  return isMetric() ? (nm * 1.852).toFixed(1) + ' km' : nm.toFixed(1) + ' nm';
}

function fmtRate(fpm) {
  if (!fpm) { return 'level'; }
  var v = isMetric() ? Math.round(fpm * 0.00508 * 10) / 10 : Math.round(fpm);
  return (fpm > 0 ? '+' : '') + v.toLocaleString() + (isMetric() ? ' m/s' : ' fpm');
}

function fmtMinutes(min) {
  if (min === null || min === undefined) { return ''; }
  if (min < 60) { return Math.round(min) + ' min'; }
  return Math.floor(min / 60) + ' h ' + Math.round(min % 60) + ' min';
}

function escapeHtml(s) {
  return String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

/*
 * ISO 3166-1 alpha-2 as a small chip.
 *
 * Emoji flags were the obvious first answer and the wrong one: they need a
 * flag-capable emoji font, which Raspberry Pi OS Lite and most kiosk images
 * do not have. The fallback there is the bare regional-indicator letters, so
 * "US N974VV" came out looking like a typo rather than a design. Two capital
 * letters in a chip render identically everywhere and read as deliberate.
 */
function ccChip(iso) {
  if (!iso || !/^[A-Za-z]{2}$/.test(iso)) { return ''; }
  return '<span class="cc">' + escapeHtml(iso.toUpperCase()) + '</span>';
}

/* Local wall-clock at the destination, from an epoch and that place's offset. */
function clockAt(epoch, offsetSeconds) {
  var d = new Date((epoch + (offsetSeconds || 0)) * 1000);
  return String(d.getUTCHours()).padStart(2, '0') + ':' +
         String(d.getUTCMinutes()).padStart(2, '0');
}

// ---------------------------------------------------------------------------
// Weather presentation
// ---------------------------------------------------------------------------

var SVG = {
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/></svg>',
  cloud: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.8 19q-2 0-3.4-1.4T2 14.2q0-1.8 1.2-3.2t3-1.6q.6-2.2 2.4-3.6T12.8 4.4q2.6 0 4.4 1.8t1.8 4.4v.3q1.6.2 2.6 1.4t1 2.8q0 1.7-1.2 2.8T18.5 19z"/></svg>',
  partly: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="8" cy="8" r="3.2"/><path d="M8 1.8v1.6M1.8 8h1.6M3.6 3.6l1.1 1.1M12.4 3.6l-1.1 1.1"/><path d="M8.4 19.4q-1.7 0-2.9-1.2t-1.2-2.9q0-1.5 1-2.6t2.5-1.4q.5-1.8 2-3t3.5-1.2q2.2 0 3.8 1.5t1.6 3.8v.2q1.3.2 2.2 1.2t.8 2.4q0 1.4-1 2.4t-2.4 1z" fill="currentColor" stroke="none"/></svg>',
  rain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6.8 15.5q-1.9 0-3.2-1.3T2.3 11q0-1.7 1.1-3t2.8-1.5q.6-2 2.3-3.3t3.9-1.3q2.4 0 4.1 1.7t1.7 4.1v.2q1.5.2 2.5 1.3t.9 2.6q0 1.6-1.1 2.6t-2.7 1.1z" fill="currentColor" stroke="none"/><path d="M8 18.2l-1 2.6M12.4 18.2l-1 2.6M16.8 18.2l-1 2.6"/></svg>',
  snow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6.8 15.5q-1.9 0-3.2-1.3T2.3 11q0-1.7 1.1-3t2.8-1.5q.6-2 2.3-3.3t3.9-1.3q2.4 0 4.1 1.7t1.7 4.1v.2q1.5.2 2.5 1.3t.9 2.6q0 1.6-1.1 2.6t-2.7 1.1z" fill="currentColor" stroke="none"/><path d="M8 19h.01M12 20.4h.01M16 19h.01M10 21.6h.01M14 17.8h.01"/></svg>',
  fog: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 8.5h18M5 12.5h14M3 16.5h18M7 20.5h10"/></svg>',
  storm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6.8 14.6q-1.9 0-3.2-1.3T2.3 10.1q0-1.7 1.1-3t2.8-1.5q.6-2 2.3-3.3T12.4 1q2.4 0 4.1 1.7t1.7 4.1v.2q1.5.2 2.5 1.3t.9 2.6q0 1.6-1.1 2.6t-2.7 1.1z" fill="currentColor" stroke="none"/><path d="M12.6 16l-3 4h3.6l-2.4 3.4" stroke-width="1.6"/></svg>'
};

// WMO 4677 codes, collapsed to what a glance needs.
function describeWeather(code) {
  if (code === 0) { return ['Clear', 'sun']; }
  if (code === 1) { return ['Mainly clear', 'partly']; }
  if (code === 2) { return ['Partly cloudy', 'partly']; }
  if (code === 3) { return ['Overcast', 'cloud']; }
  if (code === 45 || code === 48) { return ['Fog', 'fog']; }
  if (code >= 51 && code <= 57) { return ['Drizzle', 'rain']; }
  if (code >= 61 && code <= 67) { return ['Rain', 'rain']; }
  if (code >= 71 && code <= 77) { return ['Snow', 'snow']; }
  if (code >= 80 && code <= 82) { return ['Showers', 'rain']; }
  if (code === 85 || code === 86) { return ['Snow showers', 'snow']; }
  if (code >= 95) { return ['Thunderstorm', 'storm']; }
  return ['', 'cloud'];
}

var CARDINALS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

function cardinal(deg) {
  return CARDINALS[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

var data = null;          // last good /api/state payload
var dataClientAt = 0;     // performance.now() when it arrived
var fetchFailures = 0;
var config = null;
var map = null;
var baseLayer = null;
var radarLayer = null;
var radarPath = null;

var canvas = document.getElementById('scope');
var ctx = canvas.getContext('2d');

// Rendered position per aircraft, eased toward the computed one so a new poll
// never makes a target jump.
var tracks = new Map();

// Which aircraft the card is showing. `pinned` is a deliberate click and wins
// until it expires or leaves; otherwise it is whatever is nearest the centre.
var pinnedHex = null;
var pinnedAt = 0;
var spotHex = null;

// ---------------------------------------------------------------------------
// Spotlight
// ---------------------------------------------------------------------------

/*
 * Everything currently eligible for the card, nearest first.
 *
 * Aircraft on the ground are left out. There is a field within a few miles of
 * most places worth putting one of these, and a parked Cessna is both the
 * closest thing to the centre and the least interesting thing in the sky.
 */
function candidates() {
  if (!data) { return []; }
  return data.aircraft
    .filter(function (ac) { return ac.hex && !ac.on_ground; })
    .sort(function (a, b) { return (a.dist_nm || 0) - (b.dist_nm || 0); });
}

function byHex(hex) {
  if (!hex || !data) { return null; }
  for (var i = 0; i < data.aircraft.length; i++) {
    if (data.aircraft[i].hex === hex) { return data.aircraft[i]; }
  }
  return null;
}

/*
 * The aircraft the card should be showing right now.
 *
 * A click wins while it lasts. Otherwise it is simply whatever is closest to
 * the centre, and nothing at all when the nearest is further out than
 * DETAIL_RANGE_NM -- an empty map says "nothing overhead" more honestly than
 * a card about an aircraft twelve miles away.
 */
function currentSubject(nowMs) {
  if (pinnedHex) {
    var pinned = byHex(pinnedHex);
    // Drop the pin when it expires or when the aircraft leaves the area --
    // holding a card for something that is no longer there is worse than
    // moving on.
    if (pinned && nowMs - pinnedAt < PIN_MS) { return pinned; }
    pinnedHex = null;
  }

  var list = candidates();
  if (!list.length || (list[0].dist_nm || 0) > DETAIL_RANGE_NM) {
    spotHex = null;
    return null;
  }

  // Stay with the current one until something is clearly closer. Two aircraft
  // passing at similar range would otherwise trade the card back and forth
  // several times a second, which is unreadable and looks broken.
  var held = spotHex ? byHex(spotHex) : null;
  if (held && !held.on_ground && (held.dist_nm || 0) <= DETAIL_RANGE_NM &&
      (held.dist_nm || 0) - (list[0].dist_nm || 0) < DETAIL_STICKY_NM) {
    return held;
  }

  spotHex = list[0].hex;
  return list[0];
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

function setText(id, value) {
  document.getElementById(id).textContent = value || '';
}

function updateDetail(subject) {
  var card = document.getElementById('detail');
  if (!subject) { card.hidden = true; return; }
  card.hidden = false;
  card.classList.toggle('pinned', !!pinnedHex);

  var route = subject.route || {};
  var airline = route.airline || {};

  var photo = document.getElementById('d-photo');
  if (config.show_photos && subject.has_photo) {
    var want = '/api/photo/' + subject.hex + '.jpg';
    // Only reassign when it actually changes: setting src to the same URL
    // still restarts the load and makes the card flicker.
    if (photo.getAttribute('data-hex') !== subject.hex) {
      photo.setAttribute('data-hex', subject.hex);
      photo.src = want;
      photo.hidden = false;
    }
  } else {
    photo.hidden = true;
    photo.removeAttribute('data-hex');
    photo.removeAttribute('src');
  }

  var logo = document.getElementById('d-logo');
  if (config.show_logos && airline.icao) {
    if (logo.getAttribute('data-icao') !== airline.icao) {
      logo.setAttribute('data-icao', airline.icao);
      logo.src = '/api/logo/' + airline.icao + '.png';
      logo.hidden = false;
    }
  } else {
    logo.hidden = true;
    logo.removeAttribute('data-icao');
    logo.removeAttribute('src');
  }

  setText('d-callsign', subject.flight || subject.reg ||
                        (subject.hex || '').replace('~', '').toUpperCase());
  setText('d-airline', airline.name || subject.owner || '');
  setText('d-type', subject.type_long || subject.desc ||
                    [subject.manufacturer, subject.type].filter(Boolean).join(' '));

  var badges = [];
  if (subject.emergency) { badges.push(['alert', 'SQUAWK ' + subject.squawk]); }
  if (subject.military) { badges.push(['gold', 'MIL']); }
  if (subject.notable) { badges.push(['gold', 'NOTABLE']); }
  if (subject.watch) { badges.push(['gold', 'WATCH']); }
  if (subject.radio) { badges.push(['radio', '◆ HEARD HERE']); }
  document.getElementById('d-badges').innerHTML = badges.map(function (b) {
    return '<span class="d-badge ' + b[0] + '">' + b[1] + '</span>';
  }).join('');

  // Route. Only shown when the lookup survived the plausibility check --
  // route.from is absent for a rejected one, and a wrong route on screen is
  // worse than none.
  var routeEl = document.getElementById('d-route');
  if (route.from && route.to) {
    routeEl.hidden = false;
    setText('d-from-code', route.from.iata || route.from.icao || '???');
    setText('d-to-code', route.to.iata || route.to.icao || '???');
    document.getElementById('d-from-city').innerHTML =
      ccChip(route.from.country) + escapeHtml(route.from.city || '');
    document.getElementById('d-to-city').innerHTML =
      escapeHtml(route.to.city || '') + ccChip(route.to.country);

    var pct = Math.round((route.progress || 0) * 100);
    document.getElementById('d-bar-fill').style.width = pct + '%';

    var bits = [];
    if (route.progress !== undefined) { bits.push(pct + '%'); }
    if (route.remaining_nm !== undefined) { bits.push(fmtDist(route.remaining_nm) + ' to go'); }
    if (route.eta_min !== undefined) {
      var eta = '~' + fmtMinutes(route.eta_min);
      if (route.arrival_epoch && route.arrival_offset_s !== undefined) {
        eta += ' (' + clockAt(route.arrival_epoch, route.arrival_offset_s) + ' local)';
      }
      bits.push(eta);
    }
    setText('d-progress', bits.join('  ·  '));
  } else {
    routeEl.hidden = true;
  }

  // Where to look, and whether it is coming this way.
  var spot = document.getElementById('d-spot');
  var spotBits = [];
  if (subject.spotter && subject.spotter.elev_deg !== null &&
      subject.spotter.elev_deg !== undefined) {
    spotBits.push('look ' + subject.spotter.compass + ' ' +
                  Math.round(subject.spotter.elev_deg) + '° up');
  }
  if (subject.cpa) {
    spotBits.push(subject.cpa.minutes < 1
      ? 'overhead now, ' + fmtDist(subject.cpa.miss_nm)
      : 'closest in ' + fmtMinutes(subject.cpa.minutes) + ', ' + fmtDist(subject.cpa.miss_nm));
  }
  spot.hidden = !spotBits.length;
  spot.textContent = spotBits.join('  ·  ');

  // Reception from our own antenna (readsb on the Pi). Absent means the
  // aggregator saw it but our radio did not -- usually out of range.
  var radioEl = document.getElementById('d-radio');
  if (subject.radio) {
    var rb = ['◆ RECEIVED HERE'];
    rb.push(subject.radio.rssi.toFixed(1) + ' dBFS');
    rb.push(subject.radio.messages + ' msg' + (subject.radio.messages === 1 ? '' : 's'));
    if (subject.radio.age_s >= 1) { rb.push(Math.round(subject.radio.age_s) + 's ago'); }
    radioEl.textContent = rb.join('  ·  ');
    radioEl.hidden = false;
  } else {
    radioEl.hidden = true;
  }

  setText('d-alt', fmtAlt(subject));
  setText('d-spd', fmtSpeed(subject.gs_kt));
  setText('d-vs', fmtRate(subject.baro_rate));
  setText('d-dist', fmtDist(subject.dist_nm));
  setText('d-trk', subject.track_deg ? Math.round(subject.track_deg) + '°' : '—');
  document.getElementById('d-reg').innerHTML =
    ccChip(subject.reg_country) + escapeHtml(subject.reg || '—');

  var foot = [];
  if (subject.squawk) { foot.push('Squawk ' + subject.squawk); }
  if (subject.category) { foot.push('Cat. ' + subject.category); }
  if (route.flight_iata) { foot.push(route.flight_iata); }
  if (subject.hex) { foot.push(subject.hex.toUpperCase()); }
  setText('d-foot', foot.join('  ·  '));
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

function retina(url) {
  // Ask for @2x tiles but keep a 256 px tile size. The browser downsamples,
  // which keeps the basemap sharp at the fractional zoom that fitBounds
  // picks -- Leaflet would otherwise upscale a lower zoom level and smear it.
  return url.replace('{r}', '@2x');
}

function initMap() {
  map = L.map('map', {
    zoomControl: false,
    attributionControl: true,
    zoomSnap: 0,
    // Ambient display: nothing about this map is meant to be touched.
    dragging: false,
    touchZoom: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    tap: false,
    inertia: false
  });

  baseLayer = L.tileLayer(retina(config.basemap_url), {
    tileSize: 256,
    maxZoom: 19,
    keepBuffer: 4,
    updateWhenIdle: true,
    attribution: '&copy; OpenStreetMap &middot; &copy; CARTO'
  }).addTo(map);

  map.attributionControl.setPrefix('');
  fitScope();
}

/* Frame the range circle with a little air around it, whatever the screen. */
function fitScope() {
  var r = config.range_nm;
  var n = projectForward(config.lat, config.lon, 0, r);
  var e = projectForward(config.lat, config.lon, 90, r);
  var s = projectForward(config.lat, config.lon, 180, r);
  var w = projectForward(config.lat, config.lon, 270, r);
  map.fitBounds(L.latLngBounds([s[0], w[1]], [n[0], e[1]]), {
    padding: [40, 40],
    animate: false
  });
}

function updateRadarLayer() {
  if (!config.show_precipitation) { return; }
  var radar = data && data.radar;
  if (!radar || !radar.path || radar.path === radarPath) { return; }

  var url = radar.host + radar.path + '/512/{z}/{x}/{y}/4/1_1.png';
  var next = L.tileLayer(url, {
    tileSize: 256,
    opacity: 0,
    maxZoom: 19,
    // RainViewer only publishes composites to zoom 7. Ask for anything
    // deeper and it answers 200 with a "Zoom Level Not Supported" placeholder
    // -- which is a picture, so it paints happily over the map. Cap the
    // native zoom and let Leaflet upscale; the radar is ~1 km data anyway.
    maxNativeZoom: RADAR_MAX_NATIVE_ZOOM,
    updateWhenIdle: true,
    attribution: 'RainViewer'
  }).addTo(map);

  var previous = radarLayer;
  radarLayer = next;
  radarPath = radar.path;

  // Cross-fade rather than swap, so a new radar frame does not blink the
  // whole screen. Only drop the old layer once the new one has its tiles.
  next.once('load', function () {
    next.setOpacity(config.precipitation_opacity);
    if (previous) { map.removeLayer(previous); }
  });
  // If the frame never loads, do not leak the layer either.
  setTimeout(function () {
    if (map.hasLayer(next) && next.options.opacity === 0) {
      next.setOpacity(config.precipitation_opacity);
      if (previous && map.hasLayer(previous)) { map.removeLayer(previous); }
    }
  }, 8000);
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

function poll() {
  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); }, 9000);

  fetch('/api/state', { signal: controller.signal, cache: 'no-store' })
    .then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    })
    .then(function (payload) {
      data = payload;
      dataClientAt = performance.now();
      fetchFailures = 0;
      if (!config) {
        config = payload.config;
        initMap();
      }
      updateRadarLayer();
      updateWeather();
      updateIss();
      updateStatus();
    })
    .catch(function () {
      fetchFailures++;
      updateStatus();
    })
    .then(function () {
      clearTimeout(timer);
      var interval = (config ? config.poll_interval_s : 5) * 1000;
      setTimeout(poll, fetchFailures ? Math.min(30000, interval * fetchFailures) : interval);
    });
}

function updateWeather() {
  var el = document.getElementById('weather');
  var wx = data.weather;
  if (!wx || wx.temp === null || wx.temp === undefined) {
    el.hidden = true;
    return;
  }
  var described = describeWeather(wx.code);
  document.getElementById('weather-icon').innerHTML = SVG[described[1]] || SVG.cloud;
  document.getElementById('weather-temp').textContent =
    Math.round(wx.temp) + '°' + wx.temp_unit;

  var wind = document.getElementById('weather-wind');
  if (wx.wind_speed === null || wx.wind_speed === undefined) {
    wind.textContent = '';
  } else {
    // Meteorological wind direction is where it blows *from*; the arrow
    // points the way the air is going, hence the 180.
    wind.innerHTML = '<span class="arrow" style="transform:rotate(' +
      ((wx.wind_dir || 0) + 180) + 'deg)">↑</span>' +
      cardinal(wx.wind_dir || 0) + ' ' + Math.round(wx.wind_speed);
  }
  document.getElementById('weather-text').textContent = described[0];
  el.hidden = false;
}

function updateStatus() {
  var dot = document.getElementById('status-dot');
  var text = document.getElementById('status-text');
  dot.className = 'dot';

  if (!data) {
    dot.classList.add('warn');
    text.textContent = 'acquiring';
    return;
  }
  var health = data.health;
  var parts = [health.count + (health.count === 1 ? ' aircraft' : ' aircraft')];
  if (health.source) { parts.push(health.source); }

  if (fetchFailures) {
    dot.classList.add('stale');
    parts.push('page offline');
  } else if (health.stale) {
    dot.classList.add('stale');
    parts.push('feed stale');
  } else if (health.failures) {
    dot.classList.add('warn');
  }
  text.textContent = parts.join('  ·  ');
}

// ---------------------------------------------------------------------------
// Clock
// ---------------------------------------------------------------------------

function tickClock() {
  var opts = { hour: '2-digit', minute: '2-digit', hour12: false };
  if (config && config.timezone) { opts.timeZone = config.timezone; }
  var out;
  try {
    out = new Intl.DateTimeFormat([], opts).format(new Date());
  } catch (e) {
    // A bad IANA name in the config should cost the clock its timezone, not
    // the whole page.
    out = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', hour12: false })
      .format(new Date());
  }
  document.getElementById('clock-time').textContent = out;
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

function resizeCanvas() {
  var dpr = window.devicePixelRatio || 1;
  var w = window.innerWidth;
  var h = window.innerHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function drawRangeRing() {
  var centre = map.latLngToContainerPoint(L.latLng(config.lat, config.lon));
  var north = projectForward(config.lat, config.lon, 0, config.range_nm);
  var edge = map.latLngToContainerPoint(L.latLng(north[0], north[1]));
  var radius = Math.abs(centre.y - edge.y);

  ctx.save();
  ctx.strokeStyle = 'rgba(39, 209, 158, 0.38)';
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.arc(centre.x, centre.y, radius, 0, Math.PI * 2);
  ctx.stroke();

  // Range call-out, sitting on the ring at twelve o'clock.
  var label = config.range_nm + (config.units === 'metric' ? ' KM' : ' NM');
  ctx.font = '600 10px Inter, Roboto, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  var w = ctx.measureText(label).width + 12;
  ctx.fillStyle = 'rgba(3, 7, 10, 0.75)';
  roundRect(centre.x - w / 2, centre.y - radius - 8, w, 16, 3);
  ctx.fill();
  ctx.fillStyle = 'rgba(39, 209, 158, 0.75)';
  ctx.fillText(label, centre.x, centre.y - radius);

  // Home.
  ctx.strokeStyle = 'rgba(39, 209, 158, 0.9)';
  ctx.lineWidth = 1.3;
  ctx.beginPath();
  ctx.arc(centre.x, centre.y, 4.5, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(centre.x - 9, centre.y);
  ctx.lineTo(centre.x - 6, centre.y);
  ctx.moveTo(centre.x + 6, centre.y);
  ctx.lineTo(centre.x + 9, centre.y);
  ctx.moveTo(centre.x, centre.y - 9);
  ctx.lineTo(centre.x, centre.y - 6);
  ctx.moveTo(centre.x, centre.y + 6);
  ctx.lineTo(centre.x, centre.y + 9);
  ctx.stroke();
  ctx.restore();
}

/*
 * Build a polygon in unit space, rotated to the track and scaled.
 *
 * The transform is undone before stroking on purpose: leaving it in place
 * would scale the line width with the icon, and a 13x outline is a blob.
 */
function shapePath(points, x, y, track) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(track * DEG);
  ctx.scale(PLANE_SCALE, PLANE_SCALE);
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  for (var i = 1; i < points.length; i++) {
    ctx.lineTo(points[i][0], points[i][1]);
  }
  ctx.closePath();
  ctx.restore();
}

/* Dark rim first, so the silhouette still reads over a bright radar echo. */
function rimAndFill(colour) {
  ctx.lineJoin = 'round';
  ctx.strokeStyle = 'rgba(0, 0, 0, 0.85)';
  ctx.lineWidth = 2.4;
  ctx.stroke();
  ctx.fillStyle = colour;
  ctx.fill();
}

function highlightRing(x, y, emergency, gold) {
  if (!emergency && !gold) { return; }
  // Kept as a ring rather than a fill: altitude is what the colour of an
  // aircraft means here, and recolouring one to say something else would
  // break that meaning everywhere.
  ctx.strokeStyle = emergency ? '#ff4d4d' : GOLD;
  ctx.lineWidth = emergency ? 1.6 : 1.3;
  ctx.beginPath();
  ctx.arc(x, y, PLANE_SCALE + 6, 0, Math.PI * 2);
  ctx.stroke();
}

function drawPlane(x, y, track, colour, emergency, gold) {
  shapePath(PLANE, x, y, track);
  rimAndFill(colour);
  highlightRing(x, y, emergency, gold);
}

/*
 * A helicopter reads as a helicopter from across a room only if the rotor is
 * there. The body alone is a lozenge with a tail, which at this size is just
 * an aeroplane that has lost its wings.
 */
function drawHelicopter(x, y, track, colour, emergency, gold) {
  shapePath(HELI, x, y, track);
  rimAndFill(colour);

  // Two blades crossing at the hub. Deliberately not aligned to the fuselage
  // -- a rotor square to the body looks like a cross painted on the roof.
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(track * DEG);
  ctx.scale(PLANE_SCALE, PLANE_SCALE);
  ctx.beginPath();
  for (var b = 0; b < HELI_BLADES.length; b++) {
    var a = HELI_BLADES[b] * DEG;
    var dx = Math.cos(a) * HELI_ROTOR_R;
    var dy = Math.sin(a) * HELI_ROTOR_R;
    ctx.moveTo(-dx, HELI_HUB_Y - dy);
    ctx.lineTo(dx, HELI_HUB_Y + dy);
  }
  ctx.restore();

  ctx.lineCap = 'round';
  ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
  ctx.lineWidth = 2.6;
  ctx.stroke();
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.1;
  ctx.stroke();
  ctx.lineCap = 'butt';

  highlightRing(x, y, emergency, gold);
}

/*
 * The path an aircraft has flown, fading out behind it.
 *
 * Drawn segment by segment rather than as one path: a single stroke with one
 * alpha reads as a tail of equal weight all the way back, which fights the
 * eye for attention with the aircraft itself.
 */
function drawTrail(ac, colour, emphasis) {
  var pts = ac.trail;
  if (!pts || pts.length < 2) { return; }

  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = colour;

  var prev = map.latLngToContainerPoint(L.latLng(pts[0][0], pts[0][1]));
  for (var i = 1; i < pts.length; i++) {
    var cur = map.latLngToContainerPoint(L.latLng(pts[i][0], pts[i][1]));
    var f = i / (pts.length - 1);          // 0 at the oldest point, 1 at newest
    ctx.globalAlpha = Math.min(1, (0.14 + 0.5 * f) * emphasis);
    ctx.lineWidth = (1.1 + 1.6 * f) * emphasis;
    ctx.beginPath();
    ctx.moveTo(prev.x, prev.y);
    ctx.lineTo(cur.x, cur.y);
    ctx.stroke();
    prev = cur;
  }
  ctx.restore();
}

/* The great circle the selected aircraft is flying, with its airports marked. */
function drawRouteLine(ac) {
  var route = ac && ac.route;
  if (!route || !route.from || !route.to) { return; }
  if (route.from.lat === null || route.to.lat === null) { return; }

  var pts = greatCirclePoints(route.from.lat, route.from.lon,
                              route.to.lat, route.to.lon, 96);
  ctx.save();
  ctx.strokeStyle = 'rgba(39, 209, 158, 0.55)';
  ctx.lineWidth = 1.3;
  ctx.setLineDash([5, 5]);
  ctx.beginPath();
  for (var i = 0; i < pts.length; i++) {
    var p = map.latLngToContainerPoint(L.latLng(pts[i][0], pts[i][1]));
    if (i === 0) { ctx.moveTo(p.x, p.y); } else { ctx.lineTo(p.x, p.y); }
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // Airport ends, when they happen to be on screen at all. At 20 nm they
  // usually are not, and the line simply runs off the edge -- which is the
  // honest picture of a flight that started 900 miles away.
  ctx.font = '600 10px Inter, Roboto, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  [[route.from, route.from.iata], [route.to, route.to.iata]].forEach(function (end) {
    var port = end[0];
    var p = map.latLngToContainerPoint(L.latLng(port.lat, port.lon));
    if (p.x < 0 || p.y < 0 || p.x > window.innerWidth || p.y > window.innerHeight) {
      return;
    }
    ctx.fillStyle = 'rgba(39, 209, 158, 0.9)';
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillText(end[1] || '', p.x, p.y - 11);
  });
  ctx.restore();
}

/* A ring around the aircraft the card is currently talking about. */
function drawSelection(x, y, colour) {
  ctx.save();
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.4;
  ctx.globalAlpha = 0.9;
  ctx.beginPath();
  ctx.arc(x, y, PLANE_SCALE + 9, 0, Math.PI * 2);
  ctx.stroke();

  // Four ticks, so the ring reads as a reticle rather than a bubble.
  ctx.globalAlpha = 0.75;
  var r0 = PLANE_SCALE + 5;
  var r1 = PLANE_SCALE + 13;
  for (var a = 0; a < 4; a++) {
    var t = a * Math.PI / 2;
    ctx.beginPath();
    ctx.moveTo(x + Math.cos(t) * r0, y + Math.sin(t) * r0);
    ctx.lineTo(x + Math.cos(t) * r1, y + Math.sin(t) * r1);
    ctx.stroke();
  }
  ctx.restore();
}

// ---------------------------------------------------------------------------
// Overlays
// ---------------------------------------------------------------------------

// Restricted, danger and prohibited areas are worth a warmer colour than the
// controlled airspace you would expect to see around any airport.
var AIRSPACE_COLOURS = {
  R: 'rgba(255, 140, 60, 0.55)', D: 'rgba(255, 140, 60, 0.55)',
  P: 'rgba(255, 90, 90, 0.6)', TRA: 'rgba(255, 140, 60, 0.45)',
  MCTR: 'rgba(150, 160, 255, 0.45)', MTMA: 'rgba(150, 160, 255, 0.4)',
  MATZ: 'rgba(150, 160, 255, 0.4)'
};
var AIRSPACE_DEFAULT = 'rgba(120, 190, 220, 0.4)';

function drawAirspace() {
  var zones = data.airspace;
  if (!zones || !zones.length) { return; }

  ctx.save();
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  for (var i = 0; i < zones.length; i++) {
    var pts = zones[i].points;
    if (!pts || pts.length < 3) { continue; }
    ctx.strokeStyle = AIRSPACE_COLOURS[zones[i].type] || AIRSPACE_DEFAULT;
    ctx.beginPath();
    for (var j = 0; j < pts.length; j++) {
      var p = map.latLngToContainerPoint(L.latLng(pts[j][0], pts[j][1]));
      if (j === 0) { ctx.moveTo(p.x, p.y); } else { ctx.lineTo(p.x, p.y); }
    }
    ctx.closePath();
    ctx.stroke();
  }
  ctx.restore();
}

/* Radiosondes: a balloon, with a tick showing whether it is still climbing. */
function drawSondes(width, height) {
  var sondes = data.sondes;
  if (!sondes || !sondes.length) { return; }

  ctx.save();
  ctx.font = '600 9px Inter, Roboto, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (var i = 0; i < sondes.length; i++) {
    var s = sondes[i];
    var p = map.latLngToContainerPoint(L.latLng(s.lat, s.lon));
    if (p.x < 0 || p.y < 0 || p.x > width || p.y > height) { continue; }

    ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = '#c8a7ff';
    ctx.fill();

    // Descending sondes are the ones about to be recoverable, so the
    // direction is the interesting part.
    ctx.strokeStyle = '#c8a7ff';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(p.x, p.y + 4);
    ctx.lineTo(p.x, p.y + 9);
    ctx.stroke();

    ctx.fillStyle = 'rgba(200, 167, 255, 0.85)';
    ctx.fillText((s.vel_v > 0 ? '▲ ' : '▼ ') + Math.round(s.alt_m) + ' m',
                 p.x, p.y - 11);
  }
  ctx.restore();
}

/* Ships: a chevron pointing where it is going, which is all AIS really says. */
function drawShips(width, height) {
  var ships = data.ships;
  if (!ships || !ships.length) { return; }

  ctx.save();
  ctx.font = '600 10px Inter, Roboto, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (var i = 0; i < ships.length; i++) {
    var s = ships[i];
    var p = map.latLngToContainerPoint(L.latLng(s.lat, s.lon));
    if (p.x < 0 || p.y < 0 || p.x > width || p.y > height) { continue; }

    ctx.save();
    ctx.translate(p.x, p.y);
    ctx.rotate((s.cog_deg || 0) * DEG);
    ctx.beginPath();
    ctx.moveTo(0, -7);
    ctx.lineTo(4.5, 6);
    ctx.lineTo(0, 3.5);
    ctx.lineTo(-4.5, 6);
    ctx.closePath();
    ctx.restore();

    ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.fillStyle = '#5fe0c8';
    ctx.fill();

    // Only the nearest handful get a name; a busy waterway would otherwise
    // bury the aircraft this page is actually about.
    if (i < 6 && s.name) {
      ctx.fillStyle = 'rgba(95, 224, 200, 0.9)';
      ctx.fillText(s.name, p.x, p.y - 13);
    }
  }
  ctx.restore();
}

/* The ISS, when it is actually above the horizon and worth walking outside for. */
function updateIss() {
  var el = document.getElementById('iss');
  var iss = data && data.iss;
  if (!iss || iss.elev_deg === null || iss.elev_deg < 5) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  document.getElementById('iss-text').textContent =
    'ISS ' + iss.compass + ' ' + Math.round(iss.elev_deg) + '° up' +
    (iss.sunlit ? '' : ' · in shadow');
}

function overlaps(a, b) {
  return !(a.x + a.w < b.x || b.x + b.w < a.x ||
           a.y + a.h < b.y || b.y + b.h < a.y);
}

function drawLabel(ac, x, y, colour, placed, width, height) {
  // TIS-B and ADS-R relays carry a non-ICAO address the feed prefixes with a
  // tilde. Nothing is gained by showing punctuation on the screen.
  var _tf = window.frTagF || { cs: true, dest: true };
  var _nm = ac.flight || ac.reg || (ac.hex || '').replace('~', '').toUpperCase();
  var _l1 = [];
  if (_tf.cs && _nm) { _l1.push(_nm); }
  if (_tf.alt) { _l1.push('FL' + Math.round((ac.alt_ft || 0) / 100)); }
  if (_tf.gs) { _l1.push(Math.round(ac.gs_kt || 0) + ' kt'); }
  var title = _l1.join('   ');
  var route = '';
  if (_tf.dest && (ac.origin || ac.destination)) {
    route = (ac.origin || '???') + '  ›  ' + (ac.destination || '???');
  }
  if (!title && !route) { return; }
  if (!title) { title = route; route = ''; }

  var titleFont = '600 13px Inter, Roboto, sans-serif';
  var routeFont = '500 12px Inter, Roboto, sans-serif';
  ctx.font = titleFont;
  var w = ctx.measureText(title).width;
  if (route) {
    ctx.font = routeFont;
    w = Math.max(w, ctx.measureText(route).width);
  }

  var padX = 6;
  var padY = 4;
  var lineH = 15;
  var boxW = w + padX * 2;
  var boxH = padY * 2 + (route ? lineH * 2 : lineH);

  // Try the four cardinal placements in order of preference and take the
  // first that neither collides with something already drawn nor falls off
  // the screen. A label that cannot be placed is simply not drawn -- an
  // overlapping stack of them is worse than a missing one.
  var gap = PLANE_SCALE + 4;
  var candidates = [
    { x: x + gap, y: y - boxH / 2 },
    { x: x - gap - boxW, y: y - boxH / 2 },
    { x: x - boxW / 2, y: y + gap },
    { x: x - boxW / 2, y: y - gap - boxH }
  ];

  var box = null;
  for (var i = 0; i < candidates.length; i++) {
    var c = { x: candidates[i].x, y: candidates[i].y, w: boxW, h: boxH };
    if (c.x < 4 || c.y < 4 || c.x + c.w > width - 4 || c.y + c.h > height - 4) {
      continue;
    }
    var clash = false;
    for (var j = 0; j < placed.length; j++) {
      if (overlaps(c, placed[j])) { clash = true; break; }
    }
    if (!clash) { box = c; break; }
  }
  if (!box) { return; }
  placed.push(box);

  ctx.fillStyle = 'rgba(6, 11, 15, 0.82)';
  ctx.strokeStyle = ac.emergency ? 'rgba(255, 77, 77, 0.8)' : 'rgba(255, 255, 255, 0.15)';
  ctx.lineWidth = 1;
  roundRect(box.x, box.y, box.w, box.h, 3);
  ctx.fill();
  ctx.stroke();

  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.font = titleFont;
  ctx.fillStyle = ac.emergency ? '#ff8b8b' : '#edf3f1';
  ctx.fillText(title, box.x + padX, box.y + padY + lineH / 2);
  if (route) {
    ctx.font = routeFont;
    ctx.fillStyle = colour;
    ctx.fillText(route, box.x + padX, box.y + padY + lineH + lineH / 2);
  }
}

function render() {
  requestAnimationFrame(render);
  if (!map || !data) { return; }

  var now = performance.now();
  if (now - render.last < FRAME_MS) { return; }
  render.last = now;

  var width = window.innerWidth;
  var height = window.innerHeight;
  ctx.clearRect(0, 0, width, height);
  drawAirspace();
  drawRangeRing();
  drawSondes(width, height);
  drawShips(width, height);

  // How far the server's clock has advanced since this payload arrived.
  var elapsedSincePoll = (now - dataClientAt) / 1000;

  var visible = [];
  var aircraft = data.aircraft;

  for (var i = 0; i < aircraft.length; i++) {
    var ac = aircraft[i];

    if (window.frClassAllowed && !window.frClassAllowed(ac)) { continue; }

    var lat = ac.lat;
    var lon = ac.lon;
    var age = (ac.pos_age_s || 0) + elapsedSincePoll;
    if (!ac.on_ground && ac.gs_kt > 5 && age < MAX_EXTRAPOLATE_S) {
      var moved = projectForward(lat, lon, ac.track_deg, ac.gs_kt * age / 3600);
      lat = moved[0];
      lon = moved[1];
    }

    var pt = map.latLngToContainerPoint(L.latLng(lat, lon));
    if (pt.x < -60 || pt.y < -60 || pt.x > width + 60 || pt.y > height + 60) {
      continue;
    }

    // Ease toward the new position so a poll never produces a visible jump.
    var key = ac.hex || ac.flight || String(i);
    var track = tracks.get(key);
    if (!track) {
      track = { x: pt.x, y: pt.y, heading: ac.track_deg };
      tracks.set(key, track);
    } else {
      track.x += (pt.x - track.x) * 0.18;
      track.y += (pt.y - track.y) * 0.18;
      var delta = ((ac.track_deg - track.heading + 540) % 360) - 180;
      track.heading = (track.heading + delta * 0.18 + 360) % 360;
    }
    track.seen = now;

    visible.push({ ac: ac, x: track.x, y: track.y, heading: track.heading });
  }

  // Which aircraft the card is talking about. Decided once per frame so the
  // ring on the map and the text in the card can never disagree.
  var subject = currentSubject(Date.now());
  var subjectHex = subject ? subject.hex : null;

  // Route first, then trails, then the aircraft themselves: everything that
  // is context has to sit under the thing it is context for.
  if (subject) { drawRouteLine(subject); }

  for (var t = 0; t < visible.length; t++) {
    var ta = visible[t].ac;
    var _ft = ta.trail, _lm = (ta.hex === subjectHex) ? (window.frSelTrail != null ? window.frSelTrail : 12) : 12;
    if (_ft && _ft.length > 1) { if (_lm <= 1) { ta.trail = null; } else if (_ft.length > _lm) { ta.trail = _ft.slice(-_lm); } }
    drawTrail(ta, altitudeColour(ta), ta.hex === subjectHex ? 1.9 : 1);
    ta.trail = _ft;
  }

  for (var v = 0; v < visible.length; v++) {
    var item = visible[v];
    if (item.ac.hex === subjectHex) {
      drawSelection(item.x, item.y, item.ac.emergency ? '#ff4d4d' :
                    (item.ac.interesting ? GOLD : 'rgba(39, 209, 158, 0.9)'));
    }
    (item.ac.rotor ? drawHelicopter : drawPlane)(
      item.x, item.y, item.heading, altitudeColour(item.ac),
      item.ac.emergency, item.ac.interesting);
  }

  // Reserve every silhouette before placing text, so a label never lands on
  // another aircraft.
  var placed = [];
  for (var p = 0; p < visible.length; p++) {
    placed.push({
      x: visible[p].x - PLANE_SCALE, y: visible[p].y - PLANE_SCALE,
      w: PLANE_SCALE * 2, h: PLANE_SCALE * 2
    });
  }

  // aircraft[] arrives nearest-first, so this labels the closest traffic.
  var budget = config.label_count;
  for (var l = 0; l < visible.length && budget > 0; l++) {
    if (visible[l].ac.on_ground) { continue; }
    drawLabel(visible[l].ac, visible[l].x, visible[l].y,
              altitudeColour(visible[l].ac), placed, width, height);
    budget--;
  }

  // Kept for hit-testing clicks against what is actually on screen.
  render.visible = visible;

  // Refresh the card only when it would say something different. Rewriting
  // the DOM twelve times a second for no reason is how an ambient page turns
  // into a space heater.
  if (subjectHex !== render.detailHex || dataClientAt !== render.detailAt) {
    render.detailHex = subjectHex;
    render.detailAt = dataClientAt;
    updateDetail(subject);
  }

  // Aircraft that have left the area must not keep their smoothing state
  // forever; this page is expected to run for weeks.
  if (now - (render.swept || 0) > 60000) {
    render.swept = now;
    tracks.forEach(function (value, key) {
      if (now - value.seen > STALE_TRACK_MS) { tracks.delete(key); }
    });
  }
}
render.last = 0;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

/*
 * Click an aircraft to hold the card on it.
 *
 * The listener is on the document rather than the canvas: the canvas covers
 * the map, and giving it pointer events would take the clicks away from the
 * attribution links underneath, which the tile licences require to work.
 * Clicking empty sky releases the pin and hands the card back to the
 * spotlight rotation.
 */
document.addEventListener('click', function (ev) {
  if (ev.target.closest('.panel, .leaflet-control-attribution')) { return; }
  var list = render.visible || [];
  var best = null;
  var bestDist = 26;                     // a comfortable target at arm's length
  for (var i = 0; i < list.length; i++) {
    var d = Math.hypot(list[i].x - ev.clientX, list[i].y - ev.clientY);
    if (d < bestDist) { bestDist = d; best = list[i].ac; }
  }
  if (best) {
    pinnedHex = best.hex;
    pinnedAt = Date.now();
  } else {
    pinnedHex = null;
  }
  // Force the card to redraw on the next frame rather than waiting for the
  // subject or the data to change on their own.
  render.detailHex = null;
});

var resizeTimer = null;
window.addEventListener('resize', function () {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function () {
    resizeCanvas();
    if (map) {
      map.invalidateSize({ animate: false });
      fitScope();
    }
  }, 250);
});

// A logo or photo that 404s must leave a clean card, not a broken-image icon.
// The server answers 404 whenever it could not fetch one, which is normal for
// private aircraft and small operators.
['d-photo', 'd-logo'].forEach(function (id) {
  document.getElementById(id).addEventListener('error', function () {
    this.hidden = true;
  });
});

resizeCanvas();
tickClock();
setInterval(tickClock, 1000);
poll();
requestAnimationFrame(render);

// =========================================================================
// CRT radar + settings (v4) — airport info cards + multi-toggle tags.
// =========================================================================
(function () {
  var radar = document.getElementById('radar');
  if (!radar) { return; }
  var rctx = radar.getContext('2d');
  var dpr = 1;
  var MONO = 'ui-monospace, "SFMono-Regular", Menlo, monospace';
  var GREEN = '39, 209, 158', RINGTX = '89, 168, 150', APCOL = '150, 170, 180';
  var SWEEP_MS = 38000, STEP_DEG = 2.25, NEEDLE_OPA = [0.9, 0.5, 0.3];
  var viewMode = localStorage.getItem('fr_view') === 'radar' ? 'radar' : 'map';
  var showAirports = localStorage.getItem('fr_airports') === '1';
  var CLASS_KEYS = ['com','priv','heli','mil','pub','other'];
  var classOn = {}, tagF = { cs: true, alt: false, gs: false, dest: true };
  (function () {
    var s = {}; try { s = JSON.parse(localStorage.getItem('fr_classes') || '{}'); } catch (e) {}
    CLASS_KEYS.forEach(function (k) { classOn[k] = (s[k] !== false); });
    try { var tf = JSON.parse(localStorage.getItem('fr_tagf') || 'null'); if (tf) tagF = tf; } catch (e) {}
    window.frTagF = tagF;
  })();

  var BX = [0,1.5,1.5,9,9,1.5,1.5,4.5,0,-4.5,-1.5,-1.5,-9,-9,-1.5,-1.5,0];
  var BY = [-11,-6,-1,3.5,5.5,1.5,6.5,9.5,11,9.5,6.5,1.5,5.5,3.5,-1,-6,-11];
  var PUB = ['SHERIFF','POLICE',' FIRE','CAL FIRE','CALFIRE','HIGHWAY PATROL',' CHP','RESCUE',
    'COUNTY OF','CITY OF','STATE OF','FOREST','COAST GUARD','CUSTOMS','BORDER PATROL',
    'AIR METHODS','MERCY AIR','LIFE FLIGHT','REACH','MEDEVAC','PARAMEDIC','DEPARTMENT OF','CAL-FIRE'];
  function classOf(ac) {
    if (ac.military) return 'mil';
    var o = (ac.owner || '').toUpperCase();
    for (var i = 0; i < PUB.length; i++) if (o.indexOf(PUB[i]) >= 0) return 'pub';
    if (ac.rotor || ac.category === 'A7') return 'heli';
    var cs = (ac.flight || '').trim();
    if (/AIRLINE|AIRWAYS|AIR LINES|AIRLINK/.test(o) || (/^[A-Z]{3}\d/.test(cs) && !/LLC|INC|JET|AVIATION|HOLDING|LEASING|CHARTER/.test(o))) return 'com';
    var cat = ac.category || '', reg = ac.reg || '';
    if (cat === 'A1' || cat === 'A2' || /LLC|INC|JET|AVIATION|HOLDING/.test(o) || /^N\d/.test(reg)) return 'priv';
    return 'other';
  }
  window.frClassAllowed = function (ac) { return classOn[classOf(ac)] !== false; };

  var COMPASS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  function compass(b) { return COMPASS[Math.round((b % 360) / 22.5) % 16]; }

  var airports = [], apLayer = null, apScreen = [];
  function geoDB(lat, lon) {
    var R = 3440.065, la1 = config.lat * Math.PI/180, la2 = lat*Math.PI/180;
    var dla = (lat-config.lat)*Math.PI/180, dlo = (lon-config.lon)*Math.PI/180;
    var a = Math.sin(dla/2)*Math.sin(dla/2)+Math.cos(la1)*Math.cos(la2)*Math.sin(dlo/2)*Math.sin(dlo/2);
    var y = Math.sin(dlo)*Math.cos(la2), x = Math.cos(la1)*Math.sin(la2)-Math.sin(la1)*Math.cos(la2)*Math.cos(dlo);
    return { d: 2*R*Math.asin(Math.sqrt(a)), brg: (Math.atan2(y,x)*180/Math.PI+360)%360 };
  }
  function loadAirports() {
    fetch('/airports.json', { cache: 'force-cache' }).then(function (r) { return r.json(); })
      .then(function (list) {
        airports = list.map(function (a) { var g = geoDB(a.lat, a.lon); a.d = g.d; a.brg = g.brg; return a; });
        applyAirportsMap();
      }).catch(function () {});
  }
  function applyAirportsMap() {
    if (typeof map === 'undefined' || !map || typeof L === 'undefined') return;
    if (apLayer) { map.removeLayer(apLayer); apLayer = null; }
    if (!showAirports || !airports.length) return;
    apLayer = L.layerGroup();
    airports.forEach(function (a) {
      L.circleMarker([a.lat, a.lon], { radius: 4, color: 'rgba(150,170,180,0.95)', weight: 1,
        fillColor: 'rgba(150,170,180,0.5)', fillOpacity: 0.5 })
        .bindTooltip(a.code, { permanent: true, direction: 'right', className: 'ap-tip' })
        .on('click', function () { showAirportCard(a); }).addTo(apLayer);
    });
    apLayer.addTo(map);
  }

  // ---- airport info card ----
  function fmtMi(nm) { return (config && config.units === 'metric') ? (nm*1.852).toFixed(0)+' km' : nm.toFixed(1)+' nm'; }
  function fmtFt(ft) { if (ft == null) return '—'; return (config && config.units === 'metric') ? Math.round(ft*0.3048)+' m' : ft.toLocaleString()+' ft'; }
  function freqStr(f) {
    if (!f) return '';
    var order = ['Tower','Ground','ATIS','CTAF','UNICOM','AWOS','ASOS','Clearance','Approach','Departure'], out = [];
    order.forEach(function (k) { if (f[k]) out.push(k + ' ' + f[k]); });
    return out.join('  ·  ');
  }
  function showAirportCard(a) {
    var card = document.getElementById('ap-card'); if (!card) return;
    var photo = document.getElementById('ap-photo');
    if (a.photo) { if (photo.getAttribute('data-src') !== a.photo) { photo.setAttribute('data-src', a.photo); photo.src = a.photo; } photo.hidden = false; }
    else { photo.hidden = true; photo.removeAttribute('src'); photo.removeAttribute('data-src'); }
    document.getElementById('ap-name').textContent = a.name || a.code;
    var sub = [a.icao || a.code]; if (a.iata && a.iata !== (a.icao || a.code)) sub.push(a.iata);
    if (a.type) sub.push(a.type);
    document.getElementById('ap-sub').textContent = sub.join('  ·  ');
    document.getElementById('ap-addr').textContent = a.address || [a.city, a.region].filter(Boolean).join(', ');
    var rc = (a.runway_count != null) ? a.runway_count : (a.runways ? a.runways.length : 0);
    document.getElementById('ap-stats').innerHTML =
      '<span>' + fmtMi(a.d) + '  ' + compass(a.brg) + ' ' + Math.round(a.brg) + '°</span>' +
      '<span>elev ' + fmtFt(a.ele_ft) + '</span>' +
      '<span>' + rc + ' runway' + (rc === 1 ? '' : 's') + '</span>';
    var fs = freqStr(a.freqs), fe = document.getElementById('ap-freqs');
    if (fs) { fe.innerHTML = '<div class="ap-rwh">FREQUENCIES</div><div class="ap-fq">' + fs + '</div>'; fe.hidden = false; } else { fe.hidden = true; }
    var rw = a.runways || [];
    document.getElementById('ap-runways').innerHTML = rw.length
      ? '<div class="ap-rwh">RUNWAYS</div>' + rw.map(function (r) {
          return '<div class="ap-rw"><span>' + (r.id || '?') + '</span><span>' + (r.len ? fmtFt(r.len) : '—') + '</span><span>' + (r.surf || '') + '</span></div>'; }).join('')
      : '';
    var web = document.getElementById('ap-web');
    if (a.website) { web.href = a.website; web.hidden = false; } else { web.hidden = true; }
    card.hidden = false;
  }
  function hideAirportCard() { var c = document.getElementById('ap-card'); if (c) c.hidden = true; }

  function sizeRadar() {
    dpr = window.devicePixelRatio || 1;
    radar.width = Math.round(window.innerWidth * dpr); radar.height = Math.round(window.innerHeight * dpr);
    radar.style.width = window.innerWidth + 'px'; radar.style.height = window.innerHeight + 'px';
  }
  window.addEventListener('resize', sizeRadar); sizeRadar();
  function range() { return (config && config.range_nm) ? config.range_nm : 20; }
  function unit() { return (config && config.units === 'metric') ? 'KM' : 'NM'; }
  function sector(cx, cy, R, a0, a1) {
    rctx.beginPath(); rctx.moveTo(cx, cy);
    for (var s = 0; s <= 18; s++) { var d = a0+(a1-a0)*s/18, r = (d-90)*Math.PI/180; rctx.lineTo(cx+Math.cos(r)*R, cy+Math.sin(r)*R); }
    rctx.closePath();
  }
  function tagTokens(ac) {
    var t = [];
    if (tagF.cs) { var c = (ac.flight || ac.hex || '').trim(); if (c) t.push(c); }
    if (tagF.alt) t.push('FL' + Math.round((ac.alt_ft || 0) / 100));
    if (tagF.gs) t.push(Math.round(ac.gs_kt || 0) + 'kt');
    if (tagF.dest) { var d = ac.destination || ac.origin || ''; if (d) t.push('›' + d); }
    return t;
  }
  function anyTag() { return tagF.cs || tagF.alt || tagF.gs || tagF.dest; }
  function drawPlane(px, py, track, color, sel) {
    var t = (track||0)*Math.PI/180, ct = Math.cos(t), st = Math.sin(t), s = 1.25*dpr;
    rctx.beginPath();
    for (var i = 0; i < 17; i++) { var x = px+(BX[i]*ct-BY[i]*st)*s, y = py+(BX[i]*st+BY[i]*ct)*s; if (i===0) rctx.moveTo(x,y); else rctx.lineTo(x,y); }
    rctx.closePath();
    rctx.strokeStyle = color; rctx.lineWidth = (sel?3:2)*dpr; rctx.lineJoin = 'round';
    rctx.shadowColor = color; rctx.shadowBlur = 5*dpr; rctx.stroke(); rctx.shadowBlur = 0;
  }

  function drawRadar(now) {
    var W = radar.width, H = radar.height, cx = W/2, cy = H/2, maxR = Math.min(W,H)*0.42, rng = range();
    rctx.fillStyle = '#03070A'; rctx.fillRect(0, 0, W, H);
    rctx.textBaseline = 'middle'; rctx.textAlign = 'left';
    for (var i = 1; i <= 4; i++) {
      var rr = maxR*i/4, mid = (i===2), outer = (i===4);
      rctx.strokeStyle = 'rgba(' + GREEN + ',' + (outer?0.5:(mid?0.32:0.16)) + ')'; rctx.lineWidth = dpr;
      rctx.beginPath(); rctx.arc(cx, cy, rr, 0, Math.PI*2); rctx.stroke();
      if (mid || outer) { rctx.fillStyle = 'rgba(' + RINGTX + ',0.85)'; rctx.font = (11*dpr)+'px '+MONO;
        rctx.fillText(Math.round(rng*i/4)+(outer?' '+unit():''), cx+5*dpr, cy-rr-2*dpr); }
    }
    rctx.strokeStyle = 'rgba(' + GREEN + ',0.12)'; rctx.lineWidth = dpr;
    rctx.beginPath(); rctx.moveTo(cx-maxR,cy); rctx.lineTo(cx+maxR,cy); rctx.moveTo(cx,cy-maxR); rctx.lineTo(cx,cy+maxR); rctx.stroke();
    rctx.fillStyle = 'rgba(' + GREEN + ',0.5)'; rctx.textAlign = 'center'; rctx.font = (13*dpr)+'px '+MONO;
    rctx.fillText('N',cx,cy-maxR-11*dpr); rctx.fillText('S',cx,cy+maxR+12*dpr); rctx.fillText('E',cx+maxR+12*dpr,cy); rctx.fillText('W',cx-maxR-12*dpr,cy);

    apScreen.length = 0;
    if (showAirports) {
      rctx.textAlign = 'left'; rctx.textBaseline = 'middle';
      for (var p = 0; p < airports.length; p++) {
        var ap = airports[p]; if (ap.d > rng) continue;
        var arr = ap.d/rng*maxR, aa = (ap.brg-90)*Math.PI/180, ax = cx+Math.cos(aa)*arr, ay = cy+Math.sin(aa)*arr;
        rctx.strokeStyle = 'rgba(' + APCOL + ',0.75)'; rctx.lineWidth = dpr;
        rctx.beginPath(); rctx.arc(ax, ay, 3*dpr, 0, Math.PI*2); rctx.stroke();
        rctx.beginPath(); rctx.moveTo(ax-5*dpr,ay); rctx.lineTo(ax+5*dpr,ay); rctx.moveTo(ax,ay-5*dpr); rctx.lineTo(ax,ay+5*dpr); rctx.stroke();
        rctx.fillStyle = 'rgba(' + APCOL + ',0.7)'; rctx.font = (9*dpr)+'px '+MONO; rctx.fillText(ap.code, ax+7*dpr, ay);
        apScreen.push({ ap: ap, x: ax, y: ay });
      }
    }

    var sweepDeg = (now%SWEEP_MS)/SWEEP_MS*360, WEDGE = 46;
    sector(cx,cy,maxR,sweepDeg-WEDGE,sweepDeg); rctx.fillStyle = 'rgba(' + GREEN + ',0.08)'; rctx.fill();
    sector(cx,cy,maxR,sweepDeg-WEDGE/2,sweepDeg); rctx.fillStyle = 'rgba(' + GREEN + ',0.10)'; rctx.fill();
    for (var n = 0; n < 3; n++) {
      var dd = sweepDeg-n*STEP_DEG, rad = (dd-90)*Math.PI/180;
      rctx.strokeStyle = 'rgba(' + GREEN + ',' + NEEDLE_OPA[n] + ')'; rctx.lineWidth = (n===0?2:1)*dpr;
      if (n===0) { rctx.shadowColor = 'rgba(' + GREEN + ',0.9)'; rctx.shadowBlur = 8*dpr; }
      rctx.beginPath(); rctx.moveTo(cx,cy); rctx.lineTo(cx+Math.cos(rad)*maxR, cy+Math.sin(rad)*maxR); rctx.stroke(); rctx.shadowBlur = 0;
    }

    var acs = (data && data.aircraft) ? data.aircraft : [], budget = config ? config.label_count : 8, contacts = 0;
    var pinned = (typeof pinnedHex !== 'undefined') ? pinnedHex : null;
    rctx.textAlign = 'left'; rctx.textBaseline = 'alphabetic';
    for (var k = 0; k < acs.length; k++) {
      var ac = acs[k];
      if (ac.on_ground || ac.dist_nm == null || ac.dist_nm > rng*1.02) continue;
      if (!window.frClassAllowed(ac)) continue;
      contacts++;
      var r = Math.min(ac.dist_nm/rng,1)*maxR, rd = ((ac.bearing_deg||0)-90)*Math.PI/180;
      var x = cx+Math.cos(rd)*r, y = cy+Math.sin(rd)*r, sel = pinned && ac.hex === pinned;
      var col = ac.emergency ? '#ff4d4d' : (sel ? '#ffffff' : altitudeColour(ac));
      drawPlane(x, y, ac.track_deg, col, sel);
      if (ac.radio) { rctx.strokeStyle = 'rgba(120,255,160,0.8)'; rctx.lineWidth = dpr; rctx.beginPath(); rctx.arc(x, y, 15*dpr, 0, Math.PI*2); rctx.stroke(); }
      if (anyTag() && budget > 0) {
        var tok = tagTokens(ac);
        if (tok.length) {
          rctx.fillStyle = 'rgba(' + GREEN + ',0.9)'; rctx.font = (11*dpr)+'px '+MONO;
          rctx.fillText(tok[0], x+16*dpr, y-6*dpr);
          if (tok.length > 1) { rctx.fillStyle = 'rgba(' + GREEN + ',0.55)'; rctx.fillText(tok.slice(1).join(' '), x+16*dpr, y+7*dpr); }
          budget--;
        }
      }
    }
    rctx.fillStyle = 'rgba(' + GREEN + ',0.95)'; rctx.beginPath(); rctx.arc(cx, cy, 3.5*dpr, 0, Math.PI*2); rctx.fill();
    rctx.fillStyle = 'rgba(0,0,0,0.12)'; for (var yy = 0; yy < H; yy += 3*dpr) rctx.fillRect(0, yy, W, dpr);
    rctx.textAlign = 'left'; rctx.textBaseline = 'alphabetic';
    rctx.fillStyle = 'rgba(' + GREEN + ',0.8)'; rctx.font = (14*dpr)+'px '+MONO;
    rctx.fillText('SCOPE ' + rng + ' ' + unit() + '   ' + contacts + ' CONTACTS', 18*dpr, 30*dpr);
    var clk = document.getElementById('clock-time'); if (clk) { rctx.textAlign = 'right'; rctx.fillText(clk.textContent, W-18*dpr, 30*dpr); }
  }

  radar.addEventListener('click', function (e) {
    var rect = radar.getBoundingClientRect();
    var px = (e.clientX-rect.left)*dpr, py = (e.clientY-rect.top)*dpr, best = null, bd = 22*dpr;
    for (var i = 0; i < apScreen.length; i++) { var dx = apScreen[i].x-px, dy = apScreen[i].y-py, dd = Math.sqrt(dx*dx+dy*dy); if (dd < bd) { bd = dd; best = apScreen[i].ap; } }
    if (best) showAirportCard(best); else hideAirportCard();
  });

  function loop(now) { requestAnimationFrame(loop); if (viewMode !== 'radar') return; if (loop.last && now-loop.last < 55) return; loop.last = now; drawRadar(now); }
  requestAnimationFrame(loop);

  function seg(attr, val) { document.querySelectorAll('#fr-settings [' + attr + ']').forEach(function (b) { b.classList.toggle('on', b.getAttribute(attr) === val); }); }
  function applyView(m) { viewMode = m; localStorage.setItem('fr_view', m); radar.hidden = (m !== 'radar'); if (m !== 'radar') hideAirportCard(); seg('data-view', m); }
  function syncClasses() { document.querySelectorAll('#fr-settings [data-class]').forEach(function (b) { b.classList.toggle('on', classOn[b.getAttribute('data-class')] !== false); }); }
  function syncTags() { document.querySelectorAll('#fr-settings [data-tagf]').forEach(function (b) { b.classList.toggle('on', !!tagF[b.getAttribute('data-tagf')]); }); }
  function syncAirports() { document.querySelectorAll('#fr-settings [data-air]').forEach(function (b) { b.classList.toggle('on', showAirports); }); }
  var gear = document.getElementById('fr-gear'), panel = document.getElementById('fr-settings');
  var slider = document.getElementById('fr-range'), rval = document.getElementById('fr-rval');
  function syncRange() { var v = range(); if (slider) slider.value = v; if (rval) rval.textContent = v + ' ' + unit(); }
  function syncUnits() { seg('data-unit', (config && config.units === 'metric') ? 'metric' : 'aviation'); }
  function syncAll() { syncRange(); syncUnits(); syncClasses(); syncTags(); syncAirports(); syncTrail(); }
  if (gear) gear.addEventListener('click', function () { panel.hidden = !panel.hidden; if (!panel.hidden) syncAll(); });
  document.querySelectorAll('#fr-settings [data-view]').forEach(function (b) { b.addEventListener('click', function () { applyView(b.getAttribute('data-view')); }); });
  document.querySelectorAll('#fr-settings [data-tagf]').forEach(function (b) {
    b.addEventListener('click', function () { var k = b.getAttribute('data-tagf'); tagF[k] = !tagF[k]; localStorage.setItem('fr_tagf', JSON.stringify(tagF)); syncTags(); });
  });
  document.querySelectorAll('#fr-settings [data-class]').forEach(function (b) {
    b.addEventListener('click', function () { var k = b.getAttribute('data-class'); classOn[k] = !(classOn[k] !== false); localStorage.setItem('fr_classes', JSON.stringify(classOn)); syncClasses(); });
  });
  document.querySelectorAll('#fr-settings [data-air]').forEach(function (b) {
    b.addEventListener('click', function () { showAirports = !showAirports; localStorage.setItem('fr_airports', showAirports ? '1' : '0'); if (!showAirports) hideAirportCard(); syncAirports(); applyAirportsMap(); });
  });
  function postCfg(obj, after) { fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) }).then(function (r) { return r.json(); }).then(function () { if (after) after(); }).catch(function () {}); }
  document.querySelectorAll('#fr-settings [data-unit]').forEach(function (b) { b.addEventListener('click', function () { var u = b.getAttribute('data-unit'); if (config) config.units = u; syncUnits(); syncRange(); postCfg({ units: u }); }); });
  if (slider) {
    slider.addEventListener('input', function () { var v = parseInt(slider.value, 10) || 20; if (config) config.range_nm = v; if (rval) rval.textContent = v + ' ' + unit(); });
    slider.addEventListener('change', function () { postCfg({ range_nm: parseInt(slider.value, 10) || 20 }, function () { if (typeof fitScope === 'function') { try { fitScope(); } catch (e) {} } }); });
  }
  var tslider = document.getElementById('fr-trail'), tval = document.getElementById('fr-tval');
  window.frSelTrail = parseInt(localStorage.getItem('fr_seltrail') || '12', 10);
  function syncTrail() { if (tslider) { tslider.value = window.frSelTrail; if (tval) tval.textContent = window.frSelTrail; } }
  if (tslider) {
    tslider.addEventListener('input', function () { window.frSelTrail = parseInt(tslider.value, 10); if (tval) tval.textContent = tslider.value; localStorage.setItem('fr_seltrail', tslider.value); });
  }
  var apc = document.getElementById('ap-close'); if (apc) apc.addEventListener('click', hideAirportCard);
  var wait = setInterval(function () { if (config) { syncAll(); applyView(viewMode); loadAirports(); clearInterval(wait); } }, 300);
})();
