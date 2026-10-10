"""Integration tests against a REAL MongoDB through real HTTP. Skipped if MongoDB is unreachable."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from app import main as m

UTC, IST = m.UTC, m.IST


def ms(y, mo, d, h, mi, s=0, tz=IST, extra_ms=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()) * 1000 + extra_ms


def emp(api, code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
    r = api.post("/employees", json={"emp_code": code, "name": f"N {code}", "email": f"{code}@x.io",
                                      "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def pin(api, code, at, status=None):
    body = {"emp_code": code, "punched_at": at}
    if status:
        body["status"] = status
    return api.post("/attendance/punch-in", json=body)


# ---------------------------------------------------------------- employees
def test_employee_create_shape_and_duplicate(api):
    e = emp(api)
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(e["created_at"], int) and e["created_at"] > m.MIN_MS
    assert e["shift_start"] == "09:30" and "_id" not in e
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "x", "email": "a@b.co",
                                         "department": "D", "joined_on": "2026-01-01"}).status_code == 409


def test_concurrent_duplicate_employee_creates(http):
    body = {"emp_code": "EMP7777", "name": "x", "email": "a@b.co", "department": "D", "joined_on": "2026-01-01"}
    m.employees.delete_many({})

    def go(_):
        with http() as c:
            return c.post("/employees", json=body).status_code
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(go, range(10)))
    assert sorted(codes) == [201] + [409] * 9
    assert m.employees.count_documents({"emp_code": "EMP7777"}) == 1


def test_employee_list_pagination_filter_sort(api):
    for i in range(1, 8):
        emp(api, f"EMP{i:04d}", "Eng" if i % 2 else "Ops")
    r = api.get("/employees", params={"department": "Eng", "page": 2, "page_size": 3}).json()
    assert r["total"] == 4 and [e["emp_code"] for e in r["items"]] == ["EMP0007"] and r["page"] == 2
    r = api.get("/employees", params={"page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003"] and r["total"] == 7
    assert api.get("/employees", params={"page": 99}).json()["items"] == []


# ---------------------------------------------------------------- punch-in
def test_punch_in_unknown_employee_404(api):
    assert pin(api, "EMP9999", ms(2026, 7, 6, 9, 30)).status_code == 404


def test_punch_in_late_boundaries_and_truncation(api):
    for code, (h, mi, s, x), expected in [("EMP0001", (9, 40, 0, 900), 0), ("EMP0002", (9, 40, 1, 0), 10),
                                           ("EMP0003", (10, 15, 59, 0), 45)]:
        emp(api, code)
        r = pin(api, code, ms(2026, 7, 6, h, mi, s, extra_ms=x))
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["late_minutes"] == expected and j["date"] == "2026-07-06" and j["punch_in"] % 1000 == 0
        assert j["punch_out"] is None and j["work_hours"] is None and j["half_day"] is False and j["history"] == []


def test_punch_in_date_is_ist_not_utc(api):
    emp(api)
    assert pin(api, "EMP0001", ms(2026, 7, 7, 0, 30)).json()["date"] == "2026-07-07"


def test_punch_in_statuses(api):
    emp(api)
    assert pin(api, "EMP0001", ms(2026, 7, 6, 9, 0), "WFH").json()["status"] == "WFH"
    emp(api, "EMP0002")
    assert pin(api, "EMP0002", ms(2026, 7, 6, 9, 0), "LEAVE").status_code == 422


def test_duplicate_and_concurrent_punch_in(api, http):
    emp(api)
    assert pin(api, "EMP0001", ms(2026, 7, 6, 9, 0)).status_code == 201
    assert pin(api, "EMP0001", ms(2026, 7, 6, 12, 0)).status_code == 409
    emp(api, "EMP0002")

    def go(_):
        with http() as c:
            return pin(c, "EMP0002", ms(2026, 7, 6, 9, 0)).status_code
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(go, range(12)))
    assert sorted(codes) == [201] + [409] * 11


# ---------------------------------------------------------------- punch-out
def test_punch_out_rules(api):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 6, 10, 0))
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    j = r.json()
    assert r.status_code == 200 and j["overtime_minutes"] == 40 and j["work_hours"] == 9.17 and j["half_day"] is False
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20, 0)}).status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 5, 9, 0)}).status_code == 404


def test_punch_out_half_up_and_half_day_boundary(api):
    emp(api, "EMP0001")
    emp(api, "EMP0002")
    base = ms(2026, 7, 6, 10, 0)
    pin(api, "EMP0001", base)
    pin(api, "EMP0002", base)
    a = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": base + 29250 * 1000}).json()
    assert a["work_hours"] == 8.13
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": base + 16182 * 1000}).json()
    assert b["work_hours"] == 4.5 and b["half_day"] is False
    emp(api, "EMP0003")
    pin(api, "EMP0003", base)
    c = api.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": base + 16146 * 1000}).json()
    assert c["work_hours"] == 4.49 and c["half_day"] is True


def test_punch_out_invalid_times_422(api):
    emp(api)
    base = ms(2026, 7, 6, 10, 0)
    pin(api, "EMP0001", base)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": base}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": base + 24 * 3600 * 1000 + 1000}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 12345}).status_code == 422


def test_overnight_shift_punch_out_next_day(api):
    emp(api, "EMP0005", start="22:00", end="06:00")
    j = pin(api, "EMP0005", ms(2026, 7, 6, 21, 55)).json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 0
    o = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert o["date"] == "2026-07-06" and o["overtime_minutes"] == 40 and o["work_hours"] == 8.75
    # early-morning punch-in belongs to the previous day's shift
    emp(api, "EMP0006", start="22:00", end="06:00")
    assert pin(api, "EMP0006", ms(2026, 7, 7, 5, 0)).json()["date"] == "2026-07-06"


def test_concurrent_punch_out_exactly_one_200(api, http):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 6, 10, 0))
    out = ms(2026, 7, 6, 19, 0)

    def go(_):
        with http() as c:
            return c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(go, range(12)))
    assert sorted(codes) == [200] + [409] * 11


# ---------------------------------------------------------------- regularize
def reg(api, code, d, **kw):
    body = {"reason": "biometric glitch", "regularized_by": "hr.admin", **kw}
    return api.patch(f"/attendance/{code}/{d}", json=body)


def test_regularize_recomputes_and_appends_history(api):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 7, 10, 5))
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    r = reg(api, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28))
    j = r.json()
    assert r.status_code == 200 and j["late_minutes"] == 0 and j["work_hours"] == 9.03
    assert len(j["history"]) == 1
    h = j["history"][0]
    assert h["by"] == "hr.admin" and set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert h["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0} and isinstance(h["at"], int)
    r2 = reg(api, "EMP0001", "2026-07-07", status="WFH")
    assert [list(x["changes"]) for x in r2.json()["history"]] == [list(h["changes"]), ["status"]]
    got = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(got["history"]) == 2 and isinstance(got["history"][0]["changes"]["punch_in"]["from"], int)


def test_regularize_errors(api):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 7, 10, 5))
    d = "2026-07-07"
    assert reg(api, "EMP9999", d, status="WFH").status_code == 404
    assert reg(api, "EMP0001", "2026-07-20", status="WFH").status_code == 404
    assert reg(api, "EMP0001", d, status="PRESENT").status_code == 422  # changes nothing
    assert reg(api, "EMP0001", d, status="LEAVE", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422
    assert reg(api, "EMP0001", d, punch_in=ms(2026, 7, 8, 9, 0)).status_code == 422  # leaves the attendance date
    assert reg(api, "EMP0001", d, punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422  # before punch_in
    assert reg(api, "EMP0001", d, punch_out=ms(2026, 7, 8, 11, 0)).status_code == 422  # > 24h
    assert api.patch(f"/attendance/EMP0001/{d}", json={"reason": "x", "regularized_by": "a"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-13-01", json={"reason": "valid reason", "regularized_by": "a"}).status_code == 422


def test_regularize_to_leave_clears_times_and_presence_needs_punch_in(api):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 7, 10, 5))
    j = reg(api, "EMP0001", "2026-07-07", status="LEAVE").json()
    assert j["status"] == "LEAVE" and j["punch_in"] is None and j["late_minutes"] == 0
    assert set(j["history"][0]["changes"]) == {"status", "punch_in", "late_minutes"}
    assert reg(api, "EMP0001", "2026-07-07", status="PRESENT").status_code == 422
    assert reg(api, "EMP0001", "2026-07-07", status="PRESENT", punch_in=ms(2026, 7, 7, 9, 30)).status_code == 200


def test_concurrent_regularize_keeps_all_history(api, http):
    emp(api)
    pin(api, "EMP0001", ms(2026, 7, 7, 10, 5))

    def go(i):
        with http() as c:
            return reg(c, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 0, i + 1)).status_code
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(go, range(8)))
    assert set(codes) <= {200, 409} and codes.count(200) >= 1
    rec = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(rec["history"]) == codes.count(200)  # every success left exactly one entry


def test_legacy_document_reads(api):
    emp(api, "EMP0002")
    m.attendance.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                             "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
                             "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98,
                             "late_minutes": 0, "overtime_minutes": 0})
    j = api.get("/attendance").json()["items"][0]
    assert j["history"] == [] and j["half_day"] is False
    assert reg(api, "EMP0002", "2026-07-14", status="WFH").json()["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


# ---------------------------------------------------------------- attendance list
def test_attendance_list_filters_sort_pagination(api):
    emp(api, "EMP0001")
    emp(api, "EMP0002")
    for d in (6, 7, 8):
        for c in ("EMP0001", "EMP0002"):
            pin(api, c, ms(2026, 7, d, 9, 0))
    r = api.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 6 and [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07", "emp_code": "EMP0002"}).json()
    assert r["total"] == 1 and r["items"][0]["date"] == "2026-07-07"
    assert api.get("/attendance", params={"status": "LEAVE"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert api.get("/attendance", params={"page": 2, "page_size": 4}).json()["total"] == 6


# ---------------------------------------------------------------- analytics fixtures
def seed_analytics():
    m.employees.delete_many({})
    m.attendance.delete_many({})

    def e(code, dept, joined):
        m.employees.insert_one({"emp_code": code, "name": f"Name {code}", "email": "a@b.co", "department": dept,
                                "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                                "created_at": datetime(2026, 1, 1, tzinfo=UTC)})

    def log(code, d, status="PRESENT", late=0, ot=0, wh=None, half=False, with_times=True):
        doc = {"emp_code": code, "date": d, "status": status, "punch_in": None, "punch_out": None, "work_hours": wh,
               "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
        if status in m.PRESENT_STATUSES:
            doc["punch_in"] = datetime.fromisoformat(d + "T04:00:00+00:00")
        m.attendance.insert_one(doc)

    e("EMP0001", "Engineering", "2026-01-05")
    e("EMP0002", "Engineering", "2026-01-05")
    e("EMP0003", "Engineering", "2026-07-15")   # mid-month joiner
    e("EMP0004", "Sales", "2026-02-01")
    e("EMP0005", "HR", "2026-08-01")            # joins after July: HR must be omitted
    e("EMP0007", "Ops", "2026-03-01")           # no logs at all
    log("EMP0001", "2026-07-06", wh=9.0)
    log("EMP0001", "2026-07-07", "WFH", wh=3.5, half=True)
    log("EMP0001", "2026-07-08", "LEAVE")
    log("EMP0001", "2026-07-09", late=20, wh=8.0)
    log("EMP0001", "2026-07-11", late=15, ot=60, wh=6.0)   # Saturday
    log("EMP0002", "2026-07-06", late=20, wh=8.5)
    log("EMP0002", "2026-07-07", "ON_DUTY", late=20)       # open record, no work_hours
    log("EMP0002", "2026-07-08", "ABSENT")
    log("EMP0003", "2026-07-20", late=40, wh=8.0)
    log("EMP0004", "2026-07-10", late=35, wh=8.0)
    log("EMP9999", "2026-07-10", late=100, wh=8.0)         # orphan: employee does not exist


def test_employee_monthly(api):
    seed_analytics()
    j = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert j == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.5, "leave_days": 1,
                 "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 60, "attendance_pct": 10.87}
    j = api.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 13 and j["present_days"] == 1.0 and j["attendance_pct"] == 7.69
    j = api.get("/analytics/employees/EMP0005/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 0 and j["attendance_pct"] is None and j["present_days"] == 0
    j = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2027-01"}).json()
    assert j["present_days"] == 0 and j["late_count"] == 0
    assert api.get("/analytics/employees/EMP4040/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary(api):
    seed_analytics()
    j = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert j["month"] == "2026-07"
    by = {i["department"]: i for i in j["items"]}
    assert [i["department"] for i in j["items"]] == ["Engineering", "Ops", "Sales"]  # HR omitted, sorted
    assert by["Engineering"] == {"department": "Engineering", "headcount": 3, "present_days": 5.5, "avg_work_hours": 7.17,
                                 "late_count": 5, "total_late_minutes": 115, "leave_count": 1, "on_duty_count": 1}
    assert by["Ops"] == {"department": "Ops", "headcount": 1, "present_days": 0.0, "avg_work_hours": None,
                         "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    assert by["Sales"]["headcount"] == 1 and by["Sales"]["avg_work_hours"] == 8.0
    one = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert len(one["items"]) == 1
    aug = api.get("/analytics/departments/summary", params={"month": "2026-08"}).json()
    assert "HR" in [i["department"] for i in aug["items"]]
    assert api.get("/analytics/departments/summary", params={"month": "2025-01"}).json()["items"] == []


def test_late_leaderboard_ties_and_limit_after_rank(api):
    seed_analytics()
    m.attendance.insert_one({"emp_code": "EMP0002", "date": "2026-07-21", "status": "PRESENT", "late_minutes": 0})  # no late
    lb = lambda **p: api.get("/analytics/leaderboard/late", params={"month": "2026-07", **p}).json()["items"]
    # totals: EMP0002=40, EMP0003=40, EMP0001=35, EMP0004=35  -> ranks 1,1,3,3
    full = lb(limit=10)
    assert [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in full] == [
        (1, "EMP0002", 40), (1, "EMP0003", 40), (3, "EMP0001", 35), (3, "EMP0004", 35)]
    assert [i["emp_code"] for i in lb(limit=1)] == ["EMP0002", "EMP0003"]   # tie at the cutoff is kept
    assert [i["emp_code"] for i in lb(limit=2)] == ["EMP0002", "EMP0003"]   # rank 3 > limit
    assert len(lb(limit=3)) == 4                                              # more rows than limit
    assert all(i["emp_code"] != "EMP9999" for i in full)                      # orphan ignored
    assert full[0]["name"] == "Name EMP0002" and full[0]["late_count"] == 2  # two late days: 20 + 20
    eng = lb(department="Engineering")
    assert [(i["rank"], i["emp_code"]) for i in eng] == [(1, "EMP0002"), (1, "EMP0003"), (3, "EMP0001")]
    assert [(i["rank"], i["emp_code"]) for i in lb(department="Sales")] == [(1, "EMP0004")]
    assert lb(department="Ops") == []


def test_department_trend(api):
    seed_analytics()
    r = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-12"})
    assert r.status_code == 200
    rows = r.json()["items"]
    assert [x["date"] for x in rows] == [f"2026-07-{d:02d}" for d in range(1, 13)]
    assert [x["is_working_day"] for x in rows] == [True, True, True, False, False, True, True, True, True, True, False, False]
    assert all(x["headcount"] == 2 for x in rows)
    assert [x["attendance_rate"] for x in rows] == [0.0, 0.0, 0.0, None, None, 1.0, 0.75, 0.0, 0.5, 0.0, None, None]
    assert [x["moving_avg_7d"] for x in rows] == [0.0, 0.0, 0.0, 0.0, 0.0, 0.25, 0.35, 0.35, 0.45, 0.45, 0.45, 0.45]
    assert [x["present_count"] for x in rows] == [0, 0, 0, 0, 0, 2.0, 1.5, 0, 1.0, 0, 1.0, 0]
    assert [x["late_count"] for x in rows] == [0, 0, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0]
    # a window that starts on a weekend: first rows have null average
    wk = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-04", "to": "2026-07-05"}).json()["items"]
    assert [x["moving_avg_7d"] for x in wk] == [None, None]
    one = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"]
    assert len(one) == 1 and one[0]["attendance_rate"] == 0.0 and one[0]["headcount"] == 1
    hr = api.get("/analytics/departments/HR/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"]
    assert hr[0]["headcount"] == 0 and hr[0]["attendance_rate"] is None  # not joined yet
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404


def test_empty_collections(api):
    assert api.get("/attendance").json()["total"] == 0
    assert api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"] == []
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"] == []


# ---------------------------------------------------------------- explain on ~100k records
def test_explain_uses_indexes_on_100k_records(api):
    m.employees.delete_many({})
    m.attendance.delete_many({})
    depts = ["Engineering", "Sales", "Ops", "HR"]
    m.employees.insert_many([{"emp_code": f"EMP{i:04d}", "name": f"n{i}", "email": "a@b.co", "department": depts[i % 4],
                              "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
                              "created_at": datetime(2026, 1, 1, tzinfo=UTC)} for i in range(1, 1001)])
    docs, day = [], datetime(2026, 4, 1)
    for n in range(100):
        d = (day + timedelta(days=n)).strftime("%Y-%m-%d")
        for i in range(1, 1001):
            docs.append({"emp_code": f"EMP{i:04d}", "date": d, "status": "PRESENT", "punch_in": None, "punch_out": None,
                         "work_hours": 8.0, "late_minutes": i % 7 * 5, "overtime_minutes": 0, "half_day": False, "history": []})
    m.attendance.insert_many(docs, ordered=False)
    m.ensure_indexes()
    cases = [
        ("attendance_list", {}), ("attendance_list", {"emp_code": "EMP0042", "date_from": "2026-04-01", "date_to": "2026-05-01"}),
        ("attendance_list", {"status": "PRESENT", "date_from": "2026-05-01", "page": 3, "page_size": 50}),
        ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-05"}),
        ("department_summary", {"month": "2026-05"}), ("department_summary", {"month": "2026-05", "department": "Sales"}),
        ("late_leaderboard", {"month": "2026-05", "limit": 5}), ("late_leaderboard", {"month": "2026-05", "department": "Ops"}),
        ("department_trend", {"department": "Engineering", "from": "2026-04-01", "to": "2026-05-15"}),
    ]
    for ep, params in cases:
        r = api.get(f"/admin/explain/{ep}", params=params)
        assert r.status_code == 200, (ep, r.text)
        body = r.json()
        assert set(body) == {"endpoint", "collection", "explain"} and body["endpoint"] == ep
        text = json.dumps(body["explain"])
        assert "IXSCAN" in text, (ep, params)
        assert "COLLSCAN" not in text, (ep, params)