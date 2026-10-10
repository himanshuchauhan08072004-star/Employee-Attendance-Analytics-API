"""Unit tests: pure rule helpers and request validation. No MongoDB needed."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main as m

IST = m.IST


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(m.UTC)


# ---- R2 late ---------------------------------------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 30, 0, 0), (9, 0, 0, 0), (9, 40, 30, 10),
])
def test_late_boundaries(h, mi, s, expected):
    d = date(2026, 7, 6)
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), d, "09:30") == expected


# ---- R3 overtime -----------------------------------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [(19, 0, 0, 30), (18, 59, 59, 0), (19, 10, 0, 40), (18, 35, 0, 0)])
def test_overtime_threshold(h, mi, s, expected):
    assert m.compute_overtime(ist(2026, 7, 6, h, mi, s), date(2026, 7, 6), "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    out = ist(2026, 7, 7, 6, 40)
    assert m.compute_overtime(out, date(2026, 7, 6), "22:00", "06:00") == 40


# ---- R4 / R5 ---------------------------------------------------------------
def test_work_hours_half_up_not_bankers():
    pin = ist(2026, 7, 6, 10, 0)
    assert m.compute_work_hours(pin, pin + timedelta(seconds=29250)) == 8.13  # 8.125 -> half-up
    assert round(8.125, 2) == 8.12  # what the starter would have produced


def test_half_day_uses_rounded_value():
    pin = ist(2026, 7, 6, 10, 0)
    wh = m.compute_work_hours(pin, pin + timedelta(seconds=16182))  # 4.495 -> 4.50
    assert wh == 4.5 and m.compute_half_day(wh) is False
    wh = m.compute_work_hours(pin, pin + timedelta(seconds=16146))  # 4.485 -> 4.49
    assert wh == 4.49 and m.compute_half_day(wh) is True


# ---- R1 attendance date ----------------------------------------------------
def test_attendance_date_normal_shift_uses_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th: date must be the 7th
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == date(2026, 7, 7)


def test_attendance_date_overnight():
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == date(2026, 7, 7)


def test_ms_conversion_truncates_to_seconds_and_roundtrips():
    dt = m.ms_to_dt(1_783_312_500_900)
    assert dt.microsecond == 0 and m.dt_to_ms(dt) == 1_783_312_500_000


def test_naive_datetime_treated_as_utc():
    assert m.dt_to_ms(datetime(1970, 1, 2)) == 86_400_000


def test_to_ms_deep_converts_nested_history():
    t = datetime(2026, 7, 7, 4, 35, tzinfo=m.UTC)
    out = m.to_ms_deep({"punch_in": {"from": t, "to": None}, "late_minutes": {"from": 35, "to": 0}})
    assert out["punch_in"]["from"] == m.dt_to_ms(t) and out["late_minutes"]["from"] == 35


def test_serialize_legacy_record_defaults():
    rec = m.serialize_record({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                              "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=m.UTC), "punch_out": None,
                              "work_hours": None, "late_minutes": 0})
    assert rec["history"] == [] and rec["half_day"] is False and rec["overtime_minutes"] == 0
    assert "_id" not in rec


def test_month_bounds_and_weekdays():
    assert m.month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert m.month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 15), date(2026, 7, 31)) == 13


def test_half_up_helper():
    assert m.half_up(0.12345, 4) == 0.1235 and m.half_up(2.675, 2) == 2.68


# ---- validation (422 happens before any DB access) --------------------------
@pytest.fixture(scope="module")
def client():
    return TestClient(m.app)  # no lifespan: no DB needed


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.5, "1783312500000", True, 99_999_999_999, 4_102_444_800_001])
def test_punch_in_rejects_bad_timestamps(client, bad):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422


def test_punch_in_rejects_float_with_integral_value(client):
    r = client.post("/attendance/punch-in", content='{"emp_code":"EMP0001","punched_at":1783312500000.0}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_punch_in_rejects_absent_status(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "EMP0001", "email": "bad"}, {"emp_code": "EMP0001", "joined_on": "2026-02-30"},
    {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "09:30"}, {"emp_code": "EMP0001", "shift_start": "9:30"},
])
def test_employee_validation(client, patch):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-05", **patch}
    assert client.post("/employees", json=body).status_code == 422


@pytest.mark.parametrize("url", [
    "/employees?page=0", "/employees?page_size=101", "/employees?page_size=0", "/attendance?page=0",
    "/attendance?date_from=2026-07-10&date_to=2026-07-01", "/attendance?date_from=2026-02-30", "/attendance?status=NOPE",
    "/analytics/departments/summary", "/analytics/departments/summary?month=2026-13",
    "/analytics/leaderboard/late?month=2026-07&limit=0", "/analytics/leaderboard/late?month=2026-07&limit=51",
    "/analytics/departments/Eng/trend?from=2026-07-10&to=2026-07-01",
    "/analytics/departments/Eng/trend?from=2026-01-01&to=2026-06-01",
    "/analytics/departments/Eng/trend?from=2026-07-01",
    "/admin/explain/nope", "/admin/explain/department_trend", "/admin/explain/employee_monthly?month=2026-07",
    "/admin/explain/department_summary", "/admin/explain/late_leaderboard?month=bad",
])
def test_query_validation_422(client, url):
    assert client.get(url).status_code == 422


def test_trend_range_limits():
    assert m.check_trend_range("2026-07-01", "2026-09-30")  # 92 days inclusive: allowed
    with pytest.raises(m.HTTPException) as e:
        m.check_trend_range("2026-07-01", "2026-10-01")  # 93 days: 422
    assert e.value.status_code == 422
