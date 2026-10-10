# Employee Attendance & Analytics API

REST API for employee attendance (punch-in / punch-out), audited manual corrections, and analytics computed with MongoDB aggregation pipelines.

**Stack:** Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+. All application code is in `app/main.py`.

## Features
- Punch-in / punch-out with business rules: IST attendance dates, overnight shifts, late-minute grace period, overtime threshold, half-up rounding.
- Manual corrections (`PATCH`) with a full audit history: who, when, why, and the old and new values of every changed field.
- Analytics endpoints (employee monthly, department summary, late leaderboard, department trend) built as aggregation pipelines.
- Race-safe by design: unique indexes and atomic conditional updates, no check-then-write.
- `GET /admin/explain/{endpoint}` returns MongoDB `executionStats` for the real query behind each endpoint.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and MongoDB check |
| POST / GET | `/employees` | Create / list employees |
| POST | `/attendance/punch-in` | Start attendance for the day |
| POST | `/attendance/punch-out` | Close the open record |
| GET | `/attendance` | List records (filters, pagination) |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record, appends audit history |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Monthly summary per department |
| GET | `/analytics/leaderboard/late` | Late ranking with ties |
| GET | `/analytics/departments/{department}/trend` | Daily rate and 7-day moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output |

Interactive docs: `http://localhost:8000/docs`

## Setup (Windows PowerShell)
```powershell
git clone https://github.com/himanshuchauhan08072004-star/Employee-Attendance-Analytics-API.git
cd Employee-Attendance-Analytics-API
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --port 8000
```
Edit `.env` if needed:

| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

Real environment variables override `.env`. Indexes are created automatically at startup.

MongoDB 6.0+ is required: local (`docker run -d -p 27017:27017 mongo:7`) or a free Atlas cluster.

## Tests
```powershell
pip install -r tests/requirements-test.txt
pytest tests/test_unit.py -q                                  # no database needed
$env:MONGO_URI="mongodb://localhost:27017"; pytest tests -q   # plus integration tests
```
Integration tests run against a real MongoDB, use the database `attendance_test` (override with `TEST_MONGO_DB`) and **wipe it**. They cover concurrent punch-in / punch-out / corrections, analytics edge cases, and explain plans on 100,000 records (index used, no collection scan). If MongoDB is unreachable they are skipped.

## Design notes
See `DECISIONS.md` (design choices) and `REVIEW.md` (defects found in the starter code and how they were fixed).

Assumptions where the contract was ambiguous:
- `page_size` above 100 returns 422 instead of being capped.
- `moving_avg_7d` averages the unrounded daily rates and rounds once at the end.
- The trend range allows at most 92 days, both ends inclusive.
- Punch-out on an already closed record returns 409 before time-range checks.

## Known limitations
- A correction that loses a concurrent race returns 409 and must be retried by the client; there is no automatic retry.
- No authentication, because the contract does not require it.
## Himanshu Chauhan