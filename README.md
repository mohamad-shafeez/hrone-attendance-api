# Employee Attendance & Analytics API

A FastAPI and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run

Requirements: Python 3.11+ and MongoDB 6.0+.

Create a local `.env` file using `.env.example` and configure `MONGO_URI` and `MONGO_DB`. Never commit your real `.env` file.

Install dependencies and start the application:

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`GET /health` checks MongoDB connectivity. Required indexes are created idempotently at startup. The grader can seed the `employees` and `attendance_logs` collections directly; sample data is not required.

## Implementation Notes

* API timestamps are represented as epoch milliseconds; stored instants use UTC BSON datetimes.
* Analytics are implemented using MongoDB aggregation pipelines.
* `REVIEW.md` documents the code review, and `DECISIONS.md` explains key design choices.

## Verification

* Tested MongoDB connectivity, employee validation, duplicate prevention, punch-in/out calculations, attendance corrections, and audit history.
* Tested monthly analytics, department summaries, headcount, late-leaderboard ranking, department trends, and all five explain endpoint variants.
* Temporary test records were removed after testing.
* Concurrent-request behavior and large-scale performance have not been benchmarked.
