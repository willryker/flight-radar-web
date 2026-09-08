#!/usr/bin/env python3
"""
build_airports.py — regenerate static/airports.json + static/airports/*.jpg
for the Flight Radar app's airport overlay and info cards.

Self-contained and stdlib-only. Downloads its own source data, so it can be
re-run any time the home location changes or the data needs a refresh.

  python3 tools/build_airports.py                 # uses app config.json location
  python3 tools/build_airports.py --lat 51.47 --lon -0.00 --radius 60
  python3 tools/build_airports.py --force         # re-fetch aerials that exist

Sources (all free, no API key):
  - OurAirports  (airports / runways / frequencies CSV)   airport list + facts
  - OpenStreetMap Nominatim  (reverse geocode)            street address
  - Esri World Imagery export                             aerial thumbnail

Frequencies are filtered to the 118-137 MHz civil comms band and formatted to
3 decimals, because the raw OurAirports data carries the occasional bad value.
"""
import csv, math, json, os, sys, time, argparse, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.normpath(os.path.join(HERE, '..', 'static'))
WORK = os.path.join(HERE, '.airport-cache')     # downloaded CSVs live here
UA = 'flight-radar-lab/1.0 (homelab ambient display)'
OA = 'https://davidmegginson.github.io/ourairports-data/'
CSVS = {'airports.csv': 'airports.csv', 'runways.csv': 'runways.csv',
        'airport-frequencies.csv': 'freqs.csv'}

def log(*a): print(*a, flush=True)

def get(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=timeout).read()

def ensure_csvs():
    os.makedirs(WORK, exist_ok=True)
    for remote, local in CSVS.items():
        p = os.path.join(WORK, local)
        if not os.path.exists(p) or os.path.getsize(p) < 1000:
            log('downloading', remote)
            open(p, 'wb').write(get(OA + remote, timeout=90))

def home_from_config():
    cfg = os.environ.get('FLIGHT_RADAR_CONFIG', os.path.join(HERE, '..', 'config.json'))
    try:
        c = json.load(open(cfg)); return float(c['lat']), float(c['lon'])
    except Exception:
        return None

def nm(la, lo, LAT, LON):
    R = 3440.065; p1, p2 = math.radians(la), math.radians(LAT)
    dp = math.radians(LAT - la); dl = math.radians(LON - lo)
    return 2 * R * math.asin(math.sqrt(math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2))

def bearing(la, lo, LAT, LON):
    y = math.sin(math.radians(lo-LON)) * math.cos(math.radians(la))
    x = math.cos(math.radians(LAT))*math.sin(math.radians(la)) - math.sin(math.radians(LAT))*math.cos(math.radians(la))*math.cos(math.radians(lo-LON))
    return (math.degrees(math.atan2(y, x)) + 360) % 360

FRQ = {'TWR':'Tower','ATIS':'ATIS','GND':'Ground','CTAF':'CTAF','UNIC':'UNICOM',
       'AWOS':'AWOS','ASOS':'ASOS','CLD':'Clearance','APP':'Approach','DEP':'Departure'}
def freq_valid(f):
    try: return 118.0 <= float(f) <= 137.0
    except: return False

