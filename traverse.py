#!/usr/bin/env python3
"""Traverse: an offline land-navigation route planner.

Reads a plain-text file of waypoints (latitude/longitude in decimal degrees)
and works out, for every leg: great-circle distance, true, magnetic and back
bearings, ascent, and walking time (Naismith-style). It builds a clock
schedule with stops, checks whether a loop closes, and can export GPX,
reverse the route or project a destination from a bearing and distance.

Standard library only. No network access.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

EARTH_RADIUS_KM = 6371.0088
CLOSE_TOLERANCE_M = 50.0
DAY_MINUTES = 24 * 60
DIRECTIVES = ("title", "speed", "climb", "declination", "start", "point")
POINT_OPTIONS = ("elev", "stop")
CARDINALS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BUDGET = 3


@dataclass
class Point:
    name: str
    lat: float
    lon: float
    line: int
    elev: float | None = None
    stop: float = 0.0


@dataclass
class Route:
    title: str = "Untitled route"
    speed: float = 4.0  # km/h on the flat
    climb: float = 600.0  # metres of ascent per hour (Naismith uses 600)
    declination: float = 0.0  # degrees, east positive
    start: int = 8 * 60  # minutes after midnight
    points: list[Point] = field(default_factory=list)


@dataclass
class Issue:
    line: int  # 0 means file-level
    message: str
    level: str  # "error" or "warning"


@dataclass
class Leg:
    index: int
    origin: str
    destination: str
    distance_km: float
    true_bearing: float
    magnetic_bearing: float
    back_bearing: float
    ascent_m: float
    hours: float


# ---------------------------------------------------------------- parsing

def _number(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_route(text: str) -> tuple[Route, list[Issue]]:
    """Parse a route file. Returns the route and every problem found."""
    route = Route()
    issues: list[Issue] = []
    seen: set[str] = set()

    def problem(line: int, message: str, level: str = "error") -> None:
        issues.append(Issue(line, message, level))

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        words = line.split()
        key = words[0].lower()
        args = words[1:]

        if key not in DIRECTIVES:
            problem(lineno, f"unknown directive '{words[0]}'")
            continue
        if key != "point":
            if key in seen:
                problem(lineno, f"'{key}' set again; last value wins", "warning")
            seen.add(key)

        if key == "title":
            if not args:
                problem(lineno, "title needs some text")
            else:
                route.title = line.split(None, 1)[1]

        elif key in ("speed", "climb"):
            value = _number(args[0]) if len(args) == 1 else None
            if value is None or value <= 0:
                unit = "km/h" if key == "speed" else "metres per hour"
                problem(lineno, f"{key} needs one positive number ({unit})")
            elif key == "speed":
                route.speed = value
            else:
                route.climb = value

        elif key == "declination":
            match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([EWew])?", " ".join(args))
            if not match:
                problem(lineno, "declination needs degrees, optionally followed by E or W")
            else:
                value = float(match.group(1))
                hemisphere = (match.group(2) or "").upper()
                if hemisphere and value < 0:
                    problem(lineno, "use a sign or E/W for declination, not both")
                elif abs(value) > 180:
                    problem(lineno, "declination must be between -180 and 180 degrees")
                else:
                    route.declination = -value if hemisphere == "W" else value

        elif key == "start":
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", args[0]) if len(args) == 1 else None
            if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                problem(lineno, "start needs a time as HH:MM")
            else:
                route.start = int(match.group(1)) * 60 + int(match.group(2))

        elif key == "point":
            point = _parse_point(lineno, args, problem)
            if point is not None:
                route.points.append(point)

    if len(route.points) < 2:
        problem(0, "route needs at least two points")

    _lint_points(route, issues)
    issues.sort(key=lambda issue: issue.line)
    return route, issues


def _parse_point(lineno, args, problem) -> Point | None:
    if len(args) < 3:
        problem(lineno, "point needs NAME LAT LON")
        return None
    name, lat_token, lon_token = args[0], args[1], args[2]
    lat, lon = _number(lat_token), _number(lon_token)
    if lat is None or lon is None:
        problem(lineno, "latitude and longitude must be numbers")
        return None
    ok = True
    if not -90 <= lat <= 90:
        problem(lineno, f"latitude {lat:g} is outside -90..90")
        ok = False
    if not -180 <= lon <= 180:
        problem(lineno, f"longitude {lon:g} is outside -180..180")
        ok = False

    point = Point(name=name, lat=lat, lon=lon, line=lineno)
    for token in args[3:]:
        option, sep, value_text = token.partition("=")
        value = _number(value_text) if sep else None
        if option not in POINT_OPTIONS or value is None:
            problem(lineno, f"bad point option '{token}' (use elev=METRES or stop=MINUTES)")
            ok = False
        elif option == "elev":
            point.elev = value
        elif value < 0:
            problem(lineno, "stop must be zero or more minutes")
            ok = False
        else:
            point.stop = value
    return point if ok else None


def _lint_points(route: Route, issues: list[Issue]) -> None:
    """Warnings that do not stop a report from running."""
    if not route.points:
        return
    names: dict[str, int] = {}
    for previous, current in zip(route.points, route.points[1:]):
        if (previous.lat, previous.lon) == (current.lat, current.lon):
            issues.append(Issue(current.line, f"zero-length leg from '{previous.name}'", "warning"))
    first, last = route.points[0], route.points[-1]
    for point in route.points:
        # A loop returns to its start, so the final point may repeat the first one.
        closing_loop = (
            len(route.points) > 2
            and point is last
            and (point.name, point.lat, point.lon) == (first.name, first.lat, first.lon)
        )
        if closing_loop:
            continue
        if point.name in names:
            issues.append(Issue(point.line, f"name '{point.name}' also used on line {names[point.name]}", "warning"))
        else:
            names[point.name] = point.line
    with_elev = [p for p in route.points if p.elev is not None]
    if with_elev and len(with_elev) < len(route.points):
        for point in route.points:
            if point.elev is None:
                issues.append(Issue(point.line, f"no elev for '{point.name}'; ascent to or from it is not counted", "warning"))


def dump_route(route: Route) -> str:
    """Serialise a route back to the plain-text format."""
    decl = f"{-route.declination:g} W" if route.declination < 0 else f"{route.declination:g} E"
    lines = [
        f"title {route.title}",
        f"speed {route.speed:g}",
        f"climb {route.climb:g}",
        f"declination {decl}",
        f"start {fmt_clock(route.start)}",
    ]
    for point in route.points:
        options = ""
        if point.elev is not None:
            options += f" elev={point.elev:g}"
        if point.stop:
            options += f" stop={point.stop:g}"
        lines.append(f"point {point.name} {point.lat:.6f} {point.lon:.6f}{options}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- geometry

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing in degrees, 0..360, from point 1 towards point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return math.degrees(math.atan2(y, x)) % 360


def destination(lat: float, lon: float, bearing: float, distance_km: float) -> tuple[float, float]:
    """Point reached by walking distance_km on a true bearing from (lat, lon)."""
    delta = distance_km / EARTH_RADIUS_KM
    theta = math.radians(bearing)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon2 = (math.degrees(lam2) + 540) % 360 - 180
    return math.degrees(phi2), lon2


def cardinal(bearing: float) -> str:
    return CARDINALS[round(bearing / 22.5) % 16]


# ---------------------------------------------------------------- planning

def compute_legs(route: Route) -> list[Leg]:
    legs = []
    for index, (a, b) in enumerate(zip(route.points, route.points[1:]), start=1):
        distance = haversine_km(a.lat, a.lon, b.lat, b.lon)
        true = initial_bearing(a.lat, a.lon, b.lat, b.lon)
        ascent = 0.0
        if a.elev is not None and b.elev is not None:
            ascent = max(0.0, b.elev - a.elev)
        hours = distance / route.speed + ascent / route.climb
        legs.append(Leg(
            index=index,
            origin=a.name,
            destination=b.name,
            distance_km=distance,
            true_bearing=true,
            magnetic_bearing=(true - route.declination) % 360,
            back_bearing=initial_bearing(b.lat, b.lon, a.lat, a.lon),
            ascent_m=ascent,
            hours=hours,
        ))
    return legs


def build_schedule(route: Route, legs: list[Leg]) -> list[dict]:
    """Arrival and departure in minutes from midnight of day one."""
    rows = []
    clock = float(route.start)
    for position, point in enumerate(route.points):
        departure = clock + point.stop
        rows.append({"name": point.name, "arrive": clock, "depart": departure, "stop": point.stop})
        if position < len(legs):
            clock = departure + legs[position].hours * 60
    return rows


def summarise(route: Route, legs: list[Leg], schedule: list[dict]) -> dict:
    first, last = route.points[0], route.points[-1]
    gap_m = haversine_km(first.lat, first.lon, last.lat, last.lon) * 1000
    moving = sum(leg.hours for leg in legs)
    stops = sum(row["stop"] for row in schedule) / 60
    return {
        "distance_km": sum(leg.distance_km for leg in legs),
        "ascent_m": sum(leg.ascent_m for leg in legs),
        "moving_hours": moving,
        "stop_hours": stops,
        "elapsed_hours": moving + stops,
        "finish": schedule[-1]["depart"],
        "closure_gap_m": gap_m,
        "closed": gap_m <= CLOSE_TOLERANCE_M,
    }


# ---------------------------------------------------------------- formatting

def fmt_clock(minutes: float) -> str:
    total = round(minutes)
    days, rest = divmod(total, DAY_MINUTES)
    hours, mins = divmod(rest, 60)
    suffix = f" (+{days}d)" if days else ""
    return f"{hours:02d}:{mins:02d}{suffix}"


def fmt_duration(hours: float) -> str:
    total = round(hours * 60)
    return f"{total // 60}:{total % 60:02d}"


def render_report(route: Route, legs: list[Leg], schedule: list[dict], summary: dict) -> str:
    out = [
        route.title,
        f"Start {fmt_clock(route.start)}  speed {route.speed:g} km/h  "
        f"climb {route.climb:g} m/h  declination {dump_declination(route)}",
        "",
        f"{'#':>3}  {'From':<12} {'To':<12} {'Dist km':>8} {'True':>6} {'Mag':>6} "
        f"{'Back':>6} {'Card':>4} {'Ascent m':>8} {'Time':>6}",
    ]
    for leg in legs:
        out.append(
            f"{leg.index:>3}  {leg.origin:<12} {leg.destination:<12} {leg.distance_km:>8.2f} "
            f"{leg.true_bearing:>6.1f} {leg.magnetic_bearing:>6.1f} {leg.back_bearing:>6.1f} "
            f"{cardinal(leg.true_bearing):>4} {leg.ascent_m:>8.0f} {fmt_duration(leg.hours):>6}"
        )
    out += ["", f"{'Point':<14} {'Arrive':>12} {'Depart':>12} {'Stop min':>8}"]
    for row in schedule:
        out.append(
            f"{row['name']:<14} {fmt_clock(row['arrive']):>12} {fmt_clock(row['depart']):>12} "
            f"{row['stop']:>8g}"
        )
    closure = (
        f"loop closed (gap {summary['closure_gap_m']:.0f} m)"
        if summary["closed"]
        else f"open route: first and last points are {summary['closure_gap_m']:.0f} m apart"
    )
    out += [
        "",
        f"Total distance  {summary['distance_km']:.2f} km",
        f"Total ascent    {summary['ascent_m']:.0f} m",
        f"Moving time     {fmt_duration(summary['moving_hours'])}",
        f"Stop time       {fmt_duration(summary['stop_hours'])}",
        f"Elapsed         {fmt_duration(summary['elapsed_hours'])}",
        f"Finish          {fmt_clock(summary['finish'])}",
        f"Closure         {closure}",
    ]
    return "\n".join(out) + "\n"


def dump_declination(route: Route) -> str:
    if route.declination < 0:
        return f"{-route.declination:g} W"
    return f"{route.declination:g} E"


def report_json(route: Route, legs: list[Leg], schedule: list[dict], summary: dict) -> str:
    data = {
        "title": route.title,
        "speed_kmh": route.speed,
        "climb_m_per_h": route.climb,
        "declination_deg_east": route.declination,
        "legs": [
            {
                "index": leg.index,
                "from": leg.origin,
                "to": leg.destination,
                "distance_km": round(leg.distance_km, 3),
                "true_bearing": round(leg.true_bearing, 1),
                "magnetic_bearing": round(leg.magnetic_bearing, 1),
                "back_bearing": round(leg.back_bearing, 1),
                "ascent_m": round(leg.ascent_m, 1),
                "hours": round(leg.hours, 3),
            }
            for leg in legs
        ],
        "schedule": [
            {"name": row["name"], "arrive": fmt_clock(row["arrive"]),
             "depart": fmt_clock(row["depart"]), "stop_min": row["stop"]}
            for row in schedule
        ],
        "summary": {key: (round(value, 3) if isinstance(value, float) else value)
                    for key, value in summary.items() if key != "finish"}
        | {"finish": fmt_clock(summary["finish"])},
    }
    return json.dumps(data, indent=2) + "\n"


def _gpx_point(point: Point, tag: str, indent: str) -> list[str]:
    lines = [f'{indent}<{tag} lat="{point.lat:.6f}" lon="{point.lon:.6f}">']
    if point.elev is not None:
        lines.append(f"{indent}  <ele>{point.elev:g}</ele>")
    lines.append(f"{indent}  <name>{escape(point.name)}</name>")
    lines.append(f"{indent}</{tag}>")
    return lines


def render_gpx(route: Route) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Traverse" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{escape(route.title)}</name></metadata>",
    ]
    for point in route.points:
        lines += _gpx_point(point, "wpt", "  ")
    lines += ["  <trk>", f"    <name>{escape(route.title)}</name>", "    <trkseg>"]
    for point in route.points:
        lines += _gpx_point(point, "trkpt", "      ")
    lines += ["    </trkseg>", "  </trk>", "</gpx>"]
    return "\n".join(lines) + "\n"


def reverse_route(route: Route) -> Route:
    return Route(
        title=f"{route.title} (reversed)",
        speed=route.speed,
        climb=route.climb,
        declination=route.declination,
        start=route.start,
        points=list(reversed(route.points)),
    )


# ---------------------------------------------------------------- CLI

def _load(path: str) -> tuple[Route | None, list[Issue]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        print(f"traverse: cannot read {path}: {error.strerror}", file=sys.stderr)
        return None, []
    route, issues = parse_route(text)
    return route, issues


def _print_issues(path: str, issues: list[Issue]) -> None:
    for issue in issues:
        where = f"{path}:{issue.line}" if issue.line else path
        print(f"{where}: {issue.level}: {issue.message}", file=sys.stderr)


def _has_errors(issues: list[Issue], strict: bool) -> bool:
    return any(issue.level == "error" or (strict and issue.level == "warning") for issue in issues)


def cmd_report(args) -> int:
    route, issues = _load(args.file)
    if route is None:
        return EXIT_INVALID
    _print_issues(args.file, issues)
    if _has_errors(issues, strict=False):
        return EXIT_INVALID
    legs = compute_legs(route)
    schedule = build_schedule(route, legs)
    summary = summarise(route, legs, schedule)
    if args.json:
        sys.stdout.write(report_json(route, legs, schedule, summary))
    else:
        sys.stdout.write(render_report(route, legs, schedule, summary))
    if args.budget is not None and summary["elapsed_hours"] > args.budget:
        print(
            f"traverse: elapsed {fmt_duration(summary['elapsed_hours'])} is over the "
            f"budget of {fmt_duration(args.budget)}",
            file=sys.stderr,
        )
        return EXIT_BUDGET
    return EXIT_OK


def cmd_lint(args) -> int:
    route, issues = _load(args.file)
    if route is None:
        return EXIT_INVALID
    if not issues:
        print(f"{args.file}: ok ({len(route.points)} points)")
        return EXIT_OK
    _print_issues(args.file, issues)
    return EXIT_INVALID if _has_errors(issues, strict=args.strict) else EXIT_OK


def cmd_gpx(args) -> int:
    route, issues = _load(args.file)
    if route is None:
        return EXIT_INVALID
    _print_issues(args.file, issues)
    if _has_errors(issues, strict=False):
        return EXIT_INVALID
    xml = render_gpx(route)
    if args.output == "-":
        sys.stdout.write(xml)
    else:
        Path(args.output).write_text(xml, encoding="utf-8")
        print(f"wrote {len(route.points)} points to {args.output}")
    return EXIT_OK


def cmd_reverse(args) -> int:
    route, issues = _load(args.file)
    if route is None:
        return EXIT_INVALID
    _print_issues(args.file, issues)
    if _has_errors(issues, strict=False):
        return EXIT_INVALID
    text = dump_route(reverse_route(route))
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote reversed route to {args.output}")
    return EXIT_OK


def cmd_dest(args) -> int:
    if not -90 <= args.lat <= 90 or not -180 <= args.lon <= 180:
        print("traverse: latitude must be -90..90 and longitude -180..180", file=sys.stderr)
        return EXIT_INVALID
    if args.km < 0:
        print("traverse: distance must not be negative", file=sys.stderr)
        return EXIT_INVALID
    true_bearing = (args.bearing + args.declination) % 360
    lat, lon = destination(args.lat, args.lon, true_bearing, args.km)
    print(f"{lat:.6f} {lon:.6f}")
    print(f"true bearing {true_bearing:.1f} ({cardinal(true_bearing)}), "
          f"magnetic input {args.bearing % 360:.1f}", file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traverse", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="print the leg table, schedule and totals")
    report.add_argument("file")
    report.add_argument("--json", action="store_true", help="emit JSON instead of text")
    report.add_argument("--budget", type=float, metavar="HOURS",
                        help="exit 3 if elapsed time (moving plus stops) exceeds HOURS")
    report.set_defaults(func=cmd_report)

    lint = sub.add_parser("lint", help="check a route file for errors and warnings")
    lint.add_argument("file")
    lint.add_argument("--strict", action="store_true", help="treat warnings as failures")
    lint.set_defaults(func=cmd_lint)

    gpx = sub.add_parser("gpx", help="export waypoints and track as GPX 1.1")
    gpx.add_argument("file")
    gpx.add_argument("-o", "--output", default="-", help="output path, or - for stdout")
    gpx.set_defaults(func=cmd_gpx)

    reverse = sub.add_parser("reverse", help="write the route in reverse order")
    reverse.add_argument("file")
    reverse.add_argument("-o", "--output", default="-", help="output path, or - for stdout")
    reverse.set_defaults(func=cmd_reverse)

    dest = sub.add_parser("dest", help="project a point from a start, bearing and distance")
    dest.add_argument("lat", type=float)
    dest.add_argument("lon", type=float)
    dest.add_argument("bearing", type=float, help="magnetic bearing in degrees")
    dest.add_argument("km", type=float, help="distance in kilometres")
    dest.add_argument("--declination", type=float, default=0.0,
                      help="magnetic declination in degrees, east positive")
    dest.set_defaults(func=cmd_dest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
