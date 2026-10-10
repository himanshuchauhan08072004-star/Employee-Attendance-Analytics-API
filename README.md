# Employee Attendance & Analytics API

FastAPI + MongoDB 6.0+. All application code is in `app/main.py`.

## Run (PowerShell)
```powershell
cd <repo folder>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then set MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```
Check: `http://localhost:8000/health` -> `{"status":"ok"}`. Docs: `http://localhost:8000/docs`.
Indexes are created automatically at startup. Real environment variables override `.env`.

MongoDB: local (`docker run -p 27017:27017 mongo:7`) or Atlas free tier. Needs 6.0+ (`$setWindowFields`, `$dateAdd`).

## Tests
```powershell
pip install -r tests/requirements-test.txt
pytest tests/test_unit.py -q          # no database needed
$env:MONGO_URI="mongodb://localhost:27017"; pytest tests -q   # + integration tests
```
Integration tests use database `attendance_test` (override: `TEST_MONGO_DB`) and **wipe it**. They are skipped if MongoDB is unreachable. They include concurrency tests and explain checks on 100,000 attendance records.

## Assumptions (contract was ambiguous)
- `page_size` above 100 returns 422 (the contract sets `maximum: 100`), it is not silently capped.
- `moving_avg_7d` averages the unrounded daily rates; rounding to 4 decimals happens once at the end.
- Trend range limit: at most 92 calendar days, both ends inclusive.
- Punch-out on an already closed record returns 409 before time-range checks.

## Known limitations
- Concurrent corrections: the loser gets 409 and must retry (no automatic retry).
- Single file by requirement; no authentication (none in the contract).
