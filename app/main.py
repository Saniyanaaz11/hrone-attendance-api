"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import json
import os
from calendar import monthrange
from datetime import date as date_type, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # load_dotenv does not override real environment variables

# tz_aware=True: datetimes read from MongoDB come back as UTC-aware, not naive.
# The short timeout makes /health fail fast when MongoDB is down.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS = 100_000_000_000      # anything smaller looks like seconds -> reject
MAX_MS = 4_102_444_800_000    # year 2100

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# Create indexes at startup (create_index is idempotent, safe to run every time).
@app.on_event("startup")
def create_indexes():
    db.command("ping")

    # One employee per emp_code, enforced by the database.
    db.employees.create_index([("emp_code", 1)], unique=True)

    # One attendance record per (emp_code, date), enforced by the database.
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)

    # Serves the attendance list sort: date desc, emp_code asc.
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    # status filter + the same sort as the list.
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])

    # Punch-out: latest record of one employee at or before a time.
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])

    # Department filters and "joined on or before" checks.
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])


# --------------------------------------------------------------------------- #
# Small helpers: time conversion, rounding, serializing
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    # Whole seconds only (R1).
    return datetime.now(timezone.utc).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    # Integer division by 1000 drops the milliseconds (truncate to whole seconds).
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:  # safety net: treat naive values as UTC
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value, places: int = 2) -> float:
    # Decimal + ROUND_HALF_UP, because Python's round() is banker's rounding.
    quant = Decimal(1).scaleb(-places)  # places=2 -> Decimal("0.01")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def valid_date(text: str) -> bool:
    # Strict YYYY-MM-DD: "2026-7-6" parses, but does not equal its isoformat().
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat() == text
    except ValueError:
        return False


def parse_hhmm(text: str):
    h, m = text.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_day_and_start(punch_in: datetime, shift_start: str, shift_end: str):
    """Returns (attendance date string, shift start as an IST datetime). Rule R1."""
    local = punch_in.astimezone(IST)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)

    day = local.date()
    if is_overnight(shift_start, shift_end):
        # Punch-in earlier than shift_end (IST) belongs to the shift that
        # started the previous day.
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < eh * 3600 + em * 60:
            day = day - timedelta(days=1)

    start = datetime.combine(day, dtime(sh, sm), tzinfo=IST)
    return day.isoformat(), start


# --------------------------------------------------------------------------- #
# Business rule helpers (R2, R3, R4)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    _, start = attendance_day_and_start(punch_in, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    # Late only if strictly more than 10:00 after start (600 s).
    # Minutes are counted from shift start, floored.
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    eh, em = parse_hhmm(shift_end)
    end_day = date_type.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        end_day = end_day + timedelta(days=1)  # overnight shift ends next day
    shift_end_dt = datetime.combine(end_day, dtime(eh, em), tzinfo=IST)
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Turning Mongo documents into API responses
# --------------------------------------------------------------------------- #
def serialize_history(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {
                "from": dt_to_ms(change.get("from")),
                "to": dt_to_ms(change.get("to")),
            }
        else:
            changes[field] = change
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_record(doc: dict) -> dict:
    # Picks fields one by one, so _id can never leak, and fills defaults
    # for legacy documents that lack half_day / history / etc.
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history(h) for h in doc.get("history", [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"

# strict=True: floats and strings are rejected (422); the range rejects
# values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_date(cls, v):
        if not valid_date(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class AttendanceCorrectionIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # No find_one first: the unique index decides, so two simultaneous
        # requests produce exactly one 201 and one 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)  # same filter as the list

    cursor = (
        db.employees.find(query)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Epoch ms -> UTC datetime (whole seconds). No server-local time anywhere.
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    day, _ = attendance_day_and_start(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        # The unique (emp_code, date) index makes this race-safe.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_record(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Latest record that started at or before this punch-out (works across midnight).
    # ABSENT/LEAVE rows have a null punch_in, so they never match.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    seconds = (ts - record["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])

    # Only closes the record if it is still open, so of two simultaneous
    # punch-outs exactly one matches.
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": work_hours < 4.5,
        }},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "already punched out")

    return serialize_record(db.attendance_logs.find_one({"_id": record["_id"]}))


# --------------------------------------------------------------------------- #
# Regularization (PATCH)
# --------------------------------------------------------------------------- #
def build_correction(emp: dict, record: dict, body: AttendanceCorrectionIn):
    """Returns (values to $set, history entry) or raises 422."""
    date = record["date"]
    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    status = body.status or old["status"]

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if body.punch_in is not None:
            day, _ = attendance_day_and_start(new_in, emp["shift_start"], emp["shift_end"])
            if day != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            secs = (new_out - new_in).total_seconds()
            if secs <= 0 or secs > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    if status in ("ABSENT", "LEAVE"):
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        new["late_minutes"] = compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"])
        if new_out is None:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            hours = compute_work_hours(new_in, new_out)
            new.update(
                work_hours=hours,
                overtime_minutes=compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date),
                half_day=hours < 4.5,
            )

    if all(new[k] == old[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "nothing to change")

    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    return new, entry


@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    if not valid_date(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Optimistic concurrency: the update only applies if the record is still
    # exactly what we read (same history length, status and punches). If
    # somebody got in first we re-read and try again instead of losing an entry.
    for _ in range(8):
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not record:
            raise HTTPException(404, "Attendance record not found")

        new, entry = build_correction(emp, record, body)

        if "history" in record:
            history_guard = {"$size": len(record["history"])}
        else:
            history_guard = {"$exists": False}
        guard = {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": history_guard,
        }
        res = db.attendance_logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
        if res.matched_count:
            return serialize_record(db.attendance_logs.find_one({"_id": record["_id"]}))

    raise HTTPException(409, "record is being changed by someone else, try again")


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def month_bounds(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Raises 422 on junk."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        last = monthrange(year, mon)[1]
        date_type(year, mon, 1)
    except (ValueError, IndexError):
        raise HTTPException(422, "month must be YYYY-MM")
    return f"{month}-01", f"{month}-{last:02d}"


def count_weekdays(first: str, last: str) -> int:
    day, end, n = date_type.fromisoformat(first), date_type.fromisoformat(last), 0
    while day <= end:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


def is_weekday_expr(date_field: str) -> dict:
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday
    return {"$not": [{"$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}, [1, 7]
    ]}]}


def is_present_expr(status_field: str) -> dict:
    return {"$in": [status_field, PRESENT_STATUSES]}


def day_weight_expr(half_day_field: str) -> dict:
    return {"$cond": [{"$eq": [half_day_field, True]}, 0.5, 1]}


def is_late_expr(late_field: str) -> dict:
    return {"$gt": [{"$ifNull": [late_field, 0]}, 0]}


def half_up_expr(expr, places: int) -> dict:
    # Mongo's $round is banker's rounding, so do half-up by hand, in
    # Decimal128 so that exact ties (0.03125) are not hit by float noise.
    scale = 10 ** places
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [
            {"$multiply": [{"$toDecimal": expr}, scale]},
            {"$toDecimal": 0.5},
        ]}},
        scale,
    ]}}


