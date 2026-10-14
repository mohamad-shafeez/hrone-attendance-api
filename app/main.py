"""Employee Attendance & Analytics API (HROne assignment).

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables.
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from dotenv import load_dotenv

load_dotenv(override=False)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

# A short timeout lets /health report 503 rather than hanging on a dead DB.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2500,
    connectTimeoutMS=2500,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def parse_date(value: str, label: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{label} must be a valid calendar date")


def validate_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def strict_epoch(value: int) -> datetime:
    # Pydantic StrictInt rejects floats, strings, and booleans before this point.
    if not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid epoch timestamp")


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # tz_aware=True is configured, but tolerate naive datetimes from test doubles.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    value = as_utc(value)
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded)


def parse_shift(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "shift times must use HH:MM")
    h, m = map(int, value.split(":"))
    return h, m


def attendance_date_for(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = as_utc(punch_in).astimezone(IST)
    start = parse_shift(employee["shift_start"])
    end = parse_shift(employee["shift_end"])
    d = local.date()
    # A time before the end of an overnight shift belongs to yesterday's shift.
    if end <= start and (local.hour, local.minute) < end:
        d -= timedelta(days=1)
    return d


def shift_boundary(day: date, shift_time: str, overnight_next_day: bool = False) -> datetime:
    h, m = parse_shift(shift_time)
    d = day + timedelta(days=1) if overnight_next_day else day
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, employee: dict[str, Any]) -> int:
    punch_in = as_utc(punch_in).replace(microsecond=0)
    local = punch_in.astimezone(IST)
    start_hm = parse_shift(employee["shift_start"])
    end_hm = parse_shift(employee["shift_end"])
    shift_day = local.date()
    if end_hm <= start_hm and (local.hour, local.minute) < end_hm:
        shift_day -= timedelta(days=1)
    shift_start = shift_boundary(shift_day, employee["shift_start"])
    delta_seconds = (punch_in - shift_start).total_seconds()
    if delta_seconds <= 10 * 60:
        return 0
    return max(0, int(delta_seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, employee: dict[str, Any], attendance_day: date) -> int:
    start_hm = parse_shift(employee["shift_start"])
    end_hm = parse_shift(employee["shift_end"])
    overnight = end_hm <= start_hm
    shift_end = shift_boundary(attendance_day, employee["shift_end"], overnight_next_day=overnight)
    seconds = (as_utc(punch_out) - shift_end).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def validate_punch_pair(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> None:
    if punch_in is None:
        raise HTTPException(422, "presence statuses require punch_in")
    if punch_out is not None:
        seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
        if seconds <= 0 or seconds > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")


def employee_or_404(emp_code: str) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


def public_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc.get("created_at")),
    }


def public_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    out = {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")), "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)), "history": [],
    }
    for item in doc.get("history", []) or []:
        changes = {}
        for key, change in item.get("changes", {}).items():
            if key in {"punch_in", "punch_out"}:
                changes[key] = {"from": epoch_ms(change.get("from")), "to": epoch_ms(change.get("to"))}
            else:
                changes[key] = {"from": change.get("from"), "to": change.get("to")}
        out["history"].append({"at": epoch_ms(item.get("at")), "by": item.get("by"),
                               "reason": item.get("reason"), "changes": changes})
    return out


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_rate(numerator: float, denominator: int) -> Optional[float]:
    return None if denominator == 0 else half_up(Decimal(str(numerator)) / Decimal(denominator) * 100, 4)


def mongo_json(value: Any) -> Any:
    """Make explain output JSON-safe while retaining its raw structure."""
    if isinstance(value, dict):
        return {str(k): mongo_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mongo_json(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "binary") and hasattr(value, "generation_time"):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --------------------------------- models ------------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v: str) -> str:
        parse_date(v, "joined_on")
        return v


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: StrictInt = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            strict_epoch(v)
        return v

    @field_validator("status")
    @classmethod
    def valid_presence_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: StrictInt = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            strict_epoch(v)
        return v


class RegularizeIn(StrictModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: str = None
    punch_in: StrictInt = None
    punch_out: StrictInt = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            strict_epoch(v)
        return v


# ------------------------------- indexes ------------------------------------
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee_asc")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punchin")


@app.on_event("startup")
def startup() -> None:
    # Keep the HTTP process startable when the DB is temporarily down; /health then returns 503.
    try:
        ensure_indexes()
    except PyMongoError:
        pass


# -------------------------------- endpoints ----------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=422,
            detail="shift_start and shift_end must be different",
        )
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    validate_page(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = strict_epoch(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    local_date = attendance_date_for(ts, emp).isoformat()
    doc = {"emp_code": body.emp_code, "date": local_date, "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)
    start = parse_date(date_from, "date_from") if date_from else None
    end = parse_date(date_to, "date_to") if date_to else None
    if start and end and start > end:
        raise HTTPException(422, "date_from must not be after date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    query: dict[str, Any] = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    if status is not None: query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee_or_404(body.emp_code)
    ts = strict_epoch(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record already punched out")
    pin = as_utc(candidate["punch_in"])
    seconds = (ts - pin).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(pin, ts)
    day = parse_date(candidate["date"])
    overtime = compute_overtime(ts, emp, day)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime,
                   "half_day": work_hours < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record already punched out")
    return public_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    emp = employee_or_404(emp_code)
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    status = body.status if "status" in supplied else current["status"]
    if status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        pin = pout = None
    else:
        pin = strict_epoch(body.punch_in) if "punch_in" in supplied else as_utc(current.get("punch_in"))
        pout = strict_epoch(body.punch_out) if "punch_out" in supplied else as_utc(current.get("punch_out"))
        validate_punch_pair(pin, pout)
        if attendance_date_for(pin, emp).isoformat() != day.isoformat():
            raise HTTPException(422, "punch_in must belong to the attendance date")
    if status in {"ABSENT", "LEAVE"}:
        late = overtime = 0; hours = None; half_day = False
    else:
        late = compute_late_minutes(pin, emp)
        if pout is None:
            hours = None; overtime = 0; half_day = False
        else:
            hours = compute_work_hours(pin, pout)
            overtime = compute_overtime(pout, emp, day)
            half_day = hours < 4.50
    final = {"status": status, "punch_in": pin, "punch_out": pout, "work_hours": hours,
             "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}
    changes = {}
    for key, new in final.items():
        # DATA_MODEL.md defines missing legacy fields as their default values.
        defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False, "work_hours": None,
                    "punch_in": None, "punch_out": None}
        old = current.get(key, defaults.get(key))
        if key == "punch_in" or key == "punch_out":
            old_cmp, new_cmp = as_utc(old), as_utc(new)
            if old_cmp != new_cmp:
                changes[key] = {"from": old_cmp, "to": new_cmp}
        elif key == "work_hours":
            if old != new: changes[key] = {"from": old, "to": new}
        elif old != new:
            changes[key] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "regularization must change at least one field")
    history_entry = {"at": now_utc_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-set against the observed history and values. Only one concurrent edit wins;
    # the loser receives 409 rather than overwriting another correction's audit entry.
    compare = {"_id": current["_id"]}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if key in current:
            compare[key] = current[key]
        else:
            compare[key] = {"$exists": False}
    if "history" in current:
        compare["history"] = current["history"]
    else:
        compare["history"] = {"$exists": False}
    update = {"$set": final, "$push": {"history": history_entry}}
    updated = db.attendance_logs.find_one_and_update(compare, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently; retry")
    return public_attendance(updated)


# ------------------------------ aggregation helpers -------------------------
def month_bounds_strings(month: str) -> tuple[str, str, date, date]:
    start, end = validate_month(month)
    return start.isoformat(), end.isoformat(), start, end


def attendance_month_match(emp_code: Optional[str], start_s: str, end_s: str) -> dict[str, Any]:
    q: dict[str, Any] = {"date": {"$gte": start_s, "$lte": end_s}}
    if emp_code is not None: q["emp_code"] = emp_code
    return q


def employee_monthly_pipeline(emp_code: str, start_s: str, end_s: str) -> list[dict[str, Any]]:
    return [
        {"$match": attendance_month_match(emp_code, start_s, end_s)},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$date", None]},
                           {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employee_or_404(emp_code)
    start_s, end_s, start, end = month_bounds_strings(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start_s, end_s)))
    agg = rows[0] if rows else {}
    first_eligible = max(start, parse_date(emp["joined_on"], "joined_on"))
    days = working_days(first_eligible, end)
    present = float(agg.get("present_days", 0) or 0)
    return {"emp_code": emp_code, "month": month, "working_days": days,
            "present_days": half_up(present, 2), "leave_days": int(agg.get("leave_days", 0) or 0),
            "late_count": int(agg.get("late_count", 0) or 0),
            "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
            "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
            "attendance_pct": round_rate(present, days)}


def department_summary_pipeline(start_s: str, end_s: str, department: Optional[str]) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joinDate": "$joined_on"},
          "pipeline": [{"$match": {"$expr": {"$and": [
              {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]},
              {"$lte": ["$date", end_s]}, {"$gte": ["$date", "$$joinDate"]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "employee_codes": {"$addToSet": "$emp_code"},
          "present_days": {"$sum": {"$cond": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
              {"$cond": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}, 0]}, 0]}},
          "work_hours_sum": {"$sum": {"$cond": [{"$and": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},
          "work_hours_count": {"$sum": {"$cond": [{"$and": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employee_codes"},
          "present_days": {"$round": ["$present_days", 2]},
          "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
             {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
          "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds_strings(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(start_s, end_s, department)))
    # MongoDB $round uses ties-to-even; apply contract's half-up at the API boundary.
    for row in rows:
        row["present_days"] = half_up(row.get("present_days", 0), 2)
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = half_up(row["avg_work_hours"], 2)
    return {"month": month, "items": rows}


def leaderboard_pipeline(start_s: str, end_s: str, department: Optional[str], limit: int) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        stages.append({"$match": {"employee.department": department}})
    stages.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                      "total_late_minutes": 1, "late_count": 1}},
    ])
    return stages



@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds_strings(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(start_s, end_s, department, limit)))
    return {"month": month, "items": rows}


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    # Generate the calendar inside MongoDB (not in a Python loop), then look up the
    # department's logs for each day. This also returns days with no attendance.
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None,
          "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
          "codes": {"$addToSet": "$emp_code"}}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, {"$add": [
          {"$dateDiff": {"startDate": start_dt, "endDate": end_dt, "unit": "day"}}, 1]}]},
          "as": "offset", "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"day_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}},
          "headcount": {"$size": {"$filter": {"input": "$employees", "as": "employee",
            "cond": {"$lte": ["$$employee.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}}]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$day_string", "codes": "$codes"}, "pipeline": [
          {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}], "as": "logs"}},
        {"$set": {"present_count": {"$reduce": {"input": "$logs", "initialValue": 0,
          "in": {"$add": ["$$value", {"$cond": [{"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
            {"$cond": [{"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]}, 0.5, 1]}, 0]}]}}},
          "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log",
            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
          "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$days"}, [1, 7]]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
          {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$day_string", "is_working_day": 1, "headcount": 1,
          "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
          "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    # The pipeline generates every requested calendar day inside MongoDB, including gaps.
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    # Return the contract's calendar date and half-up rounded rate values.
    for row in rows:
        if isinstance(row.get("date"), datetime): row["date"] = row["date"].date().isoformat()
        if row.get("attendance_rate") is not None: row["attendance_rate"] = half_up(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None: row["moving_avg_7d"] = half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


# ------------------------------- explain ------------------------------------
def explain_find(collection, query: dict[str, Any], sort: list[tuple[str, int]], page: int, page_size: int):
    command = {"find": collection.name, "filter": query, "sort": dict(sort),
               "skip": (page - 1) * page_size, "limit": page_size}
    return db.command("explain", command, verbosity="executionStats")


def explain_aggregate(collection, pipeline: list[dict[str, Any]]):
    return db.command("explain", {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)
    try:
        if endpoint == "attendance_list":
            start = parse_date(date_from, "date_from") if date_from else None
            end = parse_date(date_to, "date_to") if date_to else None
            if start and end and start > end: raise HTTPException(422, "date_from must not be after date_to")
            q: dict[str, Any] = {}
            if emp_code: q["emp_code"] = emp_code
            if start or end:
                q["date"] = {}
                if date_from: q["date"]["$gte"] = date_from
                if date_to: q["date"]["$lte"] = date_to
            if status:
                if status not in ALL_STATUSES: raise HTTPException(422, "invalid status")
                q["status"] = status
            result = explain_find(db.attendance_logs, q, [("date", DESCENDING), ("emp_code", ASCENDING)], page, page_size)
            coll = "attendance_logs"
        elif endpoint == "employee_monthly":
            if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
            start_s, end_s, _, _ = month_bounds_strings(month)
            result = explain_aggregate(db.attendance_logs, employee_monthly_pipeline(emp_code, start_s, end_s)); coll = "attendance_logs"
        elif endpoint == "department_summary":
            if not month: raise HTTPException(422, "month is required")
            start_s, end_s, _, _ = month_bounds_strings(month)
            result = explain_aggregate(db.employees, department_summary_pipeline(start_s, end_s, department)); coll = "employees"
        elif endpoint == "late_leaderboard":
            if not month: raise HTTPException(422, "month is required")
            start_s, end_s, _, _ = month_bounds_strings(month)
            result = explain_aggregate(db.attendance_logs, leaderboard_pipeline(start_s, end_s, department, limit)); coll = "attendance_logs"
        elif endpoint == "department_trend":
            if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
            start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
            if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "invalid trend range")
            result = explain_aggregate(db.employees, department_trend_pipeline(department, start, end)); coll = "employees"
        else:
            raise HTTPException(422, "unsupported endpoint")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB explain failed") from exc
    return {"endpoint": endpoint, "collection": coll, "explain": mongo_json(result)}
