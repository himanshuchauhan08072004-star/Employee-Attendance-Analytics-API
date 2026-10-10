"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded; real environment variables win)

All code lives in this one file, as the assignment requires. Sections:
  1. config / database      4. request models        7. attendance endpoints
  2. time + rule helpers    5. serializers           8. analytics (aggregation pipelines)
  3. indexes                6. employee endpoints    9. explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config / database
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables win over .env
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
attendance = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE_STATUSES = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_SECONDS = 30 * 60   # R3
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / regularize upper bound
HALF_DAY_LIMIT = Decimal("4.50")  # R5

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
# strict=True: floats, strings and bools are rejected (422), not coerced.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# 2. Time + business-rule helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def half_up(value: Any, places: int) -> float:
    """Round half-up (R4/R8). Goes through Decimal(str(x)) so floats do not drift."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return to_minutes(shift_end) <= to_minutes(shift_start)


def ist_at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; overnight early-morning punches belong to the previous day."""
    ist = ts.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_of_day < to_minutes(shift_end) * 60:
            d -= timedelta(days=1)
    return d


def shift_end_at(d: date, shift_start: str, shift_end: str) -> datetime:
    end = ist_at(d, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, d: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes counted from shift start."""
    delta = int((punch_in - ist_at(d, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, d: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if at least 30."""
    delta = int((punch_out - shift_end_at(d, shift_start, shift_end)).total_seconds())
    return delta // 60 if delta >= MIN_OVERTIME_SECONDS else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: the ROUNDED work hours < 4.50."""
    return Decimal(str(work_hours)) < HALF_DAY_LIMIT


NO_PUNCH_DERIVED = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def derive_fields(emp: dict, d: date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All four stored derived values for a presence record (R2-R5)."""
    late = compute_late_minutes(punch_in, d, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, d, emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(wh),
    }


def unprocessable(loc: str, msg: str) -> HTTPException:
    return HTTPException(422, detail=[{"loc": ["query", loc], "msg": msg, "type": "value_error"}])


def check_date(value: str, loc: str) -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        raise unprocessable(loc, "must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable(loc, "not a real calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, nxt - timedelta(days=1)


def check_month(value: str, loc: str = "month") -> tuple[date, date]:
    if not re.fullmatch(MONTH_PATTERN, value):
        raise unprocessable(loc, "must be YYYY-MM")
    return month_bounds(value)


def count_weekdays(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def check_trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    f, t = check_date(d_from, "from"), check_date(d_to, "to")
    if t < f:
        raise unprocessable("to", "'to' must not be before 'from'")
    if (t - f).days + 1 > 92:
        raise unprocessable("to", "range must not exceed 92 days")
    return f, t


# --------------------------------------------------------------------------- #
# 3. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # unique business key; also the race-safe guard for POST /employees
        ([("emp_code", 1)], {"unique": True, "name": "uniq_emp_code"}),
        # GET /employees?department=..  (filter + sort by emp_code), trend/summary department match
        ([("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
        # headcount (R9): joined_on <= end of period when no department filter
        ([("joined_on", 1)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        # one record per (emp_code, date): punch-in race guard, PATCH lookup, monthly, $lookup joins
        ([("emp_code", 1), ("date", 1)], {"unique": True, "name": "uniq_emp_date"}),
        # GET /attendance default sort (date desc, emp_code asc), date-range scans for leaderboard
        ([("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
        # GET /attendance?status=..  with the same sort
        ([("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
        # punch-out: latest record with punch_in <= punched_at
        ([("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in"}),
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)  # idempotent
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # do not crash; /health retries and reports 503 until Mongo is up
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not re.fullmatch(DATE_PATTERN, v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic ignores unknown keys)


# --------------------------------------------------------------------------- #
# 5. Serializers (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def to_ms_deep(value: Any) -> Any:
    """Convert every datetime, also inside nested history objects, to epoch ms."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_ms_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_ms_deep(v) for v in value]
    return value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms_deep(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    wh = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms_deep(doc.get("punch_in")),
        "punch_out": to_ms_deep(doc.get("punch_out")),
        "work_hours": None if wh is None else float(wh),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [
            {"at": to_ms_deep(h["at"]), "by": h["by"], "reason": h["reason"], "changes": to_ms_deep(h.get("changes", {}))}
            for h in (doc.get("history") or [])
        ],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. System + employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError as exc:
        log.warning("health check failed: %s", exc)
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 7. Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, d, ts, None),
        "history": [],
    }
    try:
        attendance.insert_one(doc)  # unique (emp_code, date) index is the race guard
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": at}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if at <= pin:
        raise unprocessable("punched_at", "punched_at must be after punch_in")
    if (at - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise unprocessable("punched_at", "punch-out is more than 24 hours after punch-in")
    d = date.fromisoformat(rec["date"])
    derived = derive_fields(emp, d, pin, at)
    # Atomic compare-and-set: only one concurrent request can flip punch_out from null.
    updated = attendance.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": {
            "punch_out": at,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Single place that builds the list query, shared by GET /attendance and the explain endpoint."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    # with a single emp_code the secondary sort key is constant, so sorting by date alone is equivalent
    sort = {"date": -1} if emp_code is not None else {"date": -1, "emp_code": 1}
    return {"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}


def validate_attendance_filters(date_from, date_to):
    f = check_date(date_from, "date_from") if date_from is not None else None
    t = check_date(date_to, "date_to") if date_to is not None else None
    if f and t and f > t:
        raise unprocessable("date_from", "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_attendance_filters(date_from, date_to)
    spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = attendance.count_documents(spec["filter"])
    cursor = attendance.find(spec["filter"]).sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):  # noqa: A002 (path name fixed by contract)
    check_date(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = attendance.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    rec_date = datetime.strptime(date, "%Y-%m-%d").date()

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
        derived = dict(NO_PUNCH_DERIVED)
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
        derived = derive_fields(emp, rec_date, new_in, new_out)

    old = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0), "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}

    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record is still exactly what we read
    # (same status/punches and same number of history entries). Loser gets 409, no lost history.
    expected_history = len(rec.get("history") or [])
    updated = attendance.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics: aggregation pipelines
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. They read the STORED derived values (never recompute).
IS_PRESENT = {"$in": ["$status", list(PRESENT_STATUSES)]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
# presence weight in HALVES (integers keep sums exact): half day = 1, full day = 2
WEIGHT_HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}
HALVES_ANY_DAY = {"$cond": [IS_PRESENT, WEIGHT_HALVES, 0]}
HALVES_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, WEIGHT_HALVES, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MIN, 0]}
LATE_COUNT = {"$cond": [IS_LATE, 1, 0]}
LATE_TOTAL = {"$cond": [IS_LATE, LATE_MIN, 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
# avg_work_hours input: present-status records that have a work_hours value; summed as integer cents (exact)
HAS_HOURS = {"$and": [IS_PRESENT, {"$gt": [{"$ifNull": ["$work_hours", None]}, None]}]}
HOURS_CENTS = {"$cond": [HAS_HOURS, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}
HOURS_N = {"$cond": [HAS_HOURS, 1, 0]}


def status_count(status: str) -> dict:
    return {"$cond": [{"$eq": ["$status", status]}, 1, 0]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "halves": {"$sum": HALVES_WEEKDAY},  # Mon-Fri only (R7)
            "leave_days": {"$sum": status_count("LEAVE")},
            "late_count": {"$sum": LATE_COUNT},
            "total_late_minutes": {"$sum": LATE_TOTAL},  # weekends included
            "total_overtime_minutes": {"$sum": OVERTIME},  # weekends included
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))  # mid-month joiners count from joined_on
    working_days = count_weekdays(start, last) if start <= last else 0
    rows = list(attendance.aggregate(monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    halves = agg.get("halves", 0)
    present = Decimal(halves) / 2
    pct = None if working_days == 0 else half_up(present * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def dept_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9 headcount
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # Start from employees so people with zero logs still exist as rows.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "halves": {"$sum": HALVES_WEEKDAY},
                    "late_count": {"$sum": LATE_COUNT},
                    "late_total": {"$sum": LATE_TOTAL},
                    "leave": {"$sum": status_count("LEAVE")},
                    "on_duty": {"$sum": status_count("ON_DUTY")},
                    "wh_cents": {"$sum": HOURS_CENTS},
                    "wh_n": {"$sum": HOURS_N},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "halves": {"$sum": {"$ifNull": ["$s.halves", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_total": {"$sum": {"$ifNull": ["$s.late_total", 0]}},
            "leave": {"$sum": {"$ifNull": ["$s.leave", 0]}},
            "on_duty": {"$sum": {"$ifNull": ["$s.on_duty", 0]}},
            "wh_cents": {"$sum": {"$ifNull": ["$s.wh_cents", 0]}},
            "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in employees.aggregate(dept_summary_pipeline(month, department)):
        n = r["wh_n"]
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(Decimal(r["halves"]) / 2),
            # average over records: exact integer cents / count, rounded half-up once
            "avg_work_hours": half_up(Decimal(r["wh_cents"]) / Decimal(100 * n), 2) if n else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["late_total"],
            "leave_count": r["leave"],
            "on_duty_count": r["on_duty"],
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of employees that no longer exist are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # filter BEFORE ranking
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": "$total", "late_count": "$cnt",
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": list(attendance.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    return [
        {"$match": {"department": department, "joined_on": {"$lte": t}}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1, "halves": HALVES_ANY_DAY, "late": LATE_COUNT}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "joined_on": 1, "logs": 1}},
        # one document: all join dates + per-day totals for the department
        {"$facet": {
            "joins": [{"$group": {"_id": None, "joined": {"$push": "$joined_on"}}}],
            "daily": [
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date", "halves": {"$sum": "$logs.halves"}, "late": {"$sum": "$logs.late"}}},
            ],
        }},
        # gap filling: generate exactly one row per calendar day in the range
        {"$project": {"joined": {"$ifNull": [{"$arrayElemAt": ["$joins.joined", 0]}, []]}, "daily": 1,
                      "i": {"$range": [0, n_days]}}},
        {"$unwind": "$i"},
        {"$addFields": {"d": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$i"}}}},
        {"$addFields": {
            "ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "wd": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$ds"]}}}},
            "dd": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$ds"]}}}, 0]},
        }},
        {"$addFields": {"halves": {"$ifNull": ["$dd.halves", 0]}, "late": {"$ifNull": ["$dd.late", 0]}}},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$wd", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$halves", {"$multiply": [2, "$headcount"]}]},
            None,
        ]}}},
        # moving average over current row + up to 6 previous rows INSIDE the range; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": "$ds", "is_working_day": "$wd", "headcount": 1, "halves": 1, "late": 1,
                      "rate": 1, "ma": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = check_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees.aggregate(trend_pipeline(department, d_from, d_to)):
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(Decimal(r["halves"]) / 2),
            "late_count": r["late"],
            "attendance_rate": None if r["rate"] is None else half_up(r["rate"], 4),
            "moving_avg_7d": None if r.get("ma") is None else half_up(r["ma"], 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 9. Explain endpoint
# --------------------------------------------------------------------------- #
def run_explain(collection: str, kind: str, spec: Any) -> dict:
    if kind == "find":
        command = {"find": collection, "filter": spec["filter"], "sort": spec["sort"],
                   "skip": spec["skip"], "limit": spec["limit"]}
    else:
        command = {"aggregate": collection, "pipeline": spec, "cursor": {}}
    raw = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(raw))  # BSON -> plain JSON


def require(value, name: str):
    if value is None:
        raise unprocessable(name, "required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch uses the SAME builder function as the real endpoint.
    if endpoint == "attendance_list":
        validate_attendance_filters(date_from, date_to)
        coll, kind = "attendance_logs", "find"
        spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        require(emp_code, "emp_code")
        check_month(require(month, "month"))
        coll, kind, spec = "attendance_logs", "aggregate", monthly_pipeline(emp_code, month)
    elif endpoint == "department_summary":
        check_month(require(month, "month"))
        coll, kind, spec = "employees", "aggregate", dept_summary_pipeline(month, department)
    elif endpoint == "late_leaderboard":
        check_month(require(month, "month"))
        coll, kind, spec = "attendance_logs", "aggregate", leaderboard_pipeline(month, limit, department)
    else:  # department_trend
        require(department, "department")
        d_from, d_to = check_trend_range(require(from_, "from"), require(to, "to"))
        coll, kind, spec = "employees", "aggregate", trend_pipeline(department, d_from, d_to)
    return {"endpoint": endpoint, "collection": coll, "explain": run_explain(coll, kind, spec)}