# --------------------------------------------------------------------------- #
# Query builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    for value in (date_from, date_to):
        if value is not None and not valid_date(value):
            raise HTTPException(422, "dates must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    return q


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present_expr("$status"), is_weekday_expr("$date")]},
                day_weight_expr("$half_day"),
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [is_late_expr("$late_minutes"), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    emp_match = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # Left join: employees without any logs keep an empty array.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$set": {"has_hours": {"$and": [
            is_present_expr("$log.status"),
            {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
        ]}}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present_expr("$log.status"), is_weekday_expr("$log.date")]},
                day_weight_expr("$log.half_day"),
                0,
            ]}},
            "hours_sum": {"$sum": {"$cond": ["$has_hours", {"$toDecimal": "$log.work_hours"}, 0]}},
            "hours_n": {"$sum": {"$cond": ["$has_hours", 1, 0]}},
            "late_count": {"$sum": {"$cond": [is_late_expr("$log.late_minutes"), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$emps"},
            "present_days": 1,
            "hours_sum": 1,
            "hours_n": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # Inner join: logs of unknown employees disappear here.
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        # Before ranking, so the ranks are inside the department.
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},  # 1, 2, 2, 4
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def trend_range(from_date: str, to_date: str):
    if not (valid_date(from_date) and valid_date(to_date)):
        raise HTTPException(422, "from and to must be YYYY-MM-DD")
    first, last = date_type.fromisoformat(from_date), date_type.fromisoformat(to_date)
    if last < first:
        raise HTTPException(422, "to must be on or after from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "range cannot exceed 92 days")
    return first, last


def trend_pipeline(department: str, first: date_type, last: date_type) -> list:
    lower = datetime.combine(first, dtime(0, 0), tzinfo=timezone.utc)
    upper = datetime.combine(last + timedelta(days=1), dtime(0, 0), tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "log",
        }},
        {"$unwind": "$log"},
        {"$group": {
            "_id": "$log.date",
            "present_count": {"$sum": {"$cond": [
                is_present_expr("$log.status"), day_weight_expr("$log.half_day"), 0
            ]}},
            "late_count": {"$sum": {"$cond": [is_late_expr("$log.late_minutes"), 1, 0]}},
        }},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # Gap filling: one document for every calendar day in the range.
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": [lower, upper]},
        }},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", {"$literal": department}]},
                    {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr(
                {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, 4
            ),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"avg7": {"$avg": {"$toDecimal": "$rate"}, "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": "$rate",
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$avg7", None]}, None, half_up_expr("$avg7", 4)
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    start, end = month_bounds(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = count_weekdays(max(start, emp["joined_on"]), end) if emp["joined_on"] <= end else 0

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)))
    stats = rows[0] if rows else {}
    present = stats.get("present_days", 0)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_RE),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    items = []
    for row in db.employees.aggregate(summary_pipeline(start, end, department)):
        n = row.pop("hours_n")
        total = row.pop("hours_sum")
        total = total.to_decimal() if hasattr(total, "to_decimal") else Decimal(total)
        row["avg_work_hours"] = round_half_up(total / n, 2) if n else None
        row["present_days"] = float(row["present_days"])
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    first, last = trend_range(from_date, to_date)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, first, last)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _need(**params):
    missing = [k for k, v in params.items() if v in (None, "")]
    if missing:
        raise HTTPException(422, f"missing query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            collection = "attendance_logs"
            pipeline = monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            _need(month=month)
            collection = "employees"
            pipeline = summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            collection = "attendance_logs"
            pipeline = leaderboard_pipeline(*month_bounds(month), limit, department)
        elif endpoint == "department_trend":
            _need(department=department, **{"from": from_date, "to": to_date})
            collection = "employees"
            pipeline = trend_pipeline(department, *trend_range(from_date, to_date))
        else:
            raise HTTPException(422, "unknown endpoint")
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command("explain", command, verbosity="executionStats")
    # The explain document can hold ObjectIds / datetimes, so go through bson.json_util.
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}