import json
import math
import sys
from pathlib import Path
from xml.dom import minidom

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import traverse  # noqa: E402

SAMPLE = ROOT / "examples" / "ridge_loop.trv"

SIMPLE = """\
title Two Hops
speed 5
climb 600
declination 0
start 09:00
point A 0 0
point B 0 0.1 stop=15
point C 0.1 0.1
"""


def parse_ok(text):
    route, issues = traverse.parse_route(text)
    assert not [i for i in issues if i.level == "error"], issues
    return route


def errors_for(text):
    _, issues = traverse.parse_route(text)
    return [(i.line, i.message) for i in issues if i.level == "error"]


def test_haversine_one_degree_of_longitude_on_equator():
    km = traverse.haversine_km(0, 0, 0, 1)
    assert km == pytest.approx(111.195, abs=0.01)


def test_initial_bearing_cardinal_directions():
    assert traverse.initial_bearing(0, 0, 1, 0) == pytest.approx(0, abs=1e-6)
    assert traverse.initial_bearing(0, 0, 0, 1) == pytest.approx(90, abs=1e-6)
    assert traverse.initial_bearing(0, 0, -1, 0) == pytest.approx(180, abs=1e-6)
    assert traverse.initial_bearing(0, 0, 0, -1) == pytest.approx(270, abs=1e-6)


def test_back_bearing_of_eastward_leg_is_west():
    back = traverse.initial_bearing(0, 1, 0, 0)
    assert back == pytest.approx(270, abs=1e-6)


def test_destination_round_trips_with_distance_and_bearing():
    lat, lon = traverse.destination(51.5, -0.12, 37.0, 12.5)
    assert traverse.haversine_km(51.5, -0.12, lat, lon) == pytest.approx(12.5, abs=1e-6)
    assert traverse.initial_bearing(51.5, -0.12, lat, lon) == pytest.approx(37.0, abs=1e-4)


def test_destination_wraps_longitude_across_antimeridian():
    lat, lon = traverse.destination(0, 179.9, 90, 50)
    assert -180 <= lon <= 180
    assert lon < 0


def test_cardinal_names_wrap_around_north():
    assert traverse.cardinal(0) == "N"
    assert traverse.cardinal(359) == "N"
    assert traverse.cardinal(45) == "NE"
    assert traverse.cardinal(225) == "SW"


def test_magnetic_bearing_subtracts_east_declination():
    route = parse_ok(SIMPLE.replace("declination 0", "declination 10 E"))
    legs = traverse.compute_legs(route)
    assert legs[0].magnetic_bearing == pytest.approx((legs[0].true_bearing - 10) % 360)


def test_west_declination_is_negative_and_round_trips():
    route = parse_ok(SIMPLE.replace("declination 0", "declination 1.5 W"))
    assert route.declination == pytest.approx(-1.5)
    assert "declination -1.5" not in traverse.dump_route(route)
    assert "declination 1.5 W" in traverse.dump_route(route)


def test_naismith_time_adds_ascent_at_climb_rate():
    text = """\
speed 4
climb 600
point Low 0 0 elev=100
point High 0 0.1 elev=700
"""
    route = parse_ok(text)
    leg = traverse.compute_legs(route)[0]
    flat_hours = leg.distance_km / 4
    assert leg.ascent_m == pytest.approx(600)
    assert leg.hours == pytest.approx(flat_hours + 1.0)


def test_descent_is_not_counted_as_ascent():
    text = "point Top 0 0 elev=700\npoint Bottom 0 0.1 elev=100\n"
    leg = traverse.compute_legs(parse_ok(text))[0]
    assert leg.ascent_m == 0


def test_schedule_adds_stops_and_travel_time():
    route = parse_ok(SIMPLE)
    legs = traverse.compute_legs(route)
    rows = traverse.build_schedule(route, legs)
    assert rows[0]["arrive"] == 9 * 60
    assert rows[1]["depart"] == rows[1]["arrive"] + 15
    assert rows[2]["arrive"] == pytest.approx(rows[1]["depart"] + legs[1].hours * 60)


def test_fmt_clock_rolls_over_midnight_with_day_count():
    assert traverse.fmt_clock(23 * 60 + 50) == "23:50"
    assert traverse.fmt_clock(24 * 60 + 5) == "00:05 (+1d)"


def test_loop_closure_detected_for_sample():
    route = traverse.parse_route(SAMPLE.read_text(encoding="utf-8"))[0]
    legs = traverse.compute_legs(route)
    summary = traverse.summarise(route, legs, traverse.build_schedule(route, legs))
    assert summary["closed"] is True


def test_open_route_reports_gap():
    route = parse_ok("point A 0 0\npoint B 0 1\n")
    legs = traverse.compute_legs(route)
    summary = traverse.summarise(route, legs, traverse.build_schedule(route, legs))
    assert summary["closed"] is False
    assert summary["closure_gap_m"] > 100_000


def test_parser_reports_line_numbers_for_bad_input():
    text = "title ok\nspeed fast\npoint A 91 0\nbogus line\npoint B 0 0\n"
    found = errors_for(text)
    assert (2, "speed needs one positive number (km/h)") in found
    assert any(line == 3 and "latitude 91" in msg for line, msg in found)
    assert any(line == 4 and "unknown directive" in msg for line, msg in found)


def test_route_with_fewer_than_two_points_is_an_error():
    found = errors_for("point Solo 0 0\n")
    assert (0, "route needs at least two points") in found


def test_point_options_are_validated():
    found = errors_for("point A 0 0\npoint B 0 1 colour=red\npoint C 0 2 stop=-5\n")
    assert any(line == 2 and "bad point option" in msg for line, msg in found)
    assert any(line == 3 and "stop must be" in msg for line, msg in found)


def test_declination_rejects_sign_with_hemisphere_and_out_of_range():
    assert errors_for("declination -3 W\npoint A 0 0\npoint B 0 1\n")[0][0] == 1
    assert errors_for("declination 200\npoint A 0 0\npoint B 0 1\n")[0][0] == 1


def test_warnings_for_zero_length_legs_and_duplicate_names():
    _, issues = traverse.parse_route("point A 0 0\npoint A 0 0\n")
    levels = {(i.level, i.message.split(" ")[0]) for i in issues}
    assert ("warning", "zero-length") in levels
    assert ("warning", "name") in levels


def test_warnings_do_not_block_report_but_strict_lint_fails(tmp_path, capsys):
    route_file = tmp_path / "dupes.trv"
    route_file.write_text("point A 0 0\npoint A 0 0.1\n", encoding="utf-8")
    assert traverse.main(["report", str(route_file)]) == traverse.EXIT_OK
    assert traverse.main(["lint", str(route_file)]) == traverse.EXIT_OK
    assert traverse.main(["lint", "--strict", str(route_file)]) == traverse.EXIT_INVALID


def test_dump_then_parse_round_trips_points_and_options():
    route = parse_ok(SAMPLE.read_text(encoding="utf-8"))
    again = parse_ok(traverse.dump_route(route))
    assert [p.name for p in again.points] == [p.name for p in route.points]
    assert [p.stop for p in again.points] == [p.stop for p in route.points]
    assert again.declination == pytest.approx(route.declination)
    assert again.start == route.start


def test_reverse_route_flips_point_order_and_title():
    route = parse_ok(SIMPLE)
    reversed_route = traverse.reverse_route(route)
    assert [p.name for p in reversed_route.points] == ["C", "B", "A"]
    assert reversed_route.title == "Two Hops (reversed)"


def test_gpx_is_well_formed_and_escapes_names():
    route = parse_ok("title A & B\npoint <Top> 1 2 elev=5\npoint Base 1.5 2.5\n")
    document = minidom.parseString(traverse.render_gpx(route))
    names = [n.firstChild.nodeValue for n in document.getElementsByTagName("name")]
    assert "<Top>" in names
    assert len(document.getElementsByTagName("wpt")) == 2
    assert len(document.getElementsByTagName("trkpt")) == 2


def test_json_report_has_legs_schedule_and_summary():
    route = parse_ok(SIMPLE)
    legs = traverse.compute_legs(route)
    schedule = traverse.build_schedule(route, legs)
    summary = traverse.summarise(route, legs, schedule)
    data = json.loads(traverse.report_json(route, legs, schedule, summary))
    assert len(data["legs"]) == 2
    assert data["schedule"][0]["arrive"] == "09:00"
    assert data["summary"]["closed"] is False
    assert isinstance(data["summary"]["finish"], str)


def test_cli_report_budget_gate_returns_exit_three(tmp_path, capsys):
    route_file = tmp_path / "walk.trv"
    route_file.write_text(SIMPLE, encoding="utf-8")
    assert traverse.main(["report", str(route_file), "--budget", "10"]) == traverse.EXIT_OK
    assert traverse.main(["report", str(route_file), "--budget", "0.01"]) == traverse.EXIT_BUDGET


def test_cli_refuses_invalid_file_with_exit_one(tmp_path, capsys):
    route_file = tmp_path / "bad.trv"
    route_file.write_text("point A 99 0\n", encoding="utf-8")
    assert traverse.main(["report", str(route_file)]) == traverse.EXIT_INVALID
    assert "bad.trv:1: error" in capsys.readouterr().err


def test_cli_sample_file_lints_clean(capsys):
    assert traverse.main(["lint", str(SAMPLE)]) == traverse.EXIT_OK
    assert "ok (6 points)" in capsys.readouterr().out


def test_cli_dest_applies_declination(capsys):
    assert traverse.main(["dest", "0", "0", "80", "10", "--declination", "10"]) == traverse.EXIT_OK
    lat, lon = capsys.readouterr().out.split()
    assert float(lat) == pytest.approx(0, abs=1e-6)
    assert float(lon) == pytest.approx(traverse.destination(0, 0, 90, 10)[1], abs=1e-6)


def test_missing_file_is_reported_not_raised(tmp_path, capsys):
    assert traverse.main(["report", str(tmp_path / "nope.trv")]) == traverse.EXIT_INVALID
    assert "cannot read" in capsys.readouterr().err
