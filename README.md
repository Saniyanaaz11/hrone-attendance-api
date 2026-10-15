# HROne Attendance API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv
.venv\Scripts\activate        # or: source .venv/bin/activate
pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017   # real env vars win over a local .env
set MONGO_DB=attendance_db
python sample_seed.py                     # optional, loads the nine sample docs
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup. `GET /health` returns 200 once MongoDB answers.

## Notes
- Everything in the contract is implemented, including `/admin/explain/{endpoint}`.
- Analytics are MongoDB aggregation pipelines and read the stored derived values.
- `PATCH /attendance/{emp_code}/{date}` retries on a concurrent edit and returns 409 if it keeps losing, so history entries are never dropped.
- I tested against my own data (ties, weekends, a mid-month joiner, overnight shift, an employee with no logs) and about 90k generated records. Every explain plan used IXSCAN with no COLLSCAN.