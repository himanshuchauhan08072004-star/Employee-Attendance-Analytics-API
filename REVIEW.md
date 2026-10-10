# REVIEW.md

Starter `app/main.py` reviewed as a pull request. Every item below was confirmed in the code; fixes are in the current `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `/health` | Always returns ok, never talks to MongoDB. | Stop MongoDB: `/health` still 200 (must be 503). | `ping` the server; 503 on `PyMongoError`. |
| 2 | `MongoClient(...)` | Not `tz_aware`: PyMongo returns naive datetimes, mixed with aware IST values later (TypeError / wrong offsets). No server-selection timeout. | `compute_overtime` subtracts naive from aware: `TypeError`. | `tz_aware=True`, `serverSelectionTimeoutMS`; all instants handled as UTC-aware. |
| 3 | Startup | No indexes at all (also no unique constraints). | `explain` shows COLLSCAN; duplicates possible. | `ensure_indexes()` in lifespan (and retried by `/health`); see DECISIONS.md. |
| 4 | `compute_late_minutes` | Uses the punch-in's own (UTC) date for shift start, not the IST attendance date; ignores overnight shifts; `int()` truncates toward zero; compares whole minutes `> 10`, so 09:40:30 is wrongly "not late" (R2: strictly more than 10:00); no second truncation. | Shift 09:30, punch 09:40:01 returns 0 (should be 10). | `compute_late_minutes(punch_in, ist_date, shift_start)` works in seconds against IST shift start; late if `delta > 600`, then `delta // 60`. |
| 5 | `compute_work_hours` | Python `round()` is banker's rounding on floats; sub-second parts leak in. | 8.125 h gives 8.12 (R4 needs 8.13). | `Decimal` seconds/3600, `ROUND_HALF_UP`; instants truncated to seconds at the boundary. |
| 6 | `compute_overtime` | No 30-minute minimum (R3); end time built from `date_str` ignoring overnight shifts; seconds not zeroed; naive/aware mix. | Punch-out 18:35 returns 5 instead of 0; overnight shift returns hugely negative then 0. | `shift_end_at()` adds one day for overnight shifts; floor minutes only if `>= 1800` s. |
| 7 | `create_employee` | Check-then-insert race and no unique index: parallel creates produce duplicates; no validation (emp_code format, email, `shift_start != shift_end`, real dates); `created_at` is naive local time and returned as datetime, not epoch ms. | 10 parallel POSTs: several 201. `emp_code="x"` accepted. | Unique index + `DuplicateKeyError` -> 409; Pydantic patterns/validators -> 422; UTC `created_at` returned as ms. |
| 8 | `list_employees` | `skip = page * page_size` skips the whole first page; `total` counts the whole collection, ignoring the filter; no sort; `page`/`page_size` unvalidated (page 0, size 10^6). | `GET /employees?page=1` returns page 2 rows; `total` ignores `department`. | `(page-1) * page_size`; `count_documents(filter)`; sort by `emp_code`; `ge=1`, `le=100`. |
| 9 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError` -> 500 (must be 404). | Punch in with unknown `emp_code`. | `get_employee_or_404`. |
| 10 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local timezone and keeps microseconds; date is the server-local date, not IST; no overnight rule; `if body.punched_at` treats 0 as missing; any int accepted (seconds, negative). | Punch at 00:30 IST on the 7th is stored on the 6th (UTC) or wrong on a UTC server. | `EpochMs` strict int in range (422 otherwise), `ms_to_dt` truncates to seconds in UTC, `attendance_date()` implements R1. |
| 11 | `punch_in` | Check-then-insert race: two parallel punch-ins both pass `find_one` and both insert. | 10 parallel requests -> several 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 12 | `punch_in` | Accepts any status string (`ABSENT`, junk); response contains an extra `id` (ObjectId string) and raw datetimes instead of epoch ms. | `status="LEAVE"` returns 201. | `PresenceStatus` literal (422); `serialize_record` returns contract fields only, instants in ms. |
| 13 | `list_attendance` | Loads the whole collection into Python, sorts in Python by `date` only (no `emp_code` tiebreak), paginates in Python. Slow at 100k, unstable order. | 100k docs: seconds + memory; ties come back in arbitrary order. | Mongo `find` with `sort`, `skip`, `limit` served by the indexes; `count_documents(filter)` for `total`. |
| 14 | `list_attendance` | No validation: `date_from > date_to`, impossible dates, unknown `status`, `page=0`, huge `page_size`; returns `_id` as `id` and raw datetimes; `history` / `half_day` missing on legacy docs leak through as missing keys. | `date_from=2026-07-10&date_to=2026-07-01` returns 200. | 422 checks, `serialize_record` defaults (`history` `[]`, `half_day` false, nested history times to ms). |
| 15 | Whole file | Models accept anything: `EmployeeIn` has plain `str` for dates/times and no patterns. | `joined_on="tomorrow"`. | Contract patterns and validators on all request models. |

## Checked and found fine
- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env` (required by the contract).
- Reading `MONGO_URI` / `MONGO_DB` from the environment is correct; I only kept the localhost default as a development fallback.
- `doc.pop("_id")` after `insert_one` correctly keeps `_id` out of the employee response (but see #7 for the rest of that handler).
- `IST = timezone(timedelta(hours=5, minutes=30))` is the right offset (India has no DST).
- The collection names match `DATA_MODEL.md`.