W_KM = {'large_airport': 5.0, 'medium_airport': 3.4, 'small_airport': 2.0}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lat', type=float); ap.add_argument('--lon', type=float)
    ap.add_argument('--radius', type=float, default=60.0, help='nautical miles (default 60)')
    ap.add_argument('--force', action='store_true', help='re-fetch aerials that already exist')
    ap.add_argument('--no-photos', action='store_true')
    ap.add_argument('--no-address', action='store_true')
    args = ap.parse_args()

    LAT, LON = (args.lat, args.lon) if args.lat is not None and args.lon is not None else (home_from_config() or (None, None))
    if LAT is None:
        sys.exit('no location: pass --lat/--lon or provide app config.json')
    log(f'home {LAT},{LON}  radius {args.radius} nm  ->  {STATIC}')
    ensure_csvs()
    photodir = os.path.join(STATIC, 'airports'); os.makedirs(photodir, exist_ok=True)

    # airport list (large/medium/small within radius)
    aps = []
    for r in csv.DictReader(open(os.path.join(WORK, 'airports.csv'))):
        if not r['latitude_deg']: continue
        la, lo = float(r['latitude_deg']), float(r['longitude_deg'])
        if abs(la-LAT) > 1.6 or abs(lo-LON) > 1.6: continue
        if r['type'] not in ('large_airport','medium_airport','small_airport'): continue
        d = nm(la, lo, LAT, LON)
        if d > args.radius: continue
        aps.append((d, la, lo, r))
    aps.sort(key=lambda x: x[0])
    idents = set(r['ident'] for _,_,_,r in aps)
    log(f'{len(aps)} airports')

    runs = {}
    for rw in csv.DictReader(open(os.path.join(WORK, 'runways.csv'))):
        if rw['airport_ident'] in idents and not rw.get('closed','0').startswith('1'):
            runs.setdefault(rw['airport_ident'], []).append(rw)
    freqs = {}
    for f in csv.DictReader(open(os.path.join(WORK, 'freqs.csv'))):
        if f['airport_ident'] not in idents or not freq_valid(f['frequency_mhz']): continue
        t = f['type'].upper()
        for k in FRQ:
            if t.startswith(k):
                freqs.setdefault(f['airport_ident'], {}).setdefault(FRQ[k], '%.3f' % float(f['frequency_mhz'])); break

    out = []
    for i, (d, la, lo, r) in enumerate(aps):
        ident = r['ident']
        runways = []
        for x in runs.get(ident, []):
            ln = x.get('length_ft'); rid = (x.get('le_ident','')+'/'+x.get('he_ident','')).strip('/')
            runways.append({'id': rid or '?', 'len': int(ln) if ln and ln.isdigit() else None, 'surf': (x.get('surface') or '').title()})
        a = {'code': r['iata_code'] or ident, 'icao': r['icao_code'] or ident, 'iata': r['iata_code'] or '',
             'name': r['name'], 'lat': round(la,5), 'lon': round(lo,5), 'd': round(d,1), 'brg': round(bearing(la,lo,LAT,LON),1),
             'type': r['type'].replace('_',' '), 'city': r['municipality'] or '', 'region': (r['iso_region'] or '').replace('US-',''),
             'ele_ft': int(r['elevation_ft']) if r['elevation_ft'] and r['elevation_ft'].lstrip('-').isdigit() else None,
             'website': r.get('home_link') or '', 'wiki': r.get('wikipedia_link') or '',
             'runways': runways, 'runway_count': len(runways), 'freqs': freqs.get(ident, {}), 'photo': ''}
        if not args.no_address:
            try:
                j = json.loads(get(f"https://nominatim.openstreetmap.org/reverse?lat={la}&lon={lo}&format=json&zoom=14&addressdetails=1"))
                ad = j.get('address', {}); parts = []
                for k in ('road','neighbourhood','hamlet','village','town','city','county'):
                    if ad.get(k) and ad[k] not in parts: parts.append(ad[k])
                a['address'] = ', '.join(parts[:3] + ([ad['state']] if ad.get('state') else []) + ([ad['postcode']] if ad.get('postcode') else []))
            except Exception:
                a['address'] = ', '.join(x for x in (r['municipality'], (r['iso_region'] or '').replace('US-','')) if x)
            time.sleep(1.1)   # Nominatim: <=1 req/sec
        photo = os.path.join(photodir, ident + '.jpg')
        if not args.no_photos and (args.force or not os.path.exists(photo)):
            try:
                wkm = W_KM.get(r['type'], 2.0); hkm = wkm * 220 / 320
                dlon = wkm / (111.32 * math.cos(math.radians(la))); dlat = hkm / 111.0
                img = get("https://server.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export"
                          f"?bbox={lo-dlon/2},{la-dlat/2},{lo+dlon/2},{la+dlat/2}&bboxSR=4326&size=320,220&format=jpg&f=image")
                if img[:2] == b'\xff\xd8': open(photo, 'wb').write(img)
            except Exception: pass
        if os.path.exists(photo): a['photo'] = '/airports/' + ident + '.jpg'
        out.append(a)
        if (i+1) % 15 == 0: log(f'  ...{i+1}/{len(aps)}')

    json.dump(out, open(os.path.join(STATIC, 'airports.json'), 'w'))
    log(f'DONE: {len(out)} airports -> {STATIC}/airports.json, '
        f'{sum(1 for a in out if a["photo"])} aerials in {photodir}')
    log('Deploy: copy static/airports.json + static/airports/ to /opt/flight-radar/static/, no restart needed.')

if __name__ == '__main__':
    main()
