# Tiffin Manager — Version 3 review build

This is an isolated upgrade copy. The original application and database in the parent folder have not been modified. The included `tiffin.db` is a snapshot for review, not a live synchronized database.

## Preview safely

On this Mac, from the project parent folder:

```sh
version3/.venv/bin/python version3/preview.py
```

Open http://127.0.0.1:5003. Preview runs on a temporary copy of version3/tiffin.db. Entries made in preview disappear when it stops. Do not record real office transactions in preview.

## Windows setup and launch

1. Copy this version3 folder to a separate writable location. Do not overwrite the existing installation.
2. Install Python 3.12 or later and enable “Add Python to PATH.”
3. Run `Setup_Requirements.bat` once (internet required for package installation).
4. Run `Start_Tiffin.bat`. This launches the version3 app against its own database.

Python 3.9–3.11 can run in a normal local browser instead of the dedicated desktop wrapper. Application assets are local, so ordinary use does not require internet. WhatsApp links still require the relevant external service.

For preview on Windows after setup: `.venv\Scripts\python preview.py`.

## Changes included

- Fresh database initialization and legacy vendor migration corrected.
- Bulk meal edits preserve locked historical prices. The grid also displays totals using those rates. Existing duplicate slots are blocked for correction through History.
- Server validation for financial amounts, quantities, required fields, dates, months, and selected employees; signed manager-fund entries remain supported.
- CSRF protection, persistent single-use form tokens, and redirect after successful entry to prevent refresh/retry duplicates.
- Employee deletion blocked when any meal/payment/loan history exists, including recycled records.
- Loan-linked payments protected from independent editing/deletion; loan restoration and purge keep child payments together.
- SQLite snapshot backups, integrity verification, backup before startup changes, and startup stopped if the backup fails.
- PDF downloads generated in memory, avoiding overwritten report files and concurrent-download filename conflicts.
- Database paths anchored to the application directory, including the standalone importer. The importer skips exact name/department matches on repeat runs.
- Shared navigation, consistent spacing, responsive form layouts, scrollable wide tables, sticky staff names in bulk entry, visible touch actions, and keyboard focus indicators.
- Local pinned Tailwind, Chart.js, and Lucide assets; system fonts replace network fonts.
- Local calendar dates in entry forms; safer JSON embedding for chart data.
- Query indexes and reproducible top-level dependency versions.

## Validation

```sh
.venv/bin/python -m pytest tests -q --disable-warnings
```

On Windows use `.venv\Scripts\python` instead. Install `requirements-dev.txt` to obtain pytest.

The regression suite covers fresh/repeated initialization, all primary pages, old-price edits, repeat submissions, invalid payments, employee history protection, loan lifecycle, linked bulk restore, backup recovery, PDF/Excel downloads, and unchanged active accounting records after migrating a disposable copy of the office database.

Desktop and narrow-window layouts were inspected in a browser. Windows installer/packaged executable and actual WhatsApp sending were not tested on this Mac. Existing FPDF calls produce deprecation warnings; the tested exports succeed.

## Boundaries and rollout

Current accounting formulas, the isolated manager fund, and the existing seven-day recycle retention remain in place. Startup may purge expired recycle entries in the version3 database after a verified backup. Repeated backup copies are still stored on the same computer; copy important backups to independent storage.

This phase does not add user accounts, month closing, inventory alerts, or a transaction audit trail. Those require separate feature approval.

Before live use, review the preview, stop both applications, and take an external backup. An approved cutover must use the latest office database, not this potentially older snapshot. Reconcile balances and reports before entering transactions in version3. Keep the previous installation available for rollback; never run both versions for live entry against divergent copies.

## Source distribution

`app.spec` intentionally excludes the operational database. Keep office data private when distributing code. The Mac `.venv` is machine-specific and should not be copied to Windows; run Windows setup instead. `requirements-lock.txt` records the tested Mac environment, while `requirements.txt` is the cross-platform top-level installation list.
