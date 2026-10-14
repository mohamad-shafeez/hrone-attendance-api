# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` plus `date`. The second guarantees one attendance record per employee per day, even when requests arrive together. Other indexes support department and join-date filters, attendance date sorting, employee/date analytics, status filters, and finding the latest punch-in.

2. **Punch-in race.** I do not rely on checking for a record before inserting. Both requests can reach MongoDB at once, but the unique index allows one insert and rejects the duplicate. The API returns 201 for the winner and 409 for the other request.

3. **Ties.** The leaderboard calculates rank before applying the limit. Employees with equal late minutes share a rank, and the next rank is skipped. If a tie reaches the cutoff, everyone with a rank within the limit is returned.

4. **Headcount.** The department summary starts from employees and looks up their attendance. Employees with no logs are still counted if they joined by the period end; people who join later are excluded.

5. **100x data.** I would measure query plans and timings on realistic data, then consider pre-aggregated daily summaries if live aggregations became too expensive.
