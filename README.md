# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run

Use Python 3.11 or newer and MongoDB 6.0 or newer. Set `MONGO_URI` and `MONGO_DB` in the environment before starting the application. A local `.env` file may be used during development but must not be committed.

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

`GET /health` returns 200 only when MongoDB responds. Indexes are created idempotently at startup. The grader seeds the `employees` and `attendance_logs` collections directly; no sample data is required in this repository.

## Notes

- All API instants are epoch milliseconds; stored instants are UTC BSON datetimes.
- Analytics use MongoDB aggregation pipelines.
- Read `REVIEW.md` and `DECISIONS.md` for the code review and design reasoning.
- Before submission, run the app against MongoDB 6.0+ and test all endpoints, concurrent requests, and explain plans on a realistic dataset.
