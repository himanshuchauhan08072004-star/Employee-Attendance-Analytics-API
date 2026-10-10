# DECISIONS.md

### 1. Indexes
I added indexes to make common database queries faster and prevent duplicate records. The `employees` collection has a unique index on `emp_code`, along with indexes on department and joining date. For `attendance_logs`, I added a unique index on `(emp_code, date)` and other indexes to support date-based filtering, sorting, and punch-out lookups.

### 2. Two punch-ins at the same time
If two punch-in requests arrive for the same employee on the same day, both may try to create a record. The unique `(emp_code, date)` index ensures that only one record can be inserted. MongoDB rejects the duplicate request, and the API returns a `409 Conflict` response.

### 3. Ties in the leaderboard
I used MongoDB's `$rank`. Employees with the same total receive the same rank, so the ranking can look like `1, 2, 2, 4`. I apply the limit after calculating the ranks, which means employees tied at the cutoff are all included, even if the result contains more rows than the requested limit.

### 4. Employees without attendance records
The department summary starts with the `employees` collection and then uses `$lookup` to find their attendance records. This way, employees who have joined by the end of the reporting period are still included in the headcount, even if they have no attendance logs. 

### 5. Handling 100 times more data
If the dataset grows significantly, repeatedly calculating analytics from all attendance records could become slow. I would consider maintaining a summary collection with daily totals for each department and updating it as attendance data changes. I would also consider archiving older records by month.