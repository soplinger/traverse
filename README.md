# Traverse

**An offline land-navigation route planner for plain-text waypoint files.**

Traverse reads a readable `.trv` file of latitude/longitude waypoints and works out
every leg for you: distance, true, magnetic and back bearings, ascent, walking time,
a clock schedule with stops, and whether a loop closes. It can export GPX, reverse a
route, or project a point from a bearing and distance. It is one Python file, uses only
the standard library, and never touches the network.

## Features

- **Plain-text route format** with `#` comments: `title`, `speed`, `climb`,
  `declination`, `start` and `point` lines, so a route is easy to read, diff and share.
- **Line-numbered validation**: every error and warning points at `file:line`, including
  bad coordinates, unknown directives, bad options and missing points.
- **Strict lint mode** (`lint --strict`) that fails CI on warnings too.
- **Great-circle distance** per leg and in total, using the haversine formula.
- **True, magnetic and back bearings** per leg, with 16-point compass names.
- **Magnetic declination** in either sign convention (`1.5 W`, `-1.5`, `2 E`), so
  true bearings become the magnetic figures you read off a compass.
- **Naismith-style timing**: flat speed plus one hour per 600 m of climb, with ascent
  counted only where elevations are given.
- **Clock schedule** with arrival and departure times, per-point stops and `+Nd`
  day rollover for overnight routes.
- **Loop closure check** that reports the gap between start and finish and says whether
  the route is a loop.
- **Budget gate** (`report --budget HOURS`) that exits with code 3 when a route runs over
  time, for planning checks in scripts.
- **JSON report** (`report --json`) for other tools.
- **GPX 1.1 export** of waypoints and a track, with XML-safe names.
- **Reverse a route** into a new `.trv` file, ready to walk back.
- **Destination projection** (`dest`) from a start point, a magnetic bearing, a distance
  and a declination.
- **Round-trip serialisation**: a parsed route can be written back in the same format.

## Quick start

Lint the bundled example route:

```console
$ python3 traverse.py lint examples/ridge_loop.trv
examples/ridge_loop.trv: ok (6 points)
```

Print the full report (leg table, schedule, totals and closure status):

```console
$ python3 traverse.py report examples/ridge_loop.trv
```

The report prints a leg table with one row per leg (distance, true, magnetic and back
bearings, cardinal, ascent and time), then a schedule with arrive and depart times, then
totals and whether the loop closed.

Other commands:

```console
$ python3 traverse.py report examples/ridge_loop.trv --budget 4 --json
$ python3 traverse.py gpx examples/ridge_loop.trv -o ridge_loop.gpx
$ python3 traverse.py reverse examples/ridge_loop.trv -o ridge_loop_back.trv
$ python3 traverse.py dest 53.35 -1.80 90 2.5 --declination 1.5
```

### Route file format

```text
title Ridge Loop
speed 4.0            # walking speed on the flat, km/h
climb 600            # metres of ascent per hour
declination 1.5 W    # magnetic declination
start 08:30

point Trailhead 53.3500 -1.8000 elev=280
point Ridge     53.3640 -1.7750 elev=410 stop=10
point Trailhead 53.3500 -1.8000 elev=280 stop=20
```

Point names are single words (use `_` or `-` for spaces). Latitude and longitude are
decimal degrees. `elev` is in metres and `stop` is in minutes.

Exit codes: `0` success, `1` the file has errors (or warnings under `--strict`),
`3` the `--budget` was exceeded.

## How to run

Requires Python 3.10 or newer. No install step and no dependencies for the program itself.

```console
$ python3 traverse.py --help
$ python3 traverse.py report FILE.trv
```

## How to test

The test suite uses pytest. Install it once, then run from the project directory:

```console
$ python3 -m pip install pytest
$ python3 -m pytest
```

The tests cover distance and bearing maths, destination round-trips, declination signs,
Naismith timing, schedules and day rollover, closure detection, line-numbered parser
errors, warnings, GPX well-formedness, JSON output, and the CLI exit codes.

## Notes

- Distances and bearings use a spherical Earth (mean radius 6371.0088 km). The error
  against an ellipsoidal model can be around half a percent, so this is for planning
  walks, not for surveying.
- Declination is taken as given. Look up the value for your area and date; Traverse does
  not know it.
- Ascent counts only uphill gain. Descent is not added to time, following Naismith.
