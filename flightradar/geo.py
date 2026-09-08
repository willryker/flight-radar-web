"""
Geometry.

distance/bearing/projection are ports of main/geo.c, so the scope and the page
agree about where things are. The rest -- elevation, closest point of approach,
along-track progress -- exists only here, because the round panel had no room
to say any of it.
"""

import math

EARTH_RADIUS_NM = 3440.065
FT_PER_NM = 6076.12

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def distance_nm(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2) -
         math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project_forward(lat, lon, bearing, dist_nm):
    """Move dist_nm along a great circle on the given bearing."""
    ang = dist_nm / EARTH_RADIUS_NM
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing)
    phi2 = math.asin(math.sin(phi1) * math.cos(ang) +
                     math.cos(phi1) * math.sin(ang) * math.cos(theta))
    lam2 = lam1 + math.atan2(math.sin(theta) * math.sin(ang) * math.cos(phi1),
                             math.cos(ang) - math.sin(phi1) * math.sin(phi2))
    return math.degrees(phi2), (math.degrees(lam2) + 540.0) % 360.0 - 180.0


def compass_point(deg):
    if deg is None:
        return ""
    return COMPASS[int((deg % 360.0) / 22.5 + 0.5) % 16]


def elevation_deg(alt_ft, dist_nm):
    """
    How far up from the horizon to look. Ignores refraction and earth
    curvature, both of which are noise inside 20 nm.
    """
    if dist_nm is None or dist_nm <= 0.0:
        return 90.0 if alt_ft and alt_ft > 0 else None
    if not alt_ft or alt_ft <= 0:
        return 0.0
    return math.degrees(math.atan2(alt_ft / FT_PER_NM, dist_nm))


def satellite_elevation_deg(ground_dist_nm, alt_km):
    """
    Elevation of something in orbit, above the observer's horizon.

    elevation_deg() above is a flat-earth approximation, which is fine for an
    aeroplane 20 nm away and useless for the ISS: at 420 km up and 2000 km
    away the curvature of the earth is the whole answer, and the flat version
    would claim it is 12 degrees up when it is actually below the horizon.
    """
    if alt_km is None or alt_km <= 0:
        return None
    re_km = EARTH_RADIUS_NM * 1.852
    theta = (ground_dist_nm * 1.852) / re_km          # central angle, radians
    if theta >= math.pi:
        return None
    sin_t = math.sin(theta)
    if abs(sin_t) < 1e-9:
        return 90.0
    return math.degrees(math.atan((math.cos(theta) - re_km / (re_km + alt_km)) / sin_t))


def closest_approach(dist_nm_now, bearing_now, track_deg, gs_kt):
    """
    When and how close an aircraft holding its present track will pass the
    observer. Returns (minutes_until, miss_distance_nm), or None when it is
    not closing -- already past, stationary, or track unknown.

    Flat-earth vectors in nautical miles. Over the tens of miles this is used
    for, the great-circle correction is smaller than the error in a ground
    speed that was sampled seconds ago.
    """
    if not gs_kt or gs_kt < 30.0 or track_deg is None or track_deg < 0:
        return None
    if dist_nm_now is None:
        return None

    brg = math.radians(bearing_now)
    trk = math.radians(track_deg)

    # Position of the aircraft relative to the observer, and its velocity.
    rx = dist_nm_now * math.sin(brg)
    ry = dist_nm_now * math.cos(brg)
    vx = gs_kt * math.sin(trk)
    vy = gs_kt * math.cos(trk)

    vv = vx * vx + vy * vy
    if vv <= 0.0:
        return None

    # Time that minimises |r + v t|.
    t_hours = -(rx * vx + ry * vy) / vv
    if t_hours < 0.0:
        return None                      # closest approach already happened

    mx = rx + vx * t_hours
    my = ry + vy * t_hours
    return t_hours * 60.0, math.hypot(mx, my)


def route_progress(from_lat, from_lon, to_lat, to_lon, cur_lat, cur_lon):
    """
    Fraction of the trip completed, and how far is left.

    Measured as flown/(flown+remaining) rather than against the origin-to-
    destination distance directly: an aircraft that has diverted, held, or
    simply not flown the great circle would otherwise read as more than 100%
    complete while still hundreds of miles out.
    """
    flown = distance_nm(from_lat, from_lon, cur_lat, cur_lon)
    remaining = distance_nm(cur_lat, cur_lon, to_lat, to_lon)
    total = flown + remaining
    if total <= 0.0:
        return 1.0, 0.0, 0.0
    return flown / total, remaining, total


def great_circle_points(lat1, lon1, lat2, lon2, segments=48):
    """
    Points along the great circle between two airports, for drawing the route
    as the aeroplane actually flies it rather than as a straight line on a
    Mercator projection -- which for anything transcontinental is visibly and
    embarrassingly wrong.
    """
    phi1, lam1 = math.radians(lat1), math.radians(lon1)
    phi2, lam2 = math.radians(lat2), math.radians(lon2)

    d = 2 * math.asin(math.sqrt(
        math.sin((phi2 - phi1) / 2) ** 2 +
        math.cos(phi1) * math.cos(phi2) * math.sin((lam2 - lam1) / 2) ** 2))
    if d == 0.0:
        return [[lat1, lon1]]

    out = []
    for i in range(segments + 1):
        f = i / float(segments)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(phi1) * math.cos(lam1) + b * math.cos(phi2) * math.cos(lam2)
        y = a * math.cos(phi1) * math.sin(lam1) + b * math.cos(phi2) * math.sin(lam2)
        z = a * math.sin(phi1) + b * math.sin(phi2)
        out.append([math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))])
    return out
