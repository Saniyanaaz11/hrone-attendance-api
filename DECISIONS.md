# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (duplicate create -> 409). `attendance_logs (emp_code, date)` unique (one record per day, punch-in race, PATCH lookup, monthly). `(date desc, emp_code)` for the list sort and month ranges. `(status, date desc, emp_code)` for the status filter. `(emp_code, punch_in desc)` for punch-out. `employees (department, joined_on)` for headcount. Rejected: an index on `late_minutes`; the month filter already narrows the data and it would only slow writes.

2. **Punch-in race.** Both requests compute the same record and call `insert_one`. The unique `(emp_code, date)` index lets one succeed; the other gets `DuplicateKeyError` and I return 409. No find-then-insert.

3. **Ties.** I rank with `$rank` first and filter `rank <= limit` afterwards, so a tie at the cutoff returns both people (1, 2, 2, 4), even if that is more than `limit` rows. Ordered by minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins the logs with `preserveNullAndEmptyArrays`, then counts distinct `emp_code`. Someone with no logs still counts once.

5. **100x data.** I would keep pre-aggregated daily counts per department, updated on write, instead of scanning raw logs, and use keyset pagination instead of skip.