# REVIEW.md

The starter has been reviewed against `openapi.yaml` and `DATA_MODEL.md`. The fixes below are reflected in `app/main.py`. The helper checks run so far are not a substitute for running the app against MongoDB; database behavior and aggregation plans still need a real MongoDB test.

| # | Where | What is wrong | How to notice it | How it is fixed |
|---|---|---|---|---|
| 1 | `health()` | It always reports healthy without contacting MongoDB. | Stop MongoDB and call `GET /health`; the starter still returns 200. | Run MongoDB `ping`; return 503 when it fails. |
| 2 | `compute_late_minutes()` | It compares whole minutes before applying the strict 10-minute rule, so 09:40:01 can be treated as not late. It also relies on the incoming datetime's timezone. | With a 09:30 shift, test 09:40:00 and 09:40:01 IST. | Convert to IST for the shift date, truncate instants to seconds, apply the strict grace boundary, then floor whole minutes since shift start. |
| 3 | `compute_work_hours()` | Python's built-in `round()` uses ties-to-even, not the required half-up rule. | A duration of exactly 4.495 hours can round differently. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime()` | It counts any positive overtime and always assumes the shift ends on the attendance date. | Test a 29-minute extension and a 22:00–06:00 shift. | Apply the 30-minute threshold and move the shift end to the next day for overnight shifts. |
| 5 | MongoDB initialization | The starter creates no required indexes, including uniqueness guarantees. | Inspect `getIndexes()` on a clean database or send simultaneous duplicate requests. | Create idempotent indexes at startup, including unique employee codes and unique `(emp_code, date)` attendance records. |
| 6 | `create_employee()` | Checking for a duplicate and then inserting is a race; simultaneous requests can both pass the check. | Send two parallel requests with the same `emp_code`. | Rely on a unique index and translate `DuplicateKeyError` to 409. |
| 7 | `create_employee()` | `created_at` uses a naive server-local datetime and the response returns a datetime instead of epoch milliseconds. | Run on a machine with a different timezone and inspect the response type. | Store a UTC datetime truncated to seconds and serialize instants as integer epoch milliseconds. |
| 8 | `list_employees()` | `skip = page * page_size` skips the first page. | Request page 1 with page size 2. | Use `(page - 1) * page_size`. |
| 9 | `list_employees()` | `total` counts all employees instead of the filtered set, and results are not sorted. | Filter by department and compare `total`; repeat the request. | Count with the same filter and sort by `emp_code` ascending. |
| 10 | `list_employees()` and `list_attendance()` | Page and page-size boundaries are not validated. | Try page 0, page size 0, and page size 101. | Enforce page >= 1 and page size 1–100. |
| 11 | `punch_in()` | An unknown employee is dereferenced and can produce a 500. | Punch in using an unregistered employee code. | Check the employee first and return 404. |
| 12 | `punch_in()` | Timestamps use the server's local timezone, aren't truncated to whole seconds, and the date may be wrong for IST/overnight shifts. | Run with a non-IST server timezone; test an overnight punch before shift end. | Parse epoch milliseconds as UTC, truncate to seconds, calculate the IST attendance date, and handle overnight shifts. |
| 13 | `punch_in()` | The status is any string, and duplicate detection is check-then-insert. | Send `status: "ABSENT"`; also send two parallel punch-ins. | Validate only `PRESENT`, `WFH`, and `ON_DUTY`; enforce uniqueness in MongoDB and return 409 for a duplicate. |
| 14 | `punch_in()` and `list_attendance()` | Responses expose an `id` derived from MongoDB `_id`, and return raw datetime values rather than the contract's epoch-millisecond values. | Inspect response keys and timestamp JSON types. | Do not expose `_id`; serialize the contract fields explicitly. |
| 15 | `list_attendance()` | It reads the entire matching result set into Python and sorts/pages in memory. Its sort does not provide the required employee-code tie-breaker. | Try a large dataset and records sharing a date. | Count with the filter, then use MongoDB sort/skip/limit and indexes. Sort by date descending, then employee code ascending. |
| 16 | `list_attendance()` | It doesn't reject reversed date ranges or invalid statuses, and it doesn't normalize legacy missing `history` / `half_day` fields. | Try `date_from > date_to`, an unknown status, or a seeded record missing those fields. | Validate dates and status; serialize missing history as `[]` and missing `half_day` as `false`. |
| 17 | Request models | The starter has little schema validation for employee fields and epoch timestamps. | Try malformed employee codes, dates, shift times, seconds-looking timestamps, strings, and floats. | Add strict request models and reject invalid timestamps and field values with 422. |
| 18 | Missing endpoints | Punch-out, regularization, all four analytics endpoints, and explain are not implemented. | Call any endpoint marked “You” in the contract. | Implement each endpoint in `app/main.py`, with analytics calculations in MongoDB aggregation pipelines. |

## Examined and considered acceptable

- `emp_code` is the business identifier; MongoDB `_id` must remain internal.
- The stored derived attendance values are the source of truth for analytics; analytics should not recalculate them from punch timestamps.
- Missing `late_minutes`, `overtime_minutes`, `half_day`, and `history` in seeded legacy documents are defined defaults, not necessarily data corruption.
- The contract allows a synchronous PyMongo client; an async framework is not required for this assignment.
