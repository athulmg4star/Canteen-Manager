import os
import sys
import sqlite3
import datetime
from flask import Flask, render_template, send_file, request, redirect, url_for, jsonify, abort, session, g, has_request_context
from fpdf import FPDF
import pandas as pd
import io

app = Flask(__name__)
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("TIFFIN_DB", os.path.join(BASE_DIR, "tiffin.db"))

def connect_db():
    connection = sqlite3.connect(DB_FILE, timeout=10)
    if has_request_context():
        g.setdefault("db_connections", []).append(connection)
    return connection

@app.teardown_request
def close_request_connections(error=None):
    for connection in g.pop("db_connections", []):
        connection.close()

# Per-launch local session key; restarting invalidates open forms safely.
import secrets
import math
app.secret_key = secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")

@app.context_processor
def form_security():
    session.setdefault("csrf_token", secrets.token_hex(32))
    return {"csrf_token": session["csrf_token"], "submission_token": lambda: secrets.token_hex(24)}

@app.before_request
def validate_submission():
    for key in ("month", "filter_month", "report_month"):
        value = request.args.get(key)
        if value:
            try:
                if len(value) != 7:
                    raise ValueError()
                datetime.datetime.strptime(value, "%Y-%m")
            except ValueError:
                abort(400, description="Choose a valid month.")
    if request.method != "POST":
        return
    if not secrets.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "missing")):
        abort(400, description="This form has expired. Reload the page and try again.")
    for key, value in request.form.items(multi=True):
        if not value or key == "csrf_token":
            continue
        if key.endswith("date") or key in ("log_date", "payment_date", "purchase_date"):
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                abort(400, description="Enter a valid date in YYYY-MM-DD format.")
        if key.startswith(("qty_", "amt_")) or key in ("amount", "new_amount", "new_qty", "quantity", "new_price", "current_price", "price", "b_amount[]", "opening_balance", "new_balance", "canteen_opening_balance"):
            try:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError()
                if "balance" not in key and request.endpoint not in ("manager_fund", "edit_manager_fund_entry") and number < 0:
                    raise ValueError()
                if key.startswith("qty_") or key == "new_qty":
                    int(value)  # Route handlers require whole-number text.
                    if not number.is_integer():
                        raise ValueError()
                if key in ("amount", "new_amount") and request.endpoint not in ("manager_fund", "edit_manager_fund_entry") and number <= 0:
                    raise ValueError()
            except ValueError:
                abort(400, description="Enter valid quantities and amounts. Payments must be positive.")
    required = {
        "payments": ("employee_id", "amount", "payment_date"),
        "log_meals": ("employee_id", "log_date"), "bulk_log": ("log_date",),
        "bulk_payments": ("payment_date",), "bulk_purchases": ("purchase_date",),
        "purchases": ("purchase_date", "category", "amount"),
        "staff_loans": ("employee_id", "amount", "loan_date", "repay_method"),
        "consumption": ("item_id", "quantity", "amount", "purchase_date"),
        "add_misc_income": ("income_date", "source", "amount"),
        "add_item": ("item_name", "price"),
    }.get(request.endpoint, ())
    if any(not request.form.get(key, "").strip() for key in required):
        abort(400, description="Complete all required fields before saving.")
    bulk_ids = set()
    for key in request.form:
        if key.startswith("qty_"):
            parts = key.split("_", 2)
            if len(parts) == 3 and parts[1].isdigit():
                bulk_ids.add(parts[1])
        elif key.startswith("amt_"):
            bulk_ids.add(key.split("_", 1)[1])
    if bulk_ids:
        with connect_db() as conn:
            valid_ids = {str(row[0]) for row in conn.execute("SELECT employee_id FROM Employees")}
        if not bulk_ids.issubset(valid_ids):
            abort(400, description="One of the selected employees no longer exists. Reload the page.")
    employee_id = request.form.get("employee_id")
    if employee_id:
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM Employees WHERE employee_id = ?", (employee_id,)).fetchone():
                abort(400, description="Choose an existing employee.")

    token = request.form.get("submission_token", "")
    if len(token) != 48:
        abort(400, description="Reload this page before saving the form.")
    with connect_db() as conn:
        try:
            conn.execute("INSERT INTO Form_Submissions (token, endpoint) VALUES (?, ?)", (token, request.path))
        except sqlite3.IntegrityError:
            abort(409, description="This form was already submitted. Reload the page before making another entry.")
    g.submission_token = token

@app.after_request
def finish_submission(response):
    if response.status_code >= 400:
        for connection in g.get("db_connections", []):
            try:
                connection.rollback()
            except sqlite3.ProgrammingError:
                pass  # Some handlers already closed their connection.
    if response.status_code >= 400 and getattr(g, "submission_token", None):
        with connect_db() as conn:
            conn.execute("DELETE FROM Form_Submissions WHERE token = ?", (g.submission_token,))
    # Redirect successful entry forms so browser refresh cannot repost them.
    if request.method == "POST" and response.status_code == 200:
        return redirect(request.path + "?success=1", code=303)
    return response

@app.errorhandler(400)
@app.errorhandler(409)
def show_input_error(error):
    return render_template("error.html", message=error.description), error.code

# Table -> (primary key column, ordered list of real data columns excluding
# soft-delete bookkeeping). Used to set up the Recycle Bin: each real table
# gets renamed to "<Table>_Raw" and a VIEW named "<Table>" is created that
# transparently filters out soft-deleted rows for every existing read query
# in the app (no other code needs to change for reads). Writes are handled
# explicitly against "<Table>_Raw" at each call site.
RECYCLE_BIN_TABLES = {
    "Payments": ("payment_id", ["payment_id", "employee_id", "amount", "payment_date"]),
    "Purchases": ("purchase_id", ["purchase_id", "item_category", "description", "amount", "purchase_date", "vendor"]),
    "Meal_Log": ("log_id", ["log_id", "employee_id", "item_name", "quantity", "locked_price", "log_date"]),
    "Misc_Income": ("income_id", ["income_id", "source", "amount", "income_date"]),
    "Staff_Loans": ("loan_id", ["loan_id", "employee_id", "amount", "loan_date", "repay_method", "status", "settled_date", "linked_payment_id"]),
}

def ensure_recycle_bin(cursor):
    """Idempotent: safe to call any number of times, at any point in init_db."""
    for table, (pk, cols) in RECYCLE_BIN_TABLES.items():
        raw_table = f"{table}_Raw"

        cursor.execute("SELECT type FROM sqlite_master WHERE name = ?", (table,))
        row = cursor.fetchone()
        currently_a_table = row is not None and row[0] == 'table'

        if currently_a_table:
            # First time seeing this table under its original name -- add
            # soft-delete columns, then rename it out of the way.
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN is_deleted INTEGER DEFAULT 0")
            except:
                pass
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
            except:
                pass
            cursor.execute(f"ALTER TABLE {table} RENAME TO {raw_table}")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (raw_table,))
        raw_exists = cursor.fetchone() is not None

        if raw_exists:
            col_list = ", ".join(cols)
            cursor.execute(f"DROP VIEW IF EXISTS {table}")
            cursor.execute(f"""
                CREATE VIEW {table} AS
                SELECT {col_list}, is_deleted, deleted_at FROM {raw_table}
                WHERE is_deleted = 0 OR is_deleted IS NULL
            """)

RECYCLE_BIN_RETENTION_DAYS = 7

def purge_old_deleted_items(cursor):
    """Permanently removes soft-deleted rows older than the retention window."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=RECYCLE_BIN_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    for table, (pk, cols) in RECYCLE_BIN_TABLES.items():
        raw_table = f"{table}_Raw"
        try:
            date_column = {"Payments": "payment_date", "Purchases": "purchase_date", "Meal_Log": "log_date", "Misc_Income": "income_date", "Staff_Loans": "loan_date"}[table]
            cursor.execute(f"DELETE FROM {raw_table} WHERE is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ? AND NOT EXISTS (SELECT 1 FROM Closed_Months WHERE month >= substr({date_column},1,7))", (cutoff,))
        except Exception:
            pass  # table might not exist yet on a very first run before migration

def init_db():
    db_is_new = not os.path.exists(DB_FILE) or os.path.getsize(DB_FILE) == 0
    conn = connect_db()
    cursor = conn.cursor()

    if db_is_new:
        print("Initializing database Version 2.0...")
        cursor.execute("CREATE TABLE IF NOT EXISTS Employees (employee_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, department TEXT, is_active INTEGER DEFAULT 1, opening_balance REAL DEFAULT 0.0)")
        cursor.execute("CREATE TABLE IF NOT EXISTS Items_Master (item_id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT UNIQUE NOT NULL, current_price REAL NOT NULL, is_active INTEGER DEFAULT 1, display_order INTEGER DEFAULT 999)")
        cursor.execute("CREATE TABLE IF NOT EXISTS Meal_Log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, item_name TEXT, quantity INTEGER, locked_price REAL, log_date TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS Payments (payment_id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, amount REAL, payment_date TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS Purchases (purchase_id INTEGER PRIMARY KEY AUTOINCREMENT, item_category TEXT, description TEXT, amount REAL, purchase_date TEXT, vendor TEXT DEFAULT 'General')")
        
        cursor.execute("CREATE TABLE IF NOT EXISTS Expense_Categories (category_id INTEGER PRIMARY KEY AUTOINCREMENT, category_name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)")
        
        default_categories = [("Groceries & Provisions",), ("Vegetables & Meat",), ("Gas Cylinder / Fuel",), ("Miscellaneous / Cleaning",)]
        cursor.executemany("INSERT OR IGNORE INTO Expense_Categories (category_name) VALUES (?)", default_categories)
        
        cursor.execute("CREATE TABLE IF NOT EXISTS Global_Settings (id INTEGER PRIMARY KEY CHECK (id = 1), canteen_opening_balance REAL DEFAULT 0.0, canteen_manager TEXT DEFAULT '', committee_members TEXT DEFAULT '')")
        cursor.execute("INSERT OR IGNORE INTO Global_Settings (id, canteen_opening_balance) VALUES (1, 0.0)")
        
        cursor.executemany("INSERT OR IGNORE INTO Items_Master (item_name, current_price) VALUES (?, ?)", 
                           [("Breakfast", 30.00), ("Lunch", 40.00), ("Tea", 8.00), ("Snacks", 8.00), ("Omelette", 15.00), ("Egg", 12.00)])
        starter_staff = [("Akhil Nambiar", "Administration", 1, 0.0), ("Manish Singh", "Operations", 1, 0.0), 
                         ("Sreejesh EV", "Accounts", 1, 0.0), ("Athul M", "Administration", 1, 0.0),
                         ("Deepa Nair", "Accounts", 1, 0.0), ("Rahul Sharma", "Establishment", 1, 0.0)]
        cursor.executemany("INSERT OR IGNORE INTO Employees (name, department, is_active, opening_balance) VALUES (?, ?, ?, ?)", starter_staff)


    cursor.execute("CREATE TABLE IF NOT EXISTS Form_Submissions (token TEXT PRIMARY KEY, endpoint TEXT NOT NULL)")

    purchase_table = "Purchases_Raw" if cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Purchases_Raw'").fetchone() else "Purchases"
    columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({purchase_table})")}
    if columns and "vendor" not in columns:
        cursor.execute(f"ALTER TABLE {purchase_table} ADD COLUMN vendor TEXT DEFAULT 'General'")

    # Recycle Bin setup runs FIRST, before anything else touches these tables.
    ensure_recycle_bin(cursor)
    
    # --- DATABASE UPGRADES (Runs every time to ensure tables exist) ---
    try:
        cursor.execute("ALTER TABLE Purchases ADD COLUMN vendor TEXT DEFAULT 'General'")
    except:
        pass 
        
    try:
        cursor.execute("ALTER TABLE Global_Settings ADD COLUMN canteen_manager TEXT DEFAULT 'Aleena Jacob'")
    except:
        pass
        
    try:
        cursor.execute("ALTER TABLE Global_Settings ADD COLUMN committee_members TEXT DEFAULT 'Sabin P S, Abhilash, Sreelekshmi, Lekshmi'")
    except:
        pass
    # --- DATABASE UPGRADES (Runs every time to ensure tables exist) ---
    
    # [Your existing upgrades for vendor, manager, etc. are here]
    
    try:
        # 1. Fixes the Milk typo in the Dropdown menu
        cursor.execute("UPDATE Expense_Categories SET category_name = 'Milk/Milk Products' WHERE category_name = 'Milk/Milk Procucts'")
        
        # 2. Fixes your past historical records for Milk so Excel exports look correct
        cursor.execute("UPDATE Purchases_Raw SET item_category = 'Milk/Milk Products' WHERE item_category = 'Milk/Milk Procucts'")
        
        # 3. Adds 'Oil and Tea' as a brand new Expense Category
        cursor.execute("INSERT OR IGNORE INTO Expense_Categories (category_name) VALUES ('Oil and Tea')")
    except:
        pass
    # -------------------------------------------------------------------------------
        
    cursor.execute("CREATE TABLE IF NOT EXISTS Misc_Income (income_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, amount REAL, income_date TEXT)")
    # -------------------------------------------------------------------------------

    # NEW: Staff Loans — informal, interest-free money staff give the canteen
    # when expenses go negative, repaid either in cash later or adjusted
    # against their own next bill.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Staff_Loans (
        loan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER,
        amount REAL,
        loan_date TEXT,
        repay_method TEXT,
        status TEXT DEFAULT 'outstanding',
        settled_date TEXT,
        linked_payment_id INTEGER
    )""")
    # -------------------------------------------------------------------------------

    # NEW: Soft-delete support (Recycle Bin) across the 5 money-related tables.
    # Instead of DELETE, these rows get is_deleted=1 + deleted_at set, and are
    # excluded from every normal query. Permanently purged after 7 days.
    for table in ["Payments", "Purchases", "Meal_Log", "Misc_Income", "Staff_Loans"]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN is_deleted INTEGER DEFAULT 0")
        except:
            pass
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
        except:
            pass
    # -------------------------------------------------------------------------------

    # NEW: Price change history for Items_Master
    cursor.execute("""CREATE TABLE IF NOT EXISTS Price_History (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT,
        old_price REAL,
        new_price REAL,
        changed_date TEXT
    )""")
    # -------------------------------------------------------------------------------

    # NEW: Whether Bulk Entry auto-fill carries a pattern over from the
    # previous month, or starts fresh -- decided once per month, on request.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Prediction_Month_Settings (
        month TEXT PRIMARY KEY,
        carry_over INTEGER
    )""")
    # -------------------------------------------------------------------------------

    # NEW: One-time migration tracker, so ordering/data fixes below only
    # ever run once and don't silently overwrite later manual changes.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Schema_Migrations (
        migration_id TEXT PRIMARY KEY
    )""")
    # -------------------------------------------------------------------------------

    # NEW: Explicit display order for menu items across Bulk Entry, Single
    # Entry, and item lists -- rather than relying on insertion order.
    try:
        cursor.execute("ALTER TABLE Items_Master ADD COLUMN display_order INTEGER DEFAULT 999")
    except:
        pass

    # One-time ordering: Tea, Breakfast, Egg, Lunch, Omelette, Special,
    # Snacks, Pazhampori. Adds Special/Pazhampori if they don't exist yet.
    cursor.execute("SELECT 1 FROM Schema_Migrations WHERE migration_id = 'item_order_v1'")
    if not cursor.fetchone():
        REQUESTED_ORDER = ["Tea", "Breakfast", "Egg", "Lunch", "Omelette", "Special", "Snacks", "Pazhampori"]
        for i, name in enumerate(REQUESTED_ORDER):
            cursor.execute("SELECT item_id FROM Items_Master WHERE item_name = ?", (name,))
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE Items_Master SET display_order = ? WHERE item_id = ?", (i, row[0]))
            else:
                # New item (e.g. Special, Pazhampori) -- added active (per
                # the requested order) with a placeholder ₹0 price; the
                # manager needs to set the real price in Settings.
                cursor.execute(
                    "INSERT INTO Items_Master (item_name, current_price, is_active, display_order) VALUES (?, 0, 1, ?)",
                    (name, i)
                )
        cursor.execute("INSERT INTO Schema_Migrations (migration_id) VALUES ('item_order_v1')")
    # -------------------------------------------------------------------------------

    # NEW: Cleanup for accounts where "Special" already existed under a
    # slightly different name (e.g. "Specials") before the ordering
    # migration ran -- that created a duplicate. Only touches the
    # auto-created one if it's still untouched (₹0, never logged), and
    # merges the requested order onto the real pre-existing item instead.
    cursor.execute("SELECT 1 FROM Schema_Migrations WHERE migration_id = 'special_dup_cleanup_v1'")
    if not cursor.fetchone():
        cursor.execute("SELECT item_id, current_price FROM Items_Master WHERE item_name = 'Special'")
        special_row = cursor.fetchone()
        if special_row:
            special_id, special_price = special_row
            cursor.execute("SELECT COUNT(*) FROM Meal_Log WHERE item_name = 'Special'")
            special_usage = cursor.fetchone()[0]
            if special_price == 0 and special_usage == 0:
                cursor.execute("""
                    SELECT item_id FROM Items_Master 
                    WHERE item_id != ? AND item_name LIKE '%special%' COLLATE NOCASE
                """, (special_id,))
                original = cursor.fetchone()
                if original:
                    original_id = original[0]
                    cursor.execute("UPDATE Items_Master SET is_active = 0 WHERE item_id = ?", (special_id,))
                    cursor.execute("UPDATE Items_Master SET display_order = 5 WHERE item_id = ?", (original_id,))
        cursor.execute("INSERT INTO Schema_Migrations (migration_id) VALUES ('special_dup_cleanup_v1')")
    # -------------------------------------------------------------------------------

    # NEW: Fix items stuck with a shared/tied display_order (this happened
    # whenever a new item was added, since it always fell back to the same
    # default value -- swapping two equal numbers via the reorder buttons
    # visibly does nothing). Renumbers every active item sequentially based
    # on its CURRENT position, so nothing visibly moves, but every value
    # becomes unique and the reorder buttons start working for everyone.
    cursor.execute("SELECT 1 FROM Schema_Migrations WHERE migration_id = 'item_order_dedup_v1'")
    if not cursor.fetchone():
        cursor.execute("SELECT item_id FROM Items_Master WHERE is_active = 1 ORDER BY display_order ASC, item_id ASC")
        for new_order, (item_id,) in enumerate(cursor.fetchall()):
            cursor.execute("UPDATE Items_Master SET display_order = ? WHERE item_id = ?", (new_order, item_id))
        cursor.execute("INSERT INTO Schema_Migrations (migration_id) VALUES ('item_order_dedup_v1')")
    # -------------------------------------------------------------------------------

    # NEW: Manager's Fund Ledger -- the canteen manager's OWN personal
    # running account of money she's fronted or reclaimed when the canteen
    # runs short. Completely isolated: never read by, or written to, any
    # Dashboard total, Purchases, or Payments calculation. It exists purely
    # for her own reference.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Manager_Fund_Settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        opening_balance REAL DEFAULT 0,
        opening_date TEXT
    )""")
    cursor.execute("INSERT OR IGNORE INTO Manager_Fund_Settings (id, opening_balance, opening_date) VALUES (1, 0, NULL)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS Manager_Fund_Ledger (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT,
        amount REAL,
        remarks TEXT
    )""")
    # -------------------------------------------------------------------------------

    # NEW: Reusable Vendor and Item Description lists for the Expense
    # Tracker -- auto-populated as new values are typed (never need manual
    # pre-registration), with an option to manage/archive entries.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Vendors (
        vendor_id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor_name TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS Purchase_Descriptions (
        description_id INTEGER PRIMARY KEY AUTOINCREMENT,
        description_text TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1
    )""")
    # One-time backfill: pull in every distinct vendor/description already
    # sitting in past Purchases rows, so history isn't lost.
    cursor.execute("SELECT 1 FROM Schema_Migrations WHERE migration_id = 'vendor_desc_backfill_v1'")
    if not cursor.fetchone():
        cursor.execute("SELECT DISTINCT vendor FROM Purchases_Raw WHERE vendor IS NOT NULL AND vendor != ''")
        for (v,) in cursor.fetchall():
            cursor.execute("INSERT OR IGNORE INTO Vendors (vendor_name) VALUES (?)", (v,))
        cursor.execute("SELECT DISTINCT description FROM Purchases_Raw WHERE description IS NOT NULL AND description != ''")
        for (d,) in cursor.fetchall():
            cursor.execute("INSERT OR IGNORE INTO Purchase_Descriptions (description_text) VALUES (?)", (d,))
        cursor.execute("INSERT INTO Schema_Migrations (migration_id) VALUES ('vendor_desc_backfill_v1')")
    # -------------------------------------------------------------------------------

    # NEW: Consumption Module -- tracks quantity + amount purchased per raw
    # item (distinct from the free-text Expense Tracker), so average rates
    # and consumption trends can be computed per item.
    cursor.execute("""CREATE TABLE IF NOT EXISTS Consumption_Items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT UNIQUE NOT NULL,
        unit TEXT DEFAULT 'kg',
        is_active INTEGER DEFAULT 1
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS Consumption_Log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        quantity REAL,
        amount REAL,
        purchase_date TEXT,
        notes TEXT
    )""")
    cursor.execute("SELECT 1 FROM Schema_Migrations WHERE migration_id = 'consumption_seed_v1'")
    if not cursor.fetchone():
        starter_items = [
            ("Rice", "kg"), ("Cooking Oil", "liter"), ("Sugar", "kg"),
            ("Tea Powder", "kg"), ("Milk", "liter"), ("Gas Cylinder", "cylinder"),
            ("Vegetables", "kg"),
        ]
        cursor.executemany("INSERT OR IGNORE INTO Consumption_Items (item_name, unit) VALUES (?, ?)", starter_items)
        cursor.execute("INSERT INTO Schema_Migrations (migration_id) VALUES ('consumption_seed_v1')")
    # -------------------------------------------------------------------------------

    # Recycle Bin setup runs AGAIN here so a brand-new database (which just
    # had Payments/Purchases/Meal_Log created fresh above) also gets
    # converted to the raw-table-plus-view pattern immediately.
    ensure_recycle_bin(cursor)

    for table, columns in (("Meal_Log_Raw", "employee_id, log_date"), ("Payments_Raw", "employee_id, payment_date"), ("Purchases_Raw", "purchase_date"), ("Staff_Loans_Raw", "linked_payment_id")):
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_v3_{table} ON {table} ({columns})")

    from features import install_schema
    install_schema(cursor)

    # Auto-purge: permanently remove anything that's been in the Recycle
    # Bin for more than 7 days.
    purge_old_deleted_items(cursor)

    conn.commit() # Moved this outside the if block so upgrades get saved!
    conn.close()

# --- DASHBOARD (V3.1 with Dynamic Month & Bar Charts) ---
@app.route("/")
def dashboard():
    # 1. Listen for the month selector (default to current month)
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m')

    conn = connect_db()
    cursor = conn.cursor()
    
    # 2. Global Available Funds (Always all-time)
    cursor.execute("SELECT canteen_opening_balance FROM Global_Settings WHERE id = 1")
    base_bal = cursor.fetchone()[0] or 0.0
    cursor.execute("SELECT SUM(amount) FROM Payments")
    hist_in = cursor.fetchone()[0] or 0.0
    cursor.execute("SELECT SUM(amount) FROM Purchases")
    hist_out = cursor.fetchone()[0] or 0.0
    
    # NEW: Include Misc Income so the Master Ledger is perfectly balanced
    try:
        cursor.execute("SELECT SUM(amount) FROM Misc_Income")
        misc_in = cursor.fetchone()[0] or 0.0
    except:
        misc_in = 0.0
        
    global_balance = base_bal + hist_in + misc_in - hist_out
    

    # 3. Dynamic Month's Inflow & Expenses
    cursor.execute("SELECT SUM(amount) FROM Payments WHERE payment_date LIKE ?", (f"{target_month}%",))
    month_inflow = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT SUM(amount) FROM Purchases WHERE purchase_date LIKE ?", (f"{target_month}%",))
    expenses = cursor.fetchone()[0] or 0.0

    # 3b. Opening & Closing Balance for the selected month (same math as the
    # Balance Sheet PDF, so the dashboard number always matches the download)
    start_of_month = f"{target_month}-01"

    cursor.execute("SELECT SUM(amount) FROM Payments WHERE payment_date < ?", (start_of_month,))
    hist_paid_before = cursor.fetchone()[0] or 0.0

    try:
        cursor.execute("SELECT SUM(amount) FROM Misc_Income WHERE income_date < ?", (start_of_month,))
        hist_misc_before = cursor.fetchone()[0] or 0.0
    except:
        hist_misc_before = 0.0

    cursor.execute("SELECT SUM(amount) FROM Purchases WHERE purchase_date < ?", (start_of_month,))
    hist_out_before = cursor.fetchone()[0] or 0.0

    month_opening_balance = base_bal + hist_paid_before + hist_misc_before - hist_out_before

    try:
        cursor.execute("SELECT SUM(amount) FROM Misc_Income WHERE income_date LIKE ?", (f"{target_month}%",))
        month_misc_in = cursor.fetchone()[0] or 0.0
    except:
        month_misc_in = 0.0

    month_closing_balance = month_opening_balance + month_inflow + month_misc_in - expenses

    # 3c. NEW: This month vs last month comparison
    target_year, target_mon = int(target_month[:4]), int(target_month[5:7])
    if target_mon == 1:
        prev_month = f"{target_year - 1}-12"
    else:
        prev_month = f"{target_year}-{target_mon - 1:02d}"

    cursor.execute("SELECT SUM(amount) FROM Payments WHERE payment_date LIKE ?", (f"{prev_month}%",))
    prev_month_inflow = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(amount) FROM Purchases WHERE purchase_date LIKE ?", (f"{prev_month}%",))
    prev_month_expenses = cursor.fetchone()[0] or 0.0

    current_net = month_inflow - expenses
    prev_net = prev_month_inflow - prev_month_expenses

    def pct_change(current, previous):
        if previous == 0:
            return None  # nothing to compare against
        return ((current - previous) / abs(previous)) * 100

    month_comparison = {
        "prev_month_name": datetime.datetime.strptime(prev_month, '%Y-%m').strftime('%B %Y'),
        "collections": month_inflow, "prev_collections": prev_month_inflow,
        "collections_delta": month_inflow - prev_month_inflow,
        "collections_pct": pct_change(month_inflow, prev_month_inflow),
        "expenses": expenses, "prev_expenses": prev_month_expenses,
        "expenses_delta": expenses - prev_month_expenses,
        "expenses_pct": pct_change(expenses, prev_month_expenses),
        "net": current_net, "prev_net": prev_net,
        "net_delta": current_net - prev_net,
        "net_pct": pct_change(current_net, prev_net),
    }

    # 4. Profit & Loss Chart Data (Last 6 Active Months)
    cursor.execute("SELECT strftime('%Y-%m', payment_date) as m, SUM(amount) FROM Payments GROUP BY m ORDER BY m DESC LIMIT 6")
    inflow_data = dict(cursor.fetchall())
    
    cursor.execute("SELECT strftime('%Y-%m', purchase_date) as m, SUM(amount) FROM Purchases GROUP BY m ORDER BY m DESC LIMIT 6")
    outflow_data = dict(cursor.fetchall())
    
    all_months = sorted(list(set(list(inflow_data.keys()) + list(outflow_data.keys()))))[-6:]
    labels_pl = all_months
    data_in = [inflow_data.get(m, 0) for m in all_months]
    data_out = [outflow_data.get(m, 0) for m in all_months]

    # 5. Consumption Trends (Bar Chart - Target Month)
    cursor.execute("SELECT item_name, SUM(quantity), SUM(quantity * locked_price) FROM Meal_Log WHERE log_date LIKE ? GROUP BY item_name", (f"{target_month}%",))
    consumption_raw = cursor.fetchall()
    labels_items = [row[0] for row in consumption_raw]
    data_items = [row[1] for row in consumption_raw]
    data_items_amount = [row[2] for row in consumption_raw]

    # 6. Expenses by Category (Bar Chart - Target Month)
    cursor.execute("SELECT item_category, SUM(amount) FROM Purchases WHERE purchase_date LIKE ? GROUP BY item_category", (f"{target_month}%",))
    category_raw = cursor.fetchall()
    labels_categories = [row[0] for row in category_raw]
    data_categories = [row[1] for row in category_raw]

    # Itemised split-up per category for the hover tooltip on the chart
    category_details = {c: [] for c in labels_categories}
    cursor.execute("""SELECT item_category, purchase_date, description, vendor, amount
                      FROM Purchases WHERE purchase_date LIKE ? ORDER BY purchase_date, purchase_id""", (f"{target_month}%",))
    for cat, pdate, desc, vendor, amt in cursor.fetchall():
        try:
            pdate = datetime.datetime.strptime(pdate, '%Y-%m-%d').strftime('%d-%m-%Y')
        except (ValueError, TypeError):
            pass
        category_details.setdefault(cat, []).append([pdate, desc or '', vendor or '', amt or 0])

    # 7. Generate dropdown options based on database history
    cursor.execute("""
        SELECT DISTINCT strftime('%Y-%m', payment_date) FROM Payments
        UNION SELECT DISTINCT strftime('%Y-%m', purchase_date) FROM Purchases
        UNION SELECT DISTINCT strftime('%Y-%m', log_date) FROM Meal_Log
        UNION SELECT ? ORDER BY 1 DESC
    """, (target_month,))
    available_months = [row[0] for row in cursor.fetchall() if row[0]]
    available_months_display = [
        {"value": m, "label": datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y')}
        for m in available_months
    ]
    
   # 8. Outstanding Dues Leaderboard -- reflects the PREVIOUS month's cycle
    # relative to whichever month is selected, not a constantly-growing
    # live total. The bill for month M only finalizes at month-end and
    # people pay it during M+1, so while looking at September, "who's
    # pending" should mean August's bill (netted against whatever's been
    # paid toward it since, up to now) -- not September's still-accumulating,
    # not-yet-billed consumption. Payments are NOT date-capped, so someone
    # paying off their August debt in September correctly drops off here
    # immediately, same as it always has.
    prev_month_cutoff = start_of_month  # everything billed BEFORE this month started
    cursor.execute("""
        SELECT
            e.employee_id,
            e.name,
            e.department,
            e.opening_balance
                + (SELECT COALESCE(SUM(quantity * locked_price), 0)
                   FROM Meal_Log WHERE employee_id = e.employee_id AND log_date < ?)
                - (SELECT COALESCE(SUM(amount), 0)
                   FROM Payments WHERE employee_id = e.employee_id) AS net_due
        FROM Employees e
        WHERE e.is_active = 1
        ORDER BY net_due DESC
    """, (prev_month_cutoff,))
    all_balances = cursor.fetchall()

    top_debtors = [
        {"id": row[0], "name": row[1], "department": row[2] or "—", "due": row[3]}
        for row in all_balances
        if row[3] > 0.05
    ][:5]

    # NEW: Outstanding staff loans (cash owed BACK to staff, not bill dues)
    cursor.execute("""
        SELECT sl.amount, e.name
        FROM Staff_Loans sl JOIN Employees e ON sl.employee_id = e.employee_id
        WHERE sl.status = 'outstanding'
        ORDER BY sl.loan_date ASC
    """)
    outstanding_loan_rows = cursor.fetchall()
    total_outstanding_loans = sum(r[0] for r in outstanding_loan_rows)
    outstanding_loan_count = len(outstanding_loan_rows)
    
    # A. Collection Recovery Rate -- same "previous cycle" logic. The bill
    # for the month BEFORE whichever month is selected is a fixed, closed
    # number; recovery tracks how much of THAT bill came in during the
    # selected month specifically -- the natural one-month payment window
    # for it. This is deliberately bounded to just that one month (not
    # open-ended through today) so it always matches the "Collection
    # Inflow" figure shown elsewhere for that same month, rather than
    # silently including later months' payments too and blowing past 100%.
    y, m = int(target_month[:4]), int(target_month[5:7])
    prev_month_start = f"{y-1:04d}-12-01" if m == 1 else f"{y:04d}-{m-1:02d}-01"
    cursor.execute("SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE log_date >= ? AND log_date < ?",
                   (prev_month_start, start_of_month))
    prev_month_billed = cursor.fetchone()[0] or 0.0
    collected_toward_prev_month = month_inflow  # same one-month window as Collection Inflow
    recovery_rate = int((collected_toward_prev_month / prev_month_billed * 100)) if prev_month_billed > 0 else 100
    prev_month_label = datetime.datetime.strptime(prev_month_start, '%Y-%m-%d').strftime('%B %Y')
   
# B. Weekly Grocery Tracking (work-weeks: Monday-Friday only)
    #
    # Week boundaries:
    #  - If the month opens Mon-Fri, Week 1 runs from day 1 to that
    #    calendar week's Friday (may be a short week, e.g. Tue-Fri).
    #  - If the month opens Sat/Sun, those leading days are DROPPED
    #    (not counted in any week) and Week 1 starts on the first Monday.
    #  - Every week after that is a full Monday-Friday span.
    #  - A Sat/Sun that falls BETWEEN two weeks is folded into the week
    #    that just ended (not the one about to start).
    #  - Week 5 (and its whole column in the UI) only appears if the
    #    month actually has a 5th Mon-Fri span.
    cursor.execute("""
        SELECT purchase_date, amount 
        FROM Purchases 
        WHERE item_category NOT LIKE '%Gas%' 
          AND item_category NOT LIKE '%Fuel%' 
          AND item_category NOT LIKE '%Clean%'
          AND item_category NOT LIKE '%Misc%'
          AND purchase_date LIKE ?
    """, (f"{target_month}%",))
    groceries = cursor.fetchall()

    import calendar
    year, month = int(target_month.split('-')[0]), int(target_month.split('-')[1])
    days_in_month = calendar.monthrange(year, month)[1]
    first_weekday = datetime.date(year, month, 1).weekday()  # 0=Mon ... 6=Sun

    week_spans = []  # each entry is (start_day, end_day) of that week's Mon-Fri span
    if first_weekday <= 4:
        week_start = 1
        week_end = min(1 + (4 - first_weekday), days_in_month)
    else:
        days_until_monday = (0 - first_weekday) % 7  # Sat->2, Sun->1
        week_start = 1 + days_until_monday
        week_end = min(week_start + 4, days_in_month)
    while week_start <= days_in_month:
        week_spans.append((week_start, week_end))
        week_start = week_end + 3  # Friday -> next Monday
        week_end = min(week_start + 4, days_in_month)

    week_spans = week_spans[:5]  # dashboard only has 5 slots
    week5_applicable = len(week_spans) >= 5

    # Extend each week's span to swallow the weekend right after it (so a
    # Sat/Sun purchase counts toward the week that just ended); the last
    # week also absorbs any trailing days through month-end.
    bucket_ranges = []
    for i, (start, end) in enumerate(week_spans):
        bucket_end = (week_spans[i + 1][0] - 1) if i + 1 < len(week_spans) else days_in_month
        bucket_ranges.append((start, bucket_end))

    week_totals = [0.0, 0.0, 0.0, 0.0, 0.0]
    first_week_start = week_spans[0][0] if week_spans else 1
    for p_date, amt in groceries:
        day = int(p_date.split('-')[2])
        if day < first_week_start:
            continue  # leading Sat/Sun before Week 1 starts -- dropped
        for i, (b_start, b_end) in enumerate(bucket_ranges):
            if b_start <= day <= b_end:
                week_totals[i] += amt
                break

    total_grocery_spent = sum(week_totals)
    avg_weekly_grocery = (total_grocery_spent / days_in_month) * 7 if days_in_month > 0 else 0
    conn.close()
    
    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')
    
    # NEW: Categories available for the "last 6 months trend" chart selector
    conn2 = connect_db()
    cursor2 = conn2.cursor()
    cursor2.execute("""
        SELECT DISTINCT item_category FROM Purchases
        WHERE item_category IS NOT NULL AND item_category != ''
        ORDER BY item_category ASC
    """)
    expense_trend_categories = [r[0] for r in cursor2.fetchall()]
    conn2.close()
    
    # Collection Inflow card: headline = everything that came in this month,
    # with the two sources broken out underneath. NOTE: `month_inflow` itself
    # is deliberately left as staff-bill payments ONLY -- the recovery rate,
    # month-vs-month comparison and closing-balance maths all rely on that
    # (closing balance adds Misc. Income separately, so folding it into
    # month_inflow would double-count it).
    month_staff_payments = month_inflow
    month_misc_income = month_misc_in
    month_total_inflow = month_staff_payments + month_misc_income

    return render_template("index.html",
                           funds=global_balance, month_inflow=month_inflow, expenses=expenses,
                           month_staff_payments=month_staff_payments, month_misc_income=month_misc_income,
                           month_total_inflow=month_total_inflow,
                           month_opening_balance=month_opening_balance, month_closing_balance=month_closing_balance,
                           month_comparison=month_comparison,
                           target_month=target_month, display_month_name=display_month_name, available_months=available_months,
                           available_months_display=available_months_display,
                           labels_pl=labels_pl, data_in=data_in, data_out=data_out,
                           labels_items=labels_items, data_items=data_items, data_items_amount=data_items_amount,
                           labels_categories=labels_categories, category_details=category_details, data_categories=data_categories,
                           expense_trend_categories=expense_trend_categories,
                           top_debtors=top_debtors,
                           total_outstanding_loans=total_outstanding_loans, outstanding_loan_count=outstanding_loan_count,
                           recovery_rate=recovery_rate, prev_month_billed=prev_month_billed,
                           collected_toward_prev_month=collected_toward_prev_month, prev_month_label=prev_month_label, 
                           week_totals=week_totals, avg_weekly_grocery=avg_weekly_grocery,
                           week5_applicable=week5_applicable)
    # -----------------------------------
# --- GLOBAL LEADERSHIP VARIABLES ---
@app.context_processor
def inject_leadership():
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT canteen_manager, committee_members FROM Global_Settings WHERE id = 1")
        row = cursor.fetchone()
        manager = row[0] if row and row[0] else "Aleena Jacob"
        committee = row[1] if row and row[1] else "Sabin P S, Abhilash, Sreelekshmi, Lekshmi"
    except Exception:
        manager = "Aleena Jacob"
        committee = "Sabin P S, Abhilash, Sreelekshmi, Lekshmi"
    finally:
        conn.close()
    return dict(canteen_manager=manager, committee_members=committee)

@app.route("/update-leadership", methods=["POST"])
def update_leadership():
    manager = request.form.get("manager_name")
    committee = request.form.get("committee_members")
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE Global_Settings SET canteen_manager = ?, committee_members = ? WHERE id = 1", (manager, committee))
    conn.commit()
    conn.close()
    return redirect(url_for('settings'))
@app.route("/api/expense-trend")
def expense_trend():
    category = request.args.get('category')
    if not category:
        return jsonify({"labels": [], "amounts": []})

    # Always rolling from the CURRENT calendar month, independent of
    # whatever month the dashboard itself is currently viewing.
    today = datetime.date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()  # oldest to newest, left to right on the chart

    conn = connect_db()
    cursor = conn.cursor()
    amounts = []
    for month_str in months:
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM Purchases
            WHERE item_category = ? AND purchase_date LIKE ?
        """, (category, f"{month_str}%"))
        amounts.append(cursor.fetchone()[0])
    conn.close()

    labels = [datetime.datetime.strptime(m, '%Y-%m').strftime('%b %Y') for m in months]
    return jsonify({"labels": labels, "amounts": amounts})

@app.route("/bulk-log", methods=["GET", "POST"])
def bulk_log():
    success = request.args.get("success") == "1"
    conn = connect_db()
    cursor = conn.cursor()
    if request.method == "POST":
        log_date = request.form.get("log_date")
        cursor.execute("SELECT item_name, current_price FROM Items_Master")
        prices = dict(cursor.fetchall())

        # Fetch whatever's already saved for this date, so re-saving (e.g.
        # correcting a mistake, or resuming after a partial save) REPLACES
        # each slot instead of silently creating a duplicate billed entry.
        cursor.execute("SELECT log_id, employee_id, item_name, locked_price FROM Meal_Log WHERE log_date = ?", (log_date,))
        existing = {}
        for log_id, emp_id, item_name, price in cursor.fetchall():
            existing.setdefault((str(emp_id), item_name), []).append((log_id, price))
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for key, value in request.form.items():
            if key.startswith("qty_"):
                parts = key.split("_", 2)
                if len(parts) >= 3:
                    emp_id = parts[1]
                    item_name = parts[2]
                    slot = (emp_id, item_name)
                    if slot in existing:
                        # Replace: soft-delete the old entry for this exact
                        # employee+item+date before writing the new one.
                        previous = existing[slot]
                        if len(previous) != 1:
                            conn.close()
                            abort(409, description="Multiple meal entries exist for this slot. Correct them in History first.")
                        if value and int(value) > 0:
                            cursor.execute("UPDATE Meal_Log_Raw SET quantity = ? WHERE log_id = ?", (int(value), previous[0][0]))
                        else:
                            cursor.execute("UPDATE Meal_Log_Raw SET is_deleted = 1, deleted_at = ? WHERE log_id = ?", (now_str, previous[0][0]))
                        continue
                    if value and int(value) > 0 and item_name in prices:
                        cursor.execute("INSERT INTO Meal_Log_Raw (employee_id, item_name, quantity, locked_price, log_date) VALUES (?, ?, ?, ?, ?)",
                                       (emp_id, item_name, int(value), prices[item_name], log_date))
        conn.commit()
        success = True
    cursor.execute("SELECT * FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()
    cursor.execute("SELECT * FROM Items_Master WHERE is_active = 1 ORDER BY display_order ASC, item_id ASC")
    items = cursor.fetchall()
    conn.close()
    return render_template("bulk_log.html", employees=employees, items=items, success=success)

@app.route("/api/bulk-log-existing")
def bulk_log_existing():
    """Whatever's already saved for a given date, so the Bulk Entry grid can
    show it (and highlight it) when the manager revisits a date they were
    partway through -- instead of it silently looking blank again."""
    date = request.args.get('date')
    if not date:
        return jsonify({})
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT employee_id, item_name, quantity, locked_price FROM Meal_Log WHERE log_date = ?", (date,))
    rows = cursor.fetchall()
    conn.close()
    existing = {}
    for emp_id, item_name, qty, price in rows:
        existing.setdefault(str(emp_id), {})[item_name] = {"quantity": qty, "price": price} if request.args.get("include_prices") == "1" else qty
    return jsonify(existing)

@app.route("/log-meals", methods=["GET", "POST"])
def log_meals():
    success = request.args.get("success") == "1"
    conn = connect_db()
    cursor = conn.cursor()
    if request.method == "POST":
        log_date = request.form.get("log_date")
        emp_id = request.form.get("employee_id")
        cursor.execute("SELECT item_name, current_price FROM Items_Master")
        prices = dict(cursor.fetchall())
        for item_name in prices.keys():
            qty = request.form.get(f"qty_{item_name}")
            if qty and int(qty) > 0:
                cursor.execute("INSERT INTO Meal_Log_Raw (employee_id, item_name, quantity, locked_price, log_date) VALUES (?, ?, ?, ?, ?)",
                               (emp_id, item_name, int(qty), prices[item_name], log_date))
        conn.commit()
        success = True
    cursor.execute("SELECT * FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()
    cursor.execute("SELECT * FROM Items_Master WHERE is_active = 1 ORDER BY display_order ASC, item_id ASC")
    items = cursor.fetchall()
    conn.close()
    return render_template("log_meals.html", employees=employees, items=items, success=success)

@app.route("/history", methods=["GET", "POST"])
def history():
    conn = connect_db()
    cursor = conn.cursor()
    
    # 1. Handle Month Selection (Defaults to current month)
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m')
    
    if request.method == "POST":
        # Handle Single Delete (soft delete -> Recycle Bin)
        delete_id = request.form.get("delete_id")
        if delete_id:
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("UPDATE Meal_Log_Raw SET is_deleted = 1, deleted_at = ? WHERE log_id = ?", (now_str, delete_id))
            
        # Handle Bulk Delete (soft delete -> Recycle Bin)
        delete_ids = request.form.getlist("delete_ids")
        if delete_ids:
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.executemany("UPDATE Meal_Log_Raw SET is_deleted = 1, deleted_at = ? WHERE log_id = ?", [(now_str, i) for i in delete_ids])
            
        conn.commit()
        # Redirect back to the SAME month you were just looking at
        return redirect(url_for('history', month=target_month))
        
    # 2. Fetch all records FOR THIS SPECIFIC MONTH ONLY
    cursor.execute("""
        SELECT m.log_id, m.log_date, e.name, m.item_name, m.quantity, m.locked_price, (m.quantity * m.locked_price) as total
        FROM Meal_Log m JOIN Employees e ON m.employee_id = e.employee_id
        WHERE m.log_date LIKE ?
        ORDER BY m.log_date DESC, m.log_id DESC
    """, (f"{target_month}%",))
    logs = []
    for row in cursor.fetchall():
        row = list(row)
        # Keep the raw ISO date at index 1 (used by the edit modal's date
        # input and the date-based bulk-select), and append a DD MM YYYY
        # display version at the end for the table to show.
        try:
            display_date = datetime.datetime.strptime(row[1], '%Y-%m-%d').strftime('%d-%m-%Y')
        except (ValueError, TypeError):
            display_date = row[1]
        row.append(display_date)
        logs.append(row)
    
    # 3. Get available months for the dropdown
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', log_date) FROM Meal_Log UNION SELECT ? ORDER BY 1 DESC", (target_month,))
    available_months = [row[0] for row in cursor.fetchall() if row[0]]
    # 4. Monthly Verification Summary -- built DYNAMICALLY from whatever
    # items were actually logged this month, instead of a hardcoded list.
    # A hardcoded list silently drops any newer item (e.g. a renamed Tea
    # variant, or a brand new item like Pazhampori) from this summary even
    # though it's still counted correctly in the grand total.
    cursor.execute("SELECT DISTINCT item_name FROM Meal_Log WHERE log_date LIKE ?", (f"{target_month}%",))
    used_items = [r[0] for r in cursor.fetchall()]

    cursor.execute("SELECT item_name, display_order FROM Items_Master")
    item_order_map = dict(cursor.fetchall())
    # Order columns to match the rest of the app (Settings/Bulk Entry order);
    # anything no longer in Items_Master at all falls back to alphabetical.
    summary_items = sorted(used_items, key=lambda name: (item_order_map.get(name, 9999), name))

    cursor.execute("""
        SELECT e.name, m.item_name, SUM(m.quantity) as qty, SUM(m.quantity * m.locked_price) as amt
        FROM Employees e JOIN Meal_Log m ON e.employee_id = m.employee_id
        WHERE m.log_date LIKE ?
        GROUP BY e.employee_id, m.item_name
        ORDER BY e.name ASC
    """, (f"{target_month}%",))

    summary_by_employee = {}
    for name, item_name, qty, amt in cursor.fetchall():
        rec = summary_by_employee.setdefault(name, {"name": name, "food_qty": {}, "food_amt": {}, "total": 0.0})
        rec["food_qty"][item_name] = qty
        rec["food_amt"][item_name] = (amt or 0)
        rec["total"] += (amt or 0)
    summary_records = list(summary_by_employee.values())

    # Working days = number of distinct dates that have at least one entry this month
    cursor.execute("SELECT COUNT(DISTINCT log_date) FROM Meal_Log WHERE log_date LIKE ?", (f"{target_month}%",))
    working_days = cursor.fetchone()[0] or 0

    summary_totals = {"food_qty": {item: 0 for item in summary_items}, "food_amt": {item: 0.0 for item in summary_items}, "total_amount": 0.0}
    for rec in summary_records:
        for item in summary_items:
            summary_totals["food_qty"][item] += rec["food_qty"].get(item, 0)
            summary_totals["food_amt"][item] += rec["food_amt"].get(item, 0)
        summary_totals["total_amount"] += rec["total"]
    conn.close()
    
    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')

    moved_count = request.args.get('moved', type=int)

    return render_template("history.html", logs=logs, summary_records=summary_records, summary_items=summary_items, summary_totals=summary_totals, working_days=working_days, target_month=target_month, display_month_name=display_month_name, available_months=available_months, moved_count=moved_count)
# --- OFFICE ROSTER PAGE ---
@app.route("/roster")
def roster():
    conn = connect_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()
    
    cursor.execute("SELECT * FROM Employees WHERE is_active = 0 ORDER BY name ASC")
    archived_employees = cursor.fetchall()
    
    conn.close() 
    return render_template("roster.html", employees=employees, archived_employees=archived_employees)
# --- INDIVIDUAL STAFF HISTORY API ---
# --- INDIVIDUAL STAFF HISTORY API ---
@app.route("/employee/<int:emp_id>")
def employee_360(emp_id):
    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("SELECT employee_id, name, department, is_active, opening_balance FROM Employees WHERE employee_id = ?", (emp_id,))
    emp = cursor.fetchone()
    if not emp:
        conn.close()
        return "Employee not found", 404

    # Lifetime balance -- same formula used everywhere else in the app
    cursor.execute("SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = ?", (emp_id,))
    total_billed = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = ?", (emp_id,))
    total_paid = cursor.fetchone()[0]
    current_due = emp[4] + total_billed - total_paid

    # Item consumption breakdown -- lifetime by default, but selectable to
    # a specific month via ?breakdown_period=YYYY-MM
    breakdown_period = request.args.get('breakdown_period', 'lifetime')

    if breakdown_period == 'lifetime':
        cursor.execute("""
            SELECT item_name, SUM(quantity) as qty, SUM(quantity * locked_price) as total
            FROM Meal_Log WHERE employee_id = ?
            GROUP BY item_name ORDER BY total DESC
        """, (emp_id,))
        breakdown_period_label = "Lifetime"
    else:
        cursor.execute("""
            SELECT item_name, SUM(quantity) as qty, SUM(quantity * locked_price) as total
            FROM Meal_Log WHERE employee_id = ? AND log_date LIKE ?
            GROUP BY item_name ORDER BY total DESC
        """, (emp_id, f"{breakdown_period}%"))
        breakdown_period_label = datetime.datetime.strptime(breakdown_period, '%Y-%m').strftime('%B %Y')
    item_breakdown = cursor.fetchall()
    max_item_total = max([row[2] for row in item_breakdown], default=0)
    breakdown_total_qty = sum(row[1] for row in item_breakdown)
    breakdown_total_amount = sum(row[2] for row in item_breakdown)

    # Months this employee has any activity in, for the breakdown selector
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', log_date) FROM Meal_Log WHERE employee_id = ? ORDER BY 1 DESC", (emp_id,))
    available_breakdown_months = [
        {"value": r[0], "label": datetime.datetime.strptime(r[0], '%Y-%m').strftime('%B %Y')}
        for r in cursor.fetchall() if r[0]
    ]

    # Active months + average monthly bill
    cursor.execute("SELECT COUNT(DISTINCT strftime('%Y-%m', log_date)) FROM Meal_Log WHERE employee_id = ?", (emp_id,))
    active_months = cursor.fetchone()[0] or 0
    avg_monthly_bill = (total_billed / active_months) if active_months > 0 else 0.0

    # Recent activity (meals + payments), most recent first
    cursor.execute("""
        SELECT log_id, log_date, item_name, quantity, locked_price
        FROM Meal_Log WHERE employee_id = ? ORDER BY log_date DESC, log_id DESC LIMIT 25
    """, (emp_id,))
    recent_meals = cursor.fetchall()

    cursor.execute("""
        SELECT payment_id, payment_date, amount
        FROM Payments WHERE employee_id = ? ORDER BY payment_date DESC, payment_id DESC LIMIT 25
    """, (emp_id,))
    recent_payments = cursor.fetchall()

    # Staff loan status for this person
    cursor.execute("""
        SELECT loan_id, amount, loan_date, repay_method, status, settled_date
        FROM Staff_Loans WHERE employee_id = ? ORDER BY loan_date DESC
    """, (emp_id,))
    loans = cursor.fetchall()
    outstanding_loan_total = sum(r[1] for r in loans if r[4] == 'outstanding')

    # Month-by-month billed vs paid (same data the quick-view modal uses)
    cursor.execute("""
        SELECT strftime('%Y-%m', log_date) as month, SUM(quantity * locked_price)
        FROM Meal_Log WHERE employee_id = ? GROUP BY month
    """, (emp_id,))
    monthly_billed = dict(cursor.fetchall())
    cursor.execute("""
        SELECT strftime('%Y-%m', payment_date) as month, SUM(amount)
        FROM Payments WHERE employee_id = ? GROUP BY month
    """, (emp_id,))
    monthly_paid = dict(cursor.fetchall())
    all_months = sorted(set(list(monthly_billed.keys()) + list(monthly_paid.keys())), reverse=True)
    monthly_history = [
        {
            "month_name": datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y'),
            "billed": monthly_billed.get(m, 0.0),
            "paid": monthly_paid.get(m, 0.0),
        }
        for m in all_months if m
    ]

    conn.close()
    # Dates shown as DD-MM-YYYY (totals above already used the originals)
    recent_payments = [(r[0], to_dmy(r[1]), r[2]) for r in recent_payments]
    loans = [(r[0], r[1], to_dmy(r[2]), r[3], r[4], to_dmy(r[5])) for r in loans]
    return render_template(
        "employee_360.html",
        emp_id=emp[0], emp_name=emp[1], emp_dept=emp[2] or "General", is_active=emp[3], opening_balance=emp[4],
        total_billed=total_billed, total_paid=total_paid, current_due=current_due,
        item_breakdown=item_breakdown, max_item_total=max_item_total,
        breakdown_total_qty=breakdown_total_qty, breakdown_total_amount=breakdown_total_amount,
        breakdown_period=breakdown_period, breakdown_period_label=breakdown_period_label,
        available_breakdown_months=available_breakdown_months,
        active_months=active_months, avg_monthly_bill=avg_monthly_bill,
        recent_meals=recent_meals, recent_payments=recent_payments,
        loans=loans, outstanding_loan_total=outstanding_loan_total,
        monthly_history=monthly_history,
    )

@app.route("/api/staff-history/<int:emp_id>")
def staff_history(emp_id):
    conn = connect_db()
    cursor = conn.cursor()
    
    # 1. Get basic info
    cursor.execute("SELECT name, department, opening_balance FROM Employees WHERE employee_id = ?", (emp_id,))
    emp = cursor.fetchone()
    if not emp:
        return jsonify({"error": "Employee not found"}), 404
        
    # 2. Calculate their absolute total due right now
    cursor.execute("SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = ?", (emp_id,))
    total_billed = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = ?", (emp_id,))
    total_paid = cursor.fetchone()[0]
    current_due = emp[2] + total_billed - total_paid
        
    # 3. Get monthly meal totals AND consumption item breakdown
    cursor.execute("""
        SELECT strftime('%Y-%m', log_date) as month, item_name, SUM(quantity), SUM(quantity * locked_price)
        FROM Meal_Log WHERE employee_id = ? GROUP BY month, item_name
    """, (emp_id,))
    
    meals_data = {}
    for row in cursor.fetchall():
        m, item_name, qty, cost = row
        if m not in meals_data:
            meals_data[m] = {"billed": 0.0, "details": []}
        meals_data[m]["billed"] += cost
        meals_data[m]["details"].append(f"{qty} {item_name}")
    
    # 4. Get monthly payment totals
    cursor.execute("""
        SELECT strftime('%Y-%m', payment_date) as month, SUM(amount) 
        FROM Payments WHERE employee_id = ? GROUP BY month ORDER BY month DESC
    """, (emp_id,))
    payments = dict(cursor.fetchall())
    
    # 5. Merge the months together perfectly
    all_months = sorted(list(set(list(meals_data.keys()) + list(payments.keys()))), reverse=True)
    
    history = []
    for m in all_months:
        if m:
            month_name = datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y')
            m_data = meals_data.get(m, {"billed": 0.0, "details": []})
            
            history.append({
                "month_name": month_name,
                "billed": m_data["billed"],
                "paid": payments.get(m, 0.0),
                "consumption": ", ".join(m_data["details"]) if m_data["details"] else ""
            })
            
    conn.close()
    return jsonify({
        "name": emp[0],
        "department": emp[1] or "General",
        "base_balance": emp[2],
        "current_due": current_due,
        "history": history
    })
# --- EDIT MEAL LOG ---
@app.route("/edit-log", methods=["POST"])
def edit_log():
    log_id = request.form.get("log_id")
    new_qty = request.form.get("new_qty")
    new_date = request.form.get("new_date")
    target_month = request.form.get("target_month")
    
    if log_id and new_qty and new_date:
        conn = connect_db()
        cursor = conn.cursor()
        # Update the quantity and the date for this specific record
        cursor.execute("UPDATE Meal_Log_Raw SET quantity = ?, log_date = ? WHERE log_id = ?", 
                       (int(new_qty), new_date, log_id))
        conn.commit()
        conn.close()
        
    # Send the user right back to the month they were looking at
    return redirect(url_for('history', month=target_month))

# --- NEW: Fix a bulk-entry date mistake in one shot ---
# (e.g. Bulk Month Entry accidentally submitted under August instead of July)
@app.route("/api/logs-on-date/<date>")
def logs_on_date(date):
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE log_date = ?", (date,))
    count, total = cursor.fetchone()
    conn.close()
    return jsonify({"count": count, "total": total})

@app.route("/move-log-date", methods=["POST"])
def move_log_date():
    old_date = request.form.get("old_date")
    new_date = request.form.get("new_date")
    target_month = request.form.get("target_month")

    moved_count = 0
    if old_date and new_date and old_date != new_date:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Meal_Log_Raw SET log_date = ? WHERE log_date = ? AND is_deleted = 0", (new_date, old_date))
        moved_count = cursor.rowcount
        conn.commit()
        conn.close()

    # Land the user on the month the corrected entries now belong to
    landing_month = new_date[:7] if new_date else target_month
    return redirect(url_for('history', month=landing_month, moved=moved_count))
# --- SETTINGS (V2 with Master Ledger Updates) ---
@app.route("/settings")
def settings():
    conn = connect_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()
    
    # NEW: Fetch archived employees
    cursor.execute("SELECT * FROM Employees WHERE is_active = 0 ORDER BY name ASC")
    archived_employees = cursor.fetchall()
    
    cursor.execute("SELECT * FROM Items_Master WHERE is_active = 1 ORDER BY display_order ASC, item_id ASC")
    items = cursor.fetchall()
    
    cursor.execute("SELECT * FROM Items_Master WHERE is_active = 0")
    archived_items = cursor.fetchall()
    
    cursor.execute("SELECT canteen_opening_balance FROM Global_Settings WHERE id = 1")
    global_balance = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT * FROM Expense_Categories WHERE is_active = 1 ORDER BY category_name ASC")
    categories = cursor.fetchall()
    
    # NEW: Archived categories, so they can be restored from Settings too
    cursor.execute("SELECT * FROM Expense_Categories WHERE is_active = 0 ORDER BY category_name ASC")
    archived_categories = cursor.fetchall()
    
    # NEW: Recent price changes across all items
    cursor.execute("SELECT item_name, old_price, new_price, changed_date FROM Price_History ORDER BY changed_date DESC, history_id DESC LIMIT 50")
    price_history = [(name, old_p, new_p, to_dmy(changed)) for name, old_p, new_p, changed in cursor.fetchall()]
    
    conn.close() 
    
    # NEW: Added archived_employees to the template
    return render_template("settings.html", employees=employees, archived_employees=archived_employees, items=items, archived_items=archived_items, global_balance=global_balance, categories=categories, archived_categories=archived_categories, price_history=price_history)
@app.route("/update-global-balance", methods=["POST"])
def update_global_balance():
    new_balance = request.form.get("canteen_opening_balance")
    if new_balance:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Global_Settings SET canteen_opening_balance = ? WHERE id = 1", (float(new_balance),))
        conn.commit()
        conn.close()
    return redirect(url_for('settings'))

@app.route("/add-employee", methods=["POST"])
def add_employee():
    name = request.form.get("name")
    dept = request.form.get("department")
    balance = request.form.get("opening_balance") or 0.0
    if name:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Employees (name, department, is_active, opening_balance) VALUES (?, ?, 1, ?)", 
                       (name, dept or "General", float(balance)))
        conn.commit()
        conn.close()
    return redirect(url_for('roster'))
@app.route("/edit-employee", methods=["POST"])
def edit_employee():
    emp_id = request.form.get("emp_id")
    new_name = request.form.get("new_name")
    new_dept = request.form.get("new_dept")
    new_balance = request.form.get("new_balance") # <-- This catches the data from the popup
    
    if emp_id and new_name:
        # Convert the balance to a decimal, default to 0 if left totally blank
        try:
            bal_val = float(new_balance) if new_balance else 0.0
        except ValueError:
            bal_val = 0.0
            
        conn = connect_db()
        cursor = conn.cursor()
        
        # Save the updated name, department, AND balance to the database
        cursor.execute("UPDATE Employees SET name = ?, department = ?, opening_balance = ? WHERE employee_id = ?", 
                       (new_name, new_dept, bal_val, emp_id))
        conn.commit()
        conn.close()
        
    return redirect(url_for('roster'))

@app.route("/hard-delete", methods=["POST"])
def hard_delete():
    emp_id = request.form.get("emp_id")
    if emp_id:
        conn = connect_db()
        cursor = conn.cursor()
        for table in ("Meal_Log_Raw", "Payments_Raw", "Staff_Loans_Raw"):
            if cursor.execute(f"SELECT 1 FROM {table} WHERE employee_id = ? LIMIT 1", (emp_id,)).fetchone():
                conn.close()
                abort(409, description="This employee has financial history. Deactivate them instead.")
        cursor.execute("DELETE FROM Employees WHERE employee_id = ?", (emp_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('roster'))

@app.route("/deactivate-employee", methods=["POST"])
def deactivate_employee():
    emp_id = request.form.get("emp_id")
    if emp_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Employees SET is_active = 0 WHERE employee_id = ?", (emp_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('roster'))
@app.route("/reactivate-employee", methods=["POST"])
def reactivate_employee():
    emp_id = request.form.get("emp_id")
    if emp_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Employees SET is_active = 1 WHERE employee_id = ?", (emp_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('roster'))

@app.route("/update-price", methods=["POST"])
def update_price():
    item_id = request.form.get("item_id")
    new_price = request.form.get("new_price")
    if item_id and new_price:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT item_name, current_price FROM Items_Master WHERE item_id = ?", (item_id,))
        row = cursor.fetchone()
        if row:
            item_name, old_price = row
            new_price_val = float(new_price)
            if abs(old_price - new_price_val) > 0.001:  # only log a genuine change
                today_str = datetime.date.today().strftime('%Y-%m-%d')
                cursor.execute("INSERT INTO Price_History (item_name, old_price, new_price, changed_date) VALUES (?, ?, ?, ?)",
                               (item_name, old_price, new_price_val, today_str))
            cursor.execute("UPDATE Items_Master SET current_price = ? WHERE item_id = ?", (new_price_val, item_id))
            conn.commit()
        conn.close()
    return redirect(url_for('settings'))
@app.route("/add-item", methods=["POST"])
def add_item():
    item_name = request.form.get("item_name")
    price = request.form.get("price")
    if item_name and price:
        conn = connect_db()
        cursor = conn.cursor()
        item_name = item_name.strip()
        try:
            # Give it its own place at the end of the order -- never leave
            # it at the shared default, or reordering ties silently do
            # nothing (swapping two equal numbers changes nothing).
            cursor.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM Items_Master WHERE is_active = 1")
            next_order = cursor.fetchone()[0]
            cursor.execute("INSERT INTO Items_Master (item_name, current_price, is_active, display_order) VALUES (?, ?, 1, ?)", (item_name, float(price), next_order))
            conn.commit()
        except sqlite3.IntegrityError:
            # If the database says the name already exists (even if it's archived),
            # we just automatically reactivate it and update to the new price!
            cursor.execute("SELECT current_price FROM Items_Master WHERE item_name = ?", (item_name,))
            old_row = cursor.fetchone()
            new_price_val = float(price)
            if old_row and abs(old_row[0] - new_price_val) > 0.001:
                today_str = datetime.date.today().strftime('%Y-%m-%d')
                cursor.execute("INSERT INTO Price_History (item_name, old_price, new_price, changed_date) VALUES (?, ?, ?, ?)",
                               (item_name, old_row[0], new_price_val, today_str))
            cursor.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM Items_Master WHERE is_active = 1")
            next_order = cursor.fetchone()[0]
            cursor.execute("UPDATE Items_Master SET is_active = 1, current_price = ?, display_order = ? WHERE item_name = ?", (new_price_val, next_order, item_name))
            conn.commit()
        conn.close()
    return redirect(url_for('settings'))

@app.route("/deactivate-item", methods=["POST"])
def deactivate_item():
    item_id = request.form.get("item_id")
    if item_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Items_Master SET is_active = 0 WHERE item_id = ?", (item_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('settings'))
@app.route("/reactivate-item", methods=["POST"])
def reactivate_item():
    item_id = request.form.get("item_id")
    if item_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Items_Master SET is_active = 1 WHERE item_id = ?", (item_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('settings'))

@app.route("/reorder-item", methods=["POST"])
def reorder_item():
    item_id = request.form.get("item_id")
    direction = request.form.get("direction")  # "up" or "down"
    if item_id and direction in ("up", "down"):
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT item_id, display_order FROM Items_Master WHERE is_active = 1 ORDER BY display_order ASC, item_id ASC")
        ordered = cursor.fetchall()
        ids = [r[0] for r in ordered]
        if item_id.isdigit() and int(item_id) in ids:
            idx = ids.index(int(item_id))
            swap_idx = idx - 1 if direction == "up" else idx + 1
            if 0 <= swap_idx < len(ordered):
                this_id, this_order = ordered[idx]
                swap_id, swap_order = ordered[swap_idx]
                cursor.execute("UPDATE Items_Master SET display_order = ? WHERE item_id = ?", (swap_order, this_id))
                cursor.execute("UPDATE Items_Master SET display_order = ? WHERE item_id = ?", (this_order, swap_id))
                conn.commit()
        conn.close()
    return redirect(url_for('settings'))

def to_dmy(date_str):
    """'2026-09-07' -> '07-09-2026' for display. Anything that isn't a clean
    YYYY-MM-DD date is returned untouched rather than raising."""
    try:
        return datetime.datetime.strptime(str(date_str)[:10], '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return date_str

def build_excel_with_dmy_dates(df, sheet_name, date_col='Date'):
    """Write a DataFrame to an in-memory .xlsx whose `date_col` shows DD-MM-YYYY.
    Dates are stored as REAL Excel dates (so sorting/filtering by date still
    works) and only DISPLAYED as DD-MM-YYYY. If any value isn't a clean
    YYYY-MM-DD date, that column falls back to DD-MM-YYYY text instead."""
    df = df.copy()
    if date_col in df.columns:
        parsed = pd.to_datetime(df[date_col], format='%Y-%m-%d', errors='coerce')
        if parsed.notna().all():
            df[date_col] = parsed
        else:
            df[date_col] = df[date_col].map(to_dmy)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        if date_col in df.columns:
            ws = writer.sheets[sheet_name]
            col_idx = list(df.columns).index(date_col) + 1
            # Set the display format on each cell ourselves: pandas' own
            # datetime_format option is ignored by some pandas versions.
            for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                for cell in row:
                    cell.number_format = 'DD-MM-YYYY'
            # Wide enough that Excel shows the date instead of "########"
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = 14
    output.seek(0)
    return output

def to_dmy_time(ts):
    """'2026-10-15 14:22:09' -> '15-10-2026 14:22:09' (same length as the original,
    so any template that slices the text keeps working). Falls back to the input."""
    try:
        return datetime.datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S').strftime('%d-%m-%Y %H:%M:%S')
    except (ValueError, TypeError):
        return ts

@app.route("/payments", methods=["GET", "POST"])
def payments():
    success = request.args.get("success") == "1"
    conn = connect_db()
    cursor = conn.cursor()
    if request.method == "POST":
        emp_id = request.form.get("employee_id")
        amount = request.form.get("amount")
        payment_date = request.form.get("payment_date")
        if emp_id and amount:
            cursor.execute("INSERT INTO Payments_Raw (employee_id, amount, payment_date) VALUES (?, ?, ?)",
                           (emp_id, float(amount), payment_date))
            conn.commit()
            success = True
            
    cursor.execute("SELECT * FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()

    # Pending amount per staff member, shown (info only) under the Staff Bill
    # Payment form once someone is picked. Same definition as the Net Due in the
    # Excel export / billing: opening balance + every meal ever - every payment ever.
    # Positive = still owes, 0 = settled, negative = paid in advance.
    cursor.execute("""
        SELECT e.employee_id,
               e.opening_balance
               + (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id)
               - (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id)
        FROM Employees e
        WHERE e.is_active = 1
    """)
    pending_by_emp = {str(emp_id): round(pending or 0, 2) for emp_id, pending in cursor.fetchall()}

    # NEW: Month filter for Staff Payments, defaulting to the current month
    filter_month = request.args.get('filter_month') or datetime.date.today().strftime('%Y-%m')
    cursor.execute("""
        SELECT p.payment_id, p.payment_date, e.name, p.amount 
        FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id 
        WHERE strftime('%Y-%m', p.payment_date) = ?
        ORDER BY p.payment_date DESC, p.payment_id DESC
    """, (filter_month,))
    # Dates shown as DD-MM-YYYY (sorting above already used the real dates)
    recent_payments = [(pid, to_dmy(pdate), name, amt) for pid, pdate, name, amt in cursor.fetchall()]

    # Other Income History follows the SAME month filter as Staff Payments
    # (it used to ignore it and always list the latest 50 entries overall).
    cursor.execute("""
        SELECT * FROM Misc_Income
        WHERE strftime('%Y-%m', income_date) = ?
        ORDER BY income_date DESC, income_id DESC
    """, (filter_month,))
    recent_misc = []
    for row in cursor.fetchall():
        row = list(row)
        row[3] = to_dmy(row[3])  # income_date -> DD-MM-YYYY for display
        recent_misc.append(row)

    # NEW: Distinct months that have payments on record, for the monthly Excel export dropdown
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', payment_date) FROM Payments ORDER BY 1 DESC")
    available_payment_months = [row[0] for row in cursor.fetchall() if row[0]]
    # Make sure the current month is always selectable even with zero payments yet
    if filter_month not in available_payment_months:
        available_payment_months = sorted(set(available_payment_months + [filter_month]), reverse=True)

    # The month dropdowns (filter + Monthly Reports) must ALSO offer months that
    # only have Misc. Income -- otherwise those entries could never be viewed now
    # that the list is filtered by month. The Excel-export list above stays
    # payments-only, since that export contains payments.
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', income_date) FROM Misc_Income")
    misc_months = [row[0] for row in cursor.fetchall() if row[0]]
    dropdown_months = sorted(set(available_payment_months + misc_months), reverse=True)
    available_payment_months_display = [
        {"value": m, "label": datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y')}
        for m in dropdown_months
    ]
    filter_month_display = datetime.datetime.strptime(filter_month, '%Y-%m').strftime('%B %Y')
    
    conn.close()
    bulk_success = request.args.get("bulk_success") == "1"

    # --- NEW: Monthly Reports (Who Paid / Who's Pending) ---
    report_type = request.args.get('report')  # 'paid' or 'pending'
    report_month = request.args.get('report_month') or datetime.date.today().strftime('%Y-%m')
    report_rows = None
    report_total = 0.0

    if report_type in ('paid', 'pending'):
        conn2 = connect_db()
        cursor2 = conn2.cursor()

        if report_type == 'paid':
            cursor2.execute("""
                SELECT e.name, e.department, SUM(p.amount) as total_paid
                FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id
                WHERE strftime('%Y-%m', p.payment_date) = ?
                GROUP BY p.employee_id
                ORDER BY total_paid DESC
            """, (report_month,))
            report_rows = cursor2.fetchall()
            report_total = sum(r[2] for r in report_rows)

        else:  # 'pending' -- who still owes from the PREVIOUS month's bill
            # (the one that's actually closed and due), not the currently
            # selected month's still-accumulating, not-yet-billed
            # consumption. Same logic as the Dashboard's Outstanding Dues
            # Leaderboard: billed is capped at the start of the selected
            # month (i.e. everything through the previous month's close),
            # while payments are NOT capped -- someone paying it off
            # immediately drops off the list, however recently they paid.
            month_start = f"{report_month}-01"
            cursor2.execute("""
                SELECT e.employee_id, e.name, e.department, e.opening_balance,
                    (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id AND log_date < ?) as billed,
                    (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id) as paid
                FROM Employees e
                WHERE e.is_active = 1
            """, (month_start,))
            all_rows = cursor2.fetchall()
            pending = []
            for emp_id, name, dept, opening, billed, paid in all_rows:
                due = opening + billed - paid
                if due > 0.005:
                    pending.append((name, dept, due))
            pending.sort(key=lambda r: r[2], reverse=True)
            report_rows = pending
            report_total = sum(r[2] for r in report_rows)

        conn2.close()

    report_month_display = datetime.datetime.strptime(report_month, '%Y-%m').strftime('%B %Y')
    ry, rm = int(report_month[:4]), int(report_month[5:7])
    prev_report_month_start = f"{ry-1:04d}-12-01" if rm == 1 else f"{ry:04d}-{rm-1:02d}-01"
    prev_report_month_display = datetime.datetime.strptime(prev_report_month_start, '%Y-%m-%d').strftime('%B %Y')

    return render_template("payments.html", employees=employees, recent_payments=recent_payments, recent_misc=recent_misc, success=success, bulk_success=bulk_success, available_payment_months=available_payment_months, available_payment_months_display=available_payment_months_display, filter_month=filter_month, filter_month_display=filter_month_display, pending_by_emp=pending_by_emp,
                           report_type=report_type, report_month=report_month, report_month_display=report_month_display, prev_report_month_display=prev_report_month_display, report_rows=report_rows, report_total=report_total)

# NEW: Route to log payments from multiple staff on the same date in one go
@app.route("/bulk-payments", methods=["POST"])
def bulk_payments():
    payment_date = request.form.get("payment_date")
    if payment_date:
        conn = connect_db()
        cursor = conn.cursor()
        rows_to_insert = []
        for key, value in request.form.items():
            if key.startswith("amt_") and value:
                try:
                    amount = float(value)
                except ValueError:
                    amount = 0
                if amount > 0:
                    emp_id = key.split("_", 1)[1]
                    rows_to_insert.append((emp_id, amount, payment_date))
        if rows_to_insert:
            cursor.executemany("INSERT INTO Payments_Raw (employee_id, amount, payment_date) VALUES (?, ?, ?)", rows_to_insert)
            conn.commit()
        conn.close()
    return redirect(url_for('payments', bulk_success=1))

# NEW: Route to add Misc Income
@app.route("/add-misc-income", methods=["POST"])
def add_misc_income():
    date = request.form.get("income_date")
    source = request.form.get("source")
    amount = request.form.get("amount")
    if date and source and amount:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Misc_Income_Raw (source, amount, income_date) VALUES (?, ?, ?)", (source, float(amount), date))
        conn.commit()
        conn.close()
        # The list is filtered by month, so open the month this entry belongs to --
        # otherwise an entry dated in another month would look like it vanished.
        return redirect(url_for('payments', filter_month=date[:7]))
    return redirect(url_for('payments'))

# NEW: Route to delete Misc Income
@app.route("/delete-misc-income", methods=["POST"])
def delete_misc_income():
    income_id = request.form.get("income_id")
    if income_id:
        conn = connect_db()
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE Misc_Income_Raw SET is_deleted = 1, deleted_at = ? WHERE income_id = ?", (now_str, income_id))
        conn.commit()
        conn.close()
    return redirect(url_for('payments'))

# --- NEW: STAFF LOANS (informal interest-free advances from staff) ---
@app.route("/recycle-bin")
def recycle_bin():
    conn = connect_db()
    cursor = conn.cursor()

    items = []

    cursor.execute("""
        SELECT p.payment_id, e.name, p.amount, p.payment_date, p.deleted_at
        FROM Payments_Raw p LEFT JOIN Employees e ON p.employee_id = e.employee_id
        WHERE p.is_deleted = 1
    """)
    for row in cursor.fetchall():
        pid, name, amount, pdate, deleted_at = row
        items.append({
            "table": "Payments", "id": pid, "deleted_at": deleted_at,
            "icon": "credit-card", "label": "Payment",
            "summary": f"₹{amount:.2f} from {name or 'Unknown'}",
            "detail": f"Dated {to_dmy(pdate)}",
        })

    cursor.execute("""
        SELECT purchase_id, item_category, description, amount, purchase_date, vendor, deleted_at
        FROM Purchases_Raw WHERE is_deleted = 1
    """)
    for row in cursor.fetchall():
        purid, cat, desc, amount, pdate, vendor, deleted_at = row
        items.append({
            "table": "Purchases", "id": purid, "deleted_at": deleted_at,
            "icon": "shopping-cart", "label": "Expense",
            "summary": f"₹{amount:.2f} — {cat or 'Uncategorized'}",
            "detail": f"{desc or ''} · {to_dmy(pdate)}",
        })

    cursor.execute("""
        SELECT m.log_id, e.name, m.item_name, m.quantity, m.locked_price, m.log_date, m.deleted_at
        FROM Meal_Log_Raw m LEFT JOIN Employees e ON m.employee_id = e.employee_id
        WHERE m.is_deleted = 1
    """)
    for row in cursor.fetchall():
        lid, name, item, qty, price, ldate, deleted_at = row
        items.append({
            "table": "Meal_Log", "id": lid, "deleted_at": deleted_at,
            "icon": "utensils", "label": "Meal Entry",
            "summary": f"{name or 'Unknown'} — {item} x{qty}",
            "detail": f"₹{(qty or 0) * (price or 0):.2f} on {to_dmy(ldate)}",
        })

    cursor.execute("""
        SELECT income_id, source, amount, income_date, deleted_at
        FROM Misc_Income_Raw WHERE is_deleted = 1
    """)
    for row in cursor.fetchall():
        iid, source, amount, idate, deleted_at = row
        items.append({
            "table": "Misc_Income", "id": iid, "deleted_at": deleted_at,
            "icon": "banknote", "label": "Misc Income",
            "summary": f"₹{amount:.2f} — {source or 'Unspecified'}",
            "detail": f"Dated {to_dmy(idate)}",
        })

    cursor.execute("""
        SELECT sl.loan_id, e.name, sl.amount, sl.loan_date, sl.repay_method, sl.deleted_at
        FROM Staff_Loans_Raw sl LEFT JOIN Employees e ON sl.employee_id = e.employee_id
        WHERE sl.is_deleted = 1
    """)
    for row in cursor.fetchall():
        loanid, name, amount, ldate, method, deleted_at = row
        items.append({
            "table": "Staff_Loans", "id": loanid, "deleted_at": deleted_at,
            "icon": "hand-coins", "label": "Staff Loan",
            "summary": f"₹{amount:.2f} — {name or 'Unknown'}",
            "detail": f"{'Bill credit' if method == 'bill_credit' else 'Cash'} · {to_dmy(ldate)}",
        })

    # Compute days left before permanent auto-purge, sort newest-deleted first
    now = datetime.datetime.now()
    for item in items:
        try:
            deleted_dt = datetime.datetime.strptime(item["deleted_at"], '%Y-%m-%d %H:%M:%S')
            days_left = RECYCLE_BIN_RETENTION_DAYS - (now - deleted_dt).days
            item["days_left"] = max(0, days_left)
        except (TypeError, ValueError):
            item["days_left"] = RECYCLE_BIN_RETENTION_DAYS

    items.sort(key=lambda x: x["deleted_at"] or "", reverse=True)

    # Display only -- days-left and the sort above needed the original stamp
    for item in items:
        item["deleted_at"] = to_dmy_time(item["deleted_at"])

    conn.close()
    return render_template("recycle_bin.html", items=items, retention_days=RECYCLE_BIN_RETENTION_DAYS)

def _restore_recycle_bin_item(cursor, table, item_id):
    if table == "Payments" and cursor.execute("SELECT 1 FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (item_id,)).fetchone():
        abort(409, description="Restore or remove this linked payment through its Staff Loan entry.")
    pk, cols = RECYCLE_BIN_TABLES[table]
    raw_table = f"{table}_Raw"
    cursor.execute(f"UPDATE {raw_table} SET is_deleted = 0, deleted_at = NULL WHERE {pk} = ?", (item_id,))

    # Restoring a bill-credit Staff Loan also restores its linked Payment,
    # mirroring what delete_loan() does on the way out.
    if table == "Staff_Loans":
        cursor.execute("SELECT repay_method, linked_payment_id FROM Staff_Loans_Raw WHERE loan_id = ?", (item_id,))
        row = cursor.fetchone()
        if row and row[0] == 'bill_credit' and row[1]:
            cursor.execute("UPDATE Payments_Raw SET is_deleted = 0, deleted_at = NULL WHERE payment_id = ?", (row[1],))

def _purge_recycle_bin_item(cursor, table, item_id):
    if table == "Payments" and cursor.execute("SELECT 1 FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (item_id,)).fetchone():
        abort(409, description="Restore or remove this linked payment through its Staff Loan entry.")
    pk, cols = RECYCLE_BIN_TABLES[table]
    raw_table = f"{table}_Raw"
    if table == "Staff_Loans":
        cursor.execute("DELETE FROM Payments_Raw WHERE is_deleted = 1 AND payment_id IN (SELECT linked_payment_id FROM Staff_Loans_Raw WHERE loan_id = ? AND is_deleted = 1)", (item_id,))
    cursor.execute(f"DELETE FROM {raw_table} WHERE {pk} = ? AND is_deleted = 1", (item_id,))

@app.route("/restore-item", methods=["POST"])
def restore_item():
    table = request.form.get("table")
    item_id = request.form.get("item_id")

    if table in RECYCLE_BIN_TABLES and item_id:
        conn = connect_db()
        cursor = conn.cursor()
        _restore_recycle_bin_item(cursor, table, item_id)
        conn.commit()
        conn.close()
    return redirect(url_for('recycle_bin'))

@app.route("/purge-item", methods=["POST"])
def purge_item():
    table = request.form.get("table")
    item_id = request.form.get("item_id")

    if table in RECYCLE_BIN_TABLES and item_id:
        conn = connect_db()
        cursor = conn.cursor()
        _purge_recycle_bin_item(cursor, table, item_id)
        conn.commit()
        conn.close()
    return redirect(url_for('recycle_bin'))

@app.route("/bulk-restore-items", methods=["POST"])
def bulk_restore_items():
    # Each checkbox value is "Table|id" since the bin spans 5 different tables
    selected = request.form.getlist("selected_items")
    if selected:
        conn = connect_db()
        cursor = conn.cursor()
        selected_loans = {entry.partition("|")[2] for entry in selected if entry.startswith("Staff_Loans|")}
        for entry in selected:
            table, _, item_id = entry.partition("|")
            if table == "Payments":
                parent = cursor.execute("SELECT loan_id FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (item_id,)).fetchone()
                if parent and str(parent[0]) in selected_loans:
                    continue
            if table in RECYCLE_BIN_TABLES and item_id:
                _restore_recycle_bin_item(cursor, table, item_id)
        conn.commit()
        conn.close()
    return redirect(url_for('recycle_bin'))

@app.route("/bulk-purge-items", methods=["POST"])
def bulk_purge_items():
    selected = request.form.getlist("selected_items")
    if selected:
        conn = connect_db()
        cursor = conn.cursor()
        selected_loans = {entry.partition("|")[2] for entry in selected if entry.startswith("Staff_Loans|")}
        for entry in selected:
            table, _, item_id = entry.partition("|")
            if table == "Payments":
                parent = cursor.execute("SELECT loan_id FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (item_id,)).fetchone()
                if parent and str(parent[0]) in selected_loans:
                    continue
            if table in RECYCLE_BIN_TABLES and item_id:
                _purge_recycle_bin_item(cursor, table, item_id)
        conn.commit()
        conn.close()
    return redirect(url_for('recycle_bin'))


# --- NEW: CONSUMPTION MODULE (item-level quantity + amount tracking) ---

# --- NEW: MANAGER'S FUND LEDGER (personal, fully isolated from main accounts) ---
@app.route("/manager-fund", methods=["GET", "POST"])
def manager_fund():
    success = request.args.get('success') == '1'
    conn = connect_db()
    cursor = conn.cursor()

    if request.method == "POST":
        entry_date = request.form.get("entry_date")
        amount = request.form.get("amount")
        remarks = request.form.get("remarks", "")
        if entry_date and amount:
            cursor.execute("INSERT INTO Manager_Fund_Ledger (entry_date, amount, remarks) VALUES (?, ?, ?)",
                           (entry_date, float(amount), remarks))
            conn.commit()
            conn.close()
            return redirect(url_for('manager_fund', month=entry_date[:7], success=1))

    cursor.execute("SELECT opening_balance, opening_date FROM Manager_Fund_Settings WHERE id = 1")
    true_opening_balance, true_opening_date = cursor.fetchone()

    # Which month is being viewed (defaults to current month)
    target_month = request.args.get('month') or datetime.date.today().strftime('%Y-%m')
    month_start = f"{target_month}-01"

    # This month's opening = the original starting balance PLUS every entry
    # dated before this month -- i.e. automatically carried forward from
    # whatever month came before, however far back that is.
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Manager_Fund_Ledger WHERE entry_date < ?", (month_start,))
    prior_entries_total = cursor.fetchone()[0]
    month_opening_balance = true_opening_balance + prior_entries_total

    cursor.execute("""
        SELECT entry_id, entry_date, amount, remarks FROM Manager_Fund_Ledger
        WHERE entry_date LIKE ? ORDER BY entry_date ASC, entry_id ASC
    """, (f"{target_month}%",))
    raw_entries = cursor.fetchall()

    # Running balance for the month, computed fresh every time (never
    # stored) so it can never drift out of sync if an entry is edited or
    # deleted later.
    entries = []
    running = month_opening_balance
    for entry_id, entry_date, amount, remarks in raw_entries:
        running += amount
        entries.append({"id": entry_id, "date": entry_date, "amount": amount, "remarks": remarks, "balance": running})

    month_closing_balance = running  # becomes next month's opening automatically

    # The TRUE current outstanding (all-time latest), independent of
    # whichever month is currently being viewed/filtered.
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Manager_Fund_Ledger")
    all_time_total = cursor.fetchone()[0]
    current_balance = true_opening_balance + all_time_total

    # Months available for the selector: every month with entries, plus the
    # opening month and the current month, so there's always something to pick.
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', entry_date) FROM Manager_Fund_Ledger WHERE entry_date IS NOT NULL")
    available_months = set(r[0] for r in cursor.fetchall() if r[0])
    if true_opening_date:
        available_months.add(true_opening_date[:7])
    available_months.add(target_month)
    available_months = sorted(available_months, reverse=True)
    available_months_display = [
        {"value": m, "label": datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y')}
        for m in available_months
    ]
    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')

    cursor.execute("SELECT canteen_manager FROM Global_Settings WHERE id = 1")
    manager_row = cursor.fetchone()
    manager_name = manager_row[0] if manager_row else "Canteen Manager"

    conn.close()
    return render_template(
        "manager_fund.html", entries=entries,
        month_opening_balance=month_opening_balance, month_closing_balance=month_closing_balance,
        true_opening_balance=true_opening_balance, true_opening_date=true_opening_date,
        target_month=target_month, display_month_name=display_month_name,
        available_months_display=available_months_display,
        current_balance=current_balance, manager_name=manager_name, success=success,
    )

@app.route("/set-manager-fund-opening", methods=["POST"])
def set_manager_fund_opening():
    opening_balance = request.form.get("opening_balance")
    opening_date = request.form.get("opening_date")
    redirect_month = request.form.get("redirect_month")
    if opening_balance is not None:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Manager_Fund_Settings SET opening_balance = ?, opening_date = ? WHERE id = 1",
                       (float(opening_balance), opening_date))
        conn.commit()
        conn.close()
    return redirect(url_for('manager_fund', month=redirect_month))

@app.route("/edit-manager-fund-entry", methods=["POST"])
def edit_manager_fund_entry():
    entry_id = request.form.get("entry_id")
    entry_date = request.form.get("entry_date")
    amount = request.form.get("amount")
    remarks = request.form.get("remarks", "")
    redirect_month = request.form.get("redirect_month")
    if entry_id and entry_date and amount:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Manager_Fund_Ledger SET entry_date = ?, amount = ?, remarks = ? WHERE entry_id = ?",
                       (entry_date, float(amount), remarks, entry_id))
        conn.commit()
        conn.close()
    return redirect(url_for('manager_fund', month=redirect_month))

@app.route("/delete-manager-fund-entry", methods=["POST"])
def delete_manager_fund_entry():
    entry_id = request.form.get("entry_id")
    redirect_month = request.form.get("redirect_month")
    if entry_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Manager_Fund_Ledger WHERE entry_id = ?", (entry_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('manager_fund', month=redirect_month))

@app.route("/download-payments-report-pdf")
def download_payments_report_pdf():
    report_type = request.args.get('report')
    report_month = request.args.get('report_month') or datetime.date.today().strftime('%Y-%m')

    conn = connect_db()
    cursor = conn.cursor()

    if report_type == 'paid':
        cursor.execute("""
            SELECT e.name, e.department, SUM(p.amount) as total_paid
            FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id
            WHERE strftime('%Y-%m', p.payment_date) = ?
            GROUP BY p.employee_id
            ORDER BY total_paid DESC
        """, (report_month,))
        report_rows = cursor.fetchall()
        title = f"STAFF WHO PAID - {report_month}"
    else:
        y, m = int(report_month[:4]), int(report_month[5:7])
        month_start = f"{report_month}-01"
        prev_month_start = f"{y-1:04d}-12-01" if m == 1 else f"{y:04d}-{m-1:02d}-01"
        prev_month_display = datetime.datetime.strptime(prev_month_start, '%Y-%m-%d').strftime('%B %Y').upper()
        cursor.execute("""
            SELECT e.name, e.department, e.opening_balance,
                (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id AND log_date < ?) as billed,
                (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id) as paid
            FROM Employees e WHERE e.is_active = 1
        """, (month_start,))
        pending = []
        for name, dept, opening, billed, paid in cursor.fetchall():
            due = opening + billed - paid
            if due > 0.005:
                pending.append((name, dept, due))
        pending.sort(key=lambda r: r[2], reverse=True)
        report_rows = pending
        title = f"OUTSTANDING AS OF END OF {prev_month_display}"

    conn.close()
    report_total = sum(r[2] for r in report_rows)

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_margins(left=10, top=10, right=10)
    pdf.add_page()

    pdf.set_fill_color(15, 60, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", 'B', 13)
    pdf.cell(0, 10, txt=title, ln=True, align='C', fill=True)
    pdf.set_text_color(0, 0, 0)

    pdf.ln(4)
    pdf.set_fill_color(210, 225, 245)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(90, 9, txt="Staff Member", border=1, align='L', fill=True)
    pdf.cell(50, 9, txt="Department", border=1, align='L', fill=True)
    pdf.cell(40, 9, txt="Amount", border=1, align='R', fill=True)
    pdf.ln()

    pdf.set_font("Arial", '', 10)
    fill = False
    for name, dept, amount in report_rows:
        pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(90, 8, txt=name, border=1, align='L', fill=True)
        pdf.cell(50, 8, txt=(dept or "-"), border=1, align='L', fill=True)
        pdf.cell(40, 8, txt=f"{amount:.2f}", border=1, align='R', fill=True)
        pdf.ln()
        fill = not fill

    if not report_rows:
        pdf.set_font("Arial", 'I', 10)
        pdf.cell(0, 10, txt="No records found.", border=1, align='C')
    else:
        pdf.set_font("Arial", 'B', 10)
        pdf.set_fill_color(230, 230, 230)
        pdf.cell(140, 9, txt=f"Total ({len(report_rows)} staff)", border=1, align='R', fill=True)
        pdf.cell(40, 9, txt=f"{report_total:.2f}", border=1, align='R', fill=True)

    pdf_path = "Payments_Report.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))


@app.route("/download-manager-fund-pdf")
def download_manager_fund_pdf():
    target_month = request.args.get('month') or datetime.date.today().strftime('%Y-%m')
    month_start = f"{target_month}-01"

    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT opening_balance, opening_date FROM Manager_Fund_Settings WHERE id = 1")
    true_opening_balance, true_opening_date = cursor.fetchone()

    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Manager_Fund_Ledger WHERE entry_date < ?", (month_start,))
    prior_total = cursor.fetchone()[0]
    month_opening_balance = true_opening_balance + prior_total

    cursor.execute("""
        SELECT entry_date, amount, remarks FROM Manager_Fund_Ledger
        WHERE entry_date LIKE ? ORDER BY entry_date ASC, entry_id ASC
    """, (f"{target_month}%",))
    raw_entries = cursor.fetchall()

    cursor.execute("SELECT canteen_manager FROM Global_Settings WHERE id = 1")
    manager_row = cursor.fetchone()
    manager_name = manager_row[0] if manager_row else "Canteen Manager"
    conn.close()

    entries = []
    running = month_opening_balance
    for entry_date, amount, remarks in raw_entries:
        running += amount
        entries.append((entry_date, amount, running, remarks))
    month_closing_balance = running
    display_month = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_margins(left=10, top=10, right=10)
    pdf.add_page()

    pdf.set_fill_color(15, 60, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", 'B', 13)
    pdf.cell(0, 10, txt="CONTRIBUTION & EXPENDITURE - MANAGER'S FUND", ln=True, align='C', fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", '', 10)
    pdf.cell(0, 8, txt=f"Managed by: {manager_name}   |   {display_month}", ln=True, align='C')

    pdf.ln(4)
    pdf.set_fill_color(220, 210, 235)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(0, 9, txt=f"Opening (Carried Forward): Rs. {month_opening_balance:.2f}   |   Closing: Rs. {month_closing_balance:.2f}",
             border=1, align='C', fill=True, ln=True)

    pdf.ln(3)
    pdf.set_fill_color(210, 225, 245)
    pdf.set_font("Arial", 'B', 9)
    col_w = [28, 30, 32, 90]  # Date, Amount, Balance, Remarks
    pdf.cell(col_w[0], 9, txt="Date", border=1, align='C', fill=True)
    pdf.cell(col_w[1], 9, txt="Amount", border=1, align='C', fill=True)
    pdf.cell(col_w[2], 9, txt="Balance", border=1, align='C', fill=True)
    pdf.cell(col_w[3], 9, txt="Remarks", border=1, align='C', fill=True)
    pdf.ln()

    pdf.set_font("Arial", '', 9)
    fill = False
    for entry_date, amount, balance, remarks in entries:
        pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(col_w[0], 8, txt=to_dmy(entry_date), border=1, align='C', fill=True)
        color = (200, 0, 0) if amount < 0 else (0, 130, 0)
        pdf.set_text_color(*color)
        pdf.cell(col_w[1], 8, txt=f"{amount:+.2f}", border=1, align='R', fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(col_w[2], 8, txt=f"{balance:.2f}", border=1, align='R', fill=True)
        pdf.cell(col_w[3], 8, txt=(remarks or "")[:45], border=1, align='L', fill=True)
        pdf.ln()
        fill = not fill

    if not entries:
        pdf.set_font("Arial", 'I', 10)
        pdf.cell(0, 10, txt=f"No transactions logged for {display_month}.", border=1, align='C')

    pdf_path = "Manager_Fund_Ledger.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))


@app.route("/consumption", methods=["GET", "POST"])
def consumption():
    success = request.args.get('success') == '1'
    conn = connect_db()
    cursor = conn.cursor()

    if request.method == "POST":
        item_id = request.form.get("item_id")
        quantity = request.form.get("quantity")
        amount = request.form.get("amount")
        purchase_date = request.form.get("purchase_date")
        notes = request.form.get("notes", "")
        if item_id and quantity and amount and purchase_date:
            cursor.execute(
                "INSERT INTO Consumption_Log (item_id, quantity, amount, purchase_date, notes) VALUES (?, ?, ?, ?, ?)",
                (item_id, float(quantity), float(amount), purchase_date, notes)
            )
            conn.commit()
            conn.close()
            # Send them back to the exact item + month they just logged, so
            # the entry they just added is immediately visible.
            return redirect(url_for('consumption', item_id=item_id, month=purchase_date[:7], success=1))

    cursor.execute("SELECT item_id, item_name, unit FROM Consumption_Items WHERE is_active = 1 ORDER BY item_name ASC")
    items = cursor.fetchall()

    cursor.execute("SELECT item_id, item_name, unit FROM Consumption_Items WHERE is_active = 0 ORDER BY item_name ASC")
    archived_consumption_items = cursor.fetchall()

    target_month = request.args.get('month') or datetime.date.today().strftime('%Y-%m')
    selected_item_id = request.args.get('item_id', type=int)
    if not selected_item_id and items:
        selected_item_id = items[0][0]

    selected_item_name, selected_item_unit = None, None
    for it in items:
        if it[0] == selected_item_id:
            selected_item_name, selected_item_unit = it[1], it[2]

    insights = None
    history = []
    six_month_trend = {"labels": [], "rates": []}

    if selected_item_id:
        # History + this month's totals for the selected item
        cursor.execute("""
            SELECT log_id, quantity, amount, purchase_date, notes FROM Consumption_Log
            WHERE item_id = ? AND purchase_date LIKE ?
            ORDER BY purchase_date DESC, log_id DESC
        """, (selected_item_id, f"{target_month}%"))
        history = cursor.fetchall()

        total_qty = sum(r[1] for r in history)
        total_amt = sum(r[2] for r in history)
        avg_rate_this_month = (total_amt / total_qty) if total_qty > 0 else 0.0

        # Previous month, for the rate-of-increase comparison
        y, m = int(target_month[:4]), int(target_month[5:7])
        prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
        prev_month = f"{prev_y:04d}-{prev_m:02d}"
        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(amount), 0) FROM Consumption_Log
            WHERE item_id = ? AND purchase_date LIKE ?
        """, (selected_item_id, f"{prev_month}%"))
        prev_qty, prev_amt = cursor.fetchone()
        prev_rate = (prev_amt / prev_qty) if prev_qty > 0 else None
        rate_change_pct = None
        if prev_rate and prev_rate > 0:
            rate_change_pct = ((avg_rate_this_month - prev_rate) / prev_rate) * 100

        # Last 6 months (rolling back from target_month) blended average rate
        months = []
        yy, mm = y, m
        for _ in range(6):
            months.append(f"{yy:04d}-{mm:02d}")
            mm -= 1
            if mm == 0:
                mm = 12
                yy -= 1
        months.reverse()

        six_qty_total, six_amt_total = 0.0, 0.0
        for mo in months:
            cursor.execute("""
                SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(amount), 0) FROM Consumption_Log
                WHERE item_id = ? AND purchase_date LIKE ?
            """, (selected_item_id, f"{mo}%"))
            mo_qty, mo_amt = cursor.fetchone()
            six_qty_total += mo_qty
            six_amt_total += mo_amt
            six_month_trend["labels"].append(datetime.datetime.strptime(mo, '%Y-%m').strftime('%b %Y'))
            six_month_trend["rates"].append(round((mo_amt / mo_qty), 2) if mo_qty > 0 else 0)

        avg_rate_6mo = (six_amt_total / six_qty_total) if six_qty_total > 0 else 0.0

        insights = {
            "total_qty": total_qty, "total_amt": total_amt,
            "avg_rate_this_month": avg_rate_this_month,
            "avg_rate_6mo": avg_rate_6mo,
            "rate_change_pct": rate_change_pct,
        }

    # Months available for the selector (has data, plus always the current month)
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', purchase_date) FROM Consumption_Log WHERE purchase_date IS NOT NULL")
    available_months = sorted(set([r[0] for r in cursor.fetchall() if r[0]] + [target_month]), reverse=True)
    available_months_display = [
        {"value": m, "label": datetime.datetime.strptime(m, '%Y-%m').strftime('%B %Y')}
        for m in available_months
    ]
    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')

    conn.close()
    # Dates shown as DD-MM-YYYY (totals/averages above used the rows already)
    history = [(r[0], r[1], r[2], to_dmy(r[3]), *r[4:]) for r in history]
    return render_template(
        "consumption.html", items=items, archived_consumption_items=archived_consumption_items,
        target_month=target_month, display_month_name=display_month_name,
        available_months_display=available_months_display, selected_item_id=selected_item_id,
        selected_item_name=selected_item_name, selected_item_unit=selected_item_unit,
        insights=insights, history=history, six_month_trend=six_month_trend, success=success,
    )

@app.route("/add-consumption-item", methods=["POST"])
def add_consumption_item():
    item_name = request.form.get("item_name")
    unit = request.form.get("unit") or "kg"
    if item_name:
        conn = connect_db()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO Consumption_Items (item_name, unit) VALUES (?, ?)", (item_name.strip(), unit.strip()))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()
    return redirect(url_for('consumption'))

@app.route("/deactivate-consumption-item", methods=["POST"])
def deactivate_consumption_item():
    item_id = request.form.get("item_id")
    if item_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Consumption_Items SET is_active = 0 WHERE item_id = ?", (item_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('consumption'))

@app.route("/reactivate-consumption-item", methods=["POST"])
def reactivate_consumption_item():
    item_id = request.form.get("item_id")
    if item_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Consumption_Items SET is_active = 1 WHERE item_id = ?", (item_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('consumption'))

@app.route("/delete-consumption-log", methods=["POST"])
def delete_consumption_log():
    log_id = request.form.get("log_id")
    if log_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Consumption_Log WHERE log_id = ?", (log_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('consumption'))


@app.route("/staff-loans", methods=["GET", "POST"])
def staff_loans():
    success = request.args.get("success") == "1"
    conn = connect_db()
    cursor = conn.cursor()

    if request.method == "POST":
        employee_id = request.form.get("employee_id")
        amount = request.form.get("amount")
        loan_date = request.form.get("loan_date")
        repay_method = request.form.get("repay_method")

        if employee_id and amount and loan_date and repay_method in ("cash", "bill_credit"):
            amount = float(amount)
            linked_payment_id = None
            status = "outstanding"
            settled_date = None

            if repay_method == "bill_credit":
                # Applies immediately as a normal bill payment for that
                # employee, so it flows through billing/history/PDF exactly
                # like any other payment. It's "settled" the moment it's logged.
                cursor.execute("INSERT INTO Payments_Raw (employee_id, amount, payment_date) VALUES (?, ?, ?)",
                               (employee_id, amount, loan_date))
                linked_payment_id = cursor.lastrowid
                status = "settled"
                settled_date = loan_date

            cursor.execute("""INSERT INTO Staff_Loans_Raw 
                (employee_id, amount, loan_date, repay_method, status, settled_date, linked_payment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (employee_id, amount, loan_date, repay_method, status, settled_date, linked_payment_id))
            conn.commit()
            success = True

    cursor.execute("SELECT employee_id, name FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()

    cursor.execute("""
        SELECT sl.loan_id, e.name, sl.amount, sl.loan_date, sl.repay_method
        FROM Staff_Loans sl JOIN Employees e ON sl.employee_id = e.employee_id
        WHERE sl.status = 'outstanding'
        ORDER BY sl.loan_date ASC
    """)
    outstanding_loans = cursor.fetchall()

    cursor.execute("""
        SELECT sl.loan_id, e.name, sl.amount, sl.loan_date, sl.repay_method, sl.settled_date
        FROM Staff_Loans sl JOIN Employees e ON sl.employee_id = e.employee_id
        WHERE sl.status = 'settled'
        ORDER BY sl.settled_date DESC, sl.loan_id DESC
        LIMIT 50
    """)
    settled_loans = cursor.fetchall()

    total_outstanding_loans = sum(r[2] for r in outstanding_loans)

    # Dates shown as DD-MM-YYYY (the sorting and totals above are already done)
    outstanding_loans = [(r[0], r[1], r[2], to_dmy(r[3]), *r[4:]) for r in outstanding_loans]
    settled_loans = [(r[0], r[1], r[2], to_dmy(r[3]), r[4], to_dmy(r[5])) for r in settled_loans]

    conn.close()
    return render_template("staff_loans.html", employees=employees, outstanding_loans=outstanding_loans,
                           settled_loans=settled_loans, total_outstanding_loans=total_outstanding_loans, success=success)

@app.route("/settle-loan", methods=["POST"])
def settle_loan():
    loan_id = request.form.get("loan_id")
    settled_date = request.form.get("settled_date") or datetime.date.today().strftime('%Y-%m-%d')

    if loan_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT employee_id, amount, status FROM Staff_Loans WHERE loan_id = ?", (loan_id,))
        row = cursor.fetchone()
        if row and row[2] == 'outstanding':
            # Cash repayment is informational only -- it does not touch the
            # Expense Tracker or any other computed total in the module.
            cursor.execute("UPDATE Staff_Loans_Raw SET status = 'settled', settled_date = ? WHERE loan_id = ?",
                           (settled_date, loan_id))
            conn.commit()
        conn.close()
    return redirect(url_for('staff_loans'))

@app.route("/delete-loan", methods=["POST"])
def delete_loan():
    loan_id = request.form.get("loan_id")
    if loan_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT repay_method, linked_payment_id FROM Staff_Loans WHERE loan_id = ?", (loan_id,))
        row = cursor.fetchone()
        if row:
            repay_method, linked_payment_id = row
            # If it was applied as bill credit, also remove the matching
            # payment so the employee's bill doesn't keep the phantom credit.
            if repay_method == 'bill_credit' and linked_payment_id:
                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute("UPDATE Payments_Raw SET is_deleted = 1, deleted_at = ? WHERE payment_id = ?", (now_str, linked_payment_id))
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE Staff_Loans_Raw SET is_deleted = 1, deleted_at = ? WHERE loan_id = ?", (now_str, loan_id))
        conn.commit()
        conn.close()
    return redirect(url_for('staff_loans'))

@app.route("/purchases", methods=["GET", "POST"])
def purchases():
    success = request.args.get("success") == "1"
    conn = connect_db()
    cursor = conn.cursor()
    
    # 1. Handle Month Selection (Defaults to current month)
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m')
    
    if request.method == "POST":
        p_date = request.form.get("purchase_date")
        category = request.form.get("category")
        vendor = request.form.get("vendor") or "General"  
        desc = request.form.get("description")
        amount = request.form.get("amount")
        
        if p_date and amount:
            cursor.execute("INSERT INTO Purchases_Raw (item_category, description, amount, purchase_date, vendor) VALUES (?, ?, ?, ?, ?)",
                           (category, desc, float(amount), p_date, vendor))
            # Remember this vendor/description for next time, so it shows
            # up as a quick option instead of being retyped from scratch.
            if vendor and vendor != "General":
                cursor.execute("INSERT OR IGNORE INTO Vendors (vendor_name) VALUES (?)", (vendor.strip(),))
            if desc:
                cursor.execute("INSERT OR IGNORE INTO Purchase_Descriptions (description_text) VALUES (?)", (desc.strip(),))
            conn.commit()
            success = True
            
    # 2. Fetch specific month's purchases
    cursor.execute("SELECT * FROM Purchases WHERE purchase_date LIKE ? ORDER BY purchase_date DESC, purchase_id DESC", (f"{target_month}%",))
    recent_purchases = []
    for row in cursor.fetchall():
        row = list(row)
        raw_date = row[4]  # keep the YYYY-MM-DD form too, for the edit form's date input
        try:
            row[4] = datetime.datetime.strptime(row[4], '%Y-%m-%d').strftime('%d-%m-%Y')
        except (ValueError, TypeError):
            pass
        row.append(raw_date)  # row[6]
        recent_purchases.append(row)
    
    # 3. Get available months for the dropdown
    cursor.execute("SELECT DISTINCT strftime('%Y-%m', purchase_date) FROM Purchases UNION SELECT ? ORDER BY 1 DESC", (target_month,))
    available_months = [row[0] for row in cursor.fetchall() if row[0]]
    
    cursor.execute("SELECT * FROM Expense_Categories WHERE is_active = 1 ORDER BY category_name ASC")
    categories = cursor.fetchall()
    
    # NEW: Archived categories, so they can be restored right from Expense Tracker too
    cursor.execute("SELECT * FROM Expense_Categories WHERE is_active = 0 ORDER BY category_name ASC")
    archived_categories = cursor.fetchall()

    # NEW: Reusable Vendor and Item Description lists (active + archived)
    cursor.execute("SELECT * FROM Vendors WHERE is_active = 1 ORDER BY vendor_name ASC")
    vendors = cursor.fetchall()
    cursor.execute("SELECT * FROM Vendors WHERE is_active = 0 ORDER BY vendor_name ASC")
    archived_vendors = cursor.fetchall()
    cursor.execute("SELECT * FROM Purchase_Descriptions WHERE is_active = 1 ORDER BY description_text ASC")
    purchase_descriptions = cursor.fetchall()
    cursor.execute("SELECT * FROM Purchase_Descriptions WHERE is_active = 0 ORDER BY description_text ASC")
    archived_purchase_descriptions = cursor.fetchall()

    conn.close() 
    
    bulk_success = request.args.get("bulk_success") == "1"
    return render_template("purchases.html", recent_purchases=recent_purchases, success=success, categories=categories, archived_categories=archived_categories,
                           vendors=vendors, archived_vendors=archived_vendors,
                           purchase_descriptions=purchase_descriptions, archived_purchase_descriptions=archived_purchase_descriptions,
                           target_month=target_month, available_months=available_months, bulk_success=bulk_success)

# NEW: Log several expense line-items from the same shopping trip in one go
@app.route("/bulk-purchases", methods=["POST"])
def bulk_purchases():
    p_date = request.form.get("purchase_date")
    if p_date:
        categories_form = request.form.getlist("b_category[]")
        vendors_form = request.form.getlist("b_vendor[]")
        descriptions_form = request.form.getlist("b_description[]")
        amounts_form = request.form.getlist("b_amount[]")

        conn = connect_db()
        cursor = conn.cursor()
        rows_to_insert = []
        for category, vendor, description, amount in zip(categories_form, vendors_form, descriptions_form, amounts_form):
            description = (description or "").strip()
            vendor = (vendor or "").strip() or "General"
            if description and amount:
                try:
                    amount_val = float(amount)
                except ValueError:
                    amount_val = 0
                if amount_val > 0:
                    rows_to_insert.append((category, description, amount_val, p_date, vendor))
        if rows_to_insert:
            cursor.executemany(
                "INSERT INTO Purchases_Raw (item_category, description, amount, purchase_date, vendor) VALUES (?, ?, ?, ?, ?)",
                rows_to_insert
            )
            conn.commit()
        conn.close()
    return redirect(url_for('purchases', bulk_success=1))

@app.route("/edit-purchase", methods=["POST"])
def edit_purchase():
    purchase_id = request.form.get("purchase_id")
    new_date = request.form.get("new_date")
    new_category = request.form.get("new_category")
    new_vendor = (request.form.get("new_vendor") or "").strip() or "General"
    new_desc = request.form.get("new_desc")
    new_amount = request.form.get("new_amount")

    if purchase_id and new_date and new_category and new_desc and new_amount:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE Purchases_Raw SET purchase_date = ?, item_category = ?, vendor = ?, description = ?, amount = ? WHERE purchase_id = ?",
            (new_date, new_category, new_vendor, new_desc, float(new_amount), purchase_id)
        )
        # Keep the reusable vendor/description quick-lists in sync, same as new entries do
        if new_vendor and new_vendor != "General":
            cursor.execute("INSERT OR IGNORE INTO Vendors (vendor_name) VALUES (?)", (new_vendor,))
        if new_desc:
            cursor.execute("INSERT OR IGNORE INTO Purchase_Descriptions (description_text) VALUES (?)", (new_desc.strip(),))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

@app.route("/delete-purchase", methods=["POST"])
def delete_purchase():
    purchase_id = request.form.get("purchase_id")
    if purchase_id:
        conn = connect_db()
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE Purchases_Raw SET is_deleted = 1, deleted_at = ? WHERE purchase_id = ?", (now_str, purchase_id))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

# --- UPGRADED: LANDSCAPE PDF GENERATOR ---
# --- UPGRADED: LANDSCAPE PDF GENERATOR WITH PREV BALANCE ---
# --- UPGRADED: LANDSCAPE PDF GENERATOR WITH PREV BALANCE & SL NO ---
@app.route("/download-pdf")
def download_pdf():
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m') 
    display_month = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y').upper()
    
    # Define exact cutoff to calculate accurate Previous Balance
    start_of_month = f"{target_month}-01"

    conn = connect_db()
    cursor = conn.cursor()

    # Item columns are built DYNAMICALLY from whatever was actually logged
    # this month (ordered to match Settings/Bulk Entry order) -- a fixed
    # list of 6 names would silently drop any newer item (e.g. Pazhampori,
    # a renamed Tea variant) from the bill even though the total is right.
    cursor.execute("SELECT DISTINCT item_name FROM Meal_Log WHERE log_date LIKE ?", (f"{target_month}%",))
    used_items = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT item_name, display_order FROM Items_Master")
    item_order_map = dict(cursor.fetchall())
    month_items = sorted(used_items, key=lambda name: (item_order_map.get(name, 9999), name))

    # Working days = distinct dates with at least one meal entry this month
    cursor.execute("SELECT DISTINCT log_date FROM Meal_Log WHERE log_date LIKE ? ORDER BY log_date", (f"{target_month}%",))
    working_dates = [r[0] for r in cursor.fetchall()]

    # Per-employee, per-item quantities for the month (pivoted in Python
    # rather than a giant dynamic SQL CASE statement)
    cursor.execute("""
        SELECT employee_id, item_name, SUM(quantity) FROM Meal_Log
        WHERE log_date LIKE ? GROUP BY employee_id, item_name
    """, (f"{target_month}%",))
    qty_by_emp_item = {}
    for emp_id, item_name, qty in cursor.fetchall():
        qty_by_emp_item.setdefault(emp_id, {})[item_name] = qty

    cursor.execute("""
        SELECT e.employee_id, e.name,
            COALESCE((SELECT SUM(m.quantity * m.locked_price) FROM Meal_Log m WHERE m.employee_id = e.employee_id AND m.log_date LIKE ?), 0) as month_total,
            e.opening_balance,
            (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id AND log_date < ?) as past_meals,
            (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id) as total_paid
        FROM Employees e
        ORDER BY e.name ASC
    """, (f"{target_month}%", start_of_month))
    
    records = cursor.fetchall()
    conn.close()

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_margins(left=10, top=10, right=10)
    # Manual page-break handling (like the Weekly Confirmation PDF): FPDF's
    # own auto page-break checks per CELL, not per ROW, so with it left on,
    # a row could get split mid-way across two pages -- half its cells on
    # the bottom of page 1, the rest starting fresh at the top of page 2 --
    # which is exactly what was scrambling everything after ~row 14/15.
    pdf.set_auto_page_break(False)
    page_bottom = 195  # leave room for the bottom margin on landscape A4
    row_height = 10

    # Fixed columns pared down to only what they actually need, so the
    # growing number of item columns gets the bulk of the usable width
    # instead of overlapping/getting clipped.
    SL_W, NAME_W, DUE_W, PREV_W, BILL_W = 10, 40, 26, 30, 30
    # Fixed, pre-decided line breaks for the wider labels (rather than
    # auto-detecting the wrap) so the header renders identically no matter
    # what fpdf version/font metrics are running.
    DUE_LINES = ["AMOUNT", "PAYABLE"]
    BILL_LINES = ["MONTHLY", "BILL"]
    fixed_total = SL_W + NAME_W + DUE_W + PREV_W + BILL_W
    item_w = max((277 - fixed_total) / len(month_items), 7) if month_items else 0
    item_font_size = 8 if item_w >= 16 else (7 if item_w >= 12 else 6)
    header_line_h = 3.6

    # Header text WRAPS instead of getting cut off (e.g. "Tea (11 AM)"
    # becomes two lines rather than "Tea (11"). Figure out how many lines
    # the longest item name needs at this width/font, and size the whole
    # header row to fit it. Widths/fonts don't change page to page, so
    # this is worked out once, up front, rather than on every page.
    pdf.add_page()
    pdf.set_font("Arial", 'B', item_font_size)
    max_header_lines = max(len(DUE_LINES), len(BILL_LINES))
    item_wrap_lines = {}
    for item in month_items:
        lines = pdf.multi_cell(item_w - 1, header_line_h, item, border=0, align='C', split_only=True)
        item_wrap_lines[item] = lines
        max_header_lines = max(max_header_lines, len(lines))
    header_h = max(10, max_header_lines * header_line_h + 3)

    def draw_wrapped_header_cell(x, y, width, lines):
        """Bordered/filled header cell, vertically centered, one plain
        cell() per line -- no multi_cell auto-wrap involved."""
        pdf.rect(x, y, width, header_h, 'DF')
        text_h = len(lines) * header_line_h
        ly = y + (header_h - text_h) / 2
        for line in lines:
            pdf.set_xy(x, ly)
            pdf.cell(width, header_line_h, txt=line, border=0, align='C')
            ly += header_line_h

    def draw_page_header():
        """Title + column header row -- called for page 1 and again at the
        top of every continuation page, so the table is always readable no
        matter which page a row lands on."""
        pdf.set_fill_color(15, 60, 120)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Arial", 'B', 14)
        pdf.cell(0, 12, txt=" ESIC SUB-REGIONAL OFFICE, ERNAKULAM", ln=True, align='C', fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", 'B', 11)
        pdf.cell(0, 10, txt=f"STAFF TIFFIN ROOM BILL FOR THE MONTH OF {display_month}", ln=True, align='C')
        pdf.ln(5)
        pdf.set_fill_color(210, 225, 245)

        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(SL_W, header_h, txt="SL NO", border=1, align='C', fill=True)
        pdf.cell(NAME_W, header_h, txt="STAFF NAME", border=1, align='L', fill=True)

        due_x = pdf.get_x()
        draw_wrapped_header_cell(due_x, y0, DUE_W, DUE_LINES)
        pdf.set_xy(due_x + DUE_W, y0)

        cx = pdf.get_x()
        pdf.set_font("Arial", 'B', item_font_size)
        for item in month_items:
            draw_wrapped_header_cell(cx, y0, item_w, item_wrap_lines[item])
            cx += item_w

        pdf.set_xy(cx, y0)
        pdf.set_font("Arial", 'B', 8)
        pdf.cell(PREV_W, header_h, txt="PREV. BAL", border=1, align='C', fill=True)

        bill_x = pdf.get_x()
        draw_wrapped_header_cell(bill_x, y0, BILL_W, BILL_LINES)
        pdf.set_xy(bill_x + BILL_W, y0)
        # Explicit height, not pdf.ln(): a bare ln() advances by the last
        # printed line's height, which under-shoots whenever this header
        # row wrapped to 2 lines -- that under-shoot is what previously let
        # the next (data) row start drawing before the header had actually
        # finished, producing overlapping/garbled text.
        pdf.ln(header_h)

    draw_page_header()

    pdf.set_font("Arial", '', 9)
    fill = False
    
    # NEW: Initialize a serial number counter
    sl_no = 1

    # Running totals for the TOTAL row printed under the last staff row
    total_due = 0.0
    total_prev = 0.0
    total_bill = 0.0
    total_item_qty = {item: 0 for item in month_items}

    def color_and_text(value):
        """Red '+X.XX' if pending, green 'X.XX'/'0.00' if credit or settled."""
        if value > 0.005:
            return (200, 0, 0), f"+{value:.2f}"
        elif value < -0.005:
            return (0, 150, 0), f"{value:.2f}"
        else:
            return (0, 150, 0), "0.00"
    
    for rec in records:
        emp_id, name, month_bill, opening_bal, past_meals, total_paid = rec
        month_bill = month_bill or 0
        
        # Previous Balance now reflects ALL payments made to date (not just
        # ones before this month), so a June debt that was paid off in July
        # correctly shows as settled here instead of still being "due".
        prev_balance = opening_bal + past_meals - total_paid
        net_due = prev_balance + month_bill
        
        # Skip completely inactive accounts with no debt
        if month_bill == 0 and net_due == 0 and prev_balance == 0:
            continue

        # Accumulate totals only for rows that are actually printed
        total_due += net_due
        total_prev += prev_balance
        total_bill += month_bill
        for item in month_items:
            total_item_qty[item] += qty_by_emp_item.get(emp_id, {}).get(item, 0)

        # Page break if this row won't fit -- checked BEFORE the row starts,
        # so a row is never split across two pages. Header repeats on the
        # new page and the zebra striping restarts clean.
        if pdf.get_y() + row_height > page_bottom:
            pdf.add_page()
            draw_page_header()
            fill = False

        pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)

        pdf.set_font("Arial", '', 9)
        pdf.cell(SL_W, row_height, txt=f"{sl_no}", border=1, align='C', fill=True)
        pdf.cell(NAME_W, row_height, txt=f" {name}", border=1, align='L', fill=True)
        
        # NET DUE (right after name)
        color, due_text = color_and_text(net_due)
        pdf.set_text_color(*color)
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(DUE_W, row_height, txt=due_text, border=1, align='R', fill=True)
        pdf.set_text_color(0, 0, 0)

        pdf.set_font("Arial", '', item_font_size)
        emp_qtys = qty_by_emp_item.get(emp_id, {})
        for item in month_items:
            pdf.cell(item_w, row_height, txt=f"{emp_qtys.get(item, 0)}", border=1, align='C', fill=True)
        
        # PREVIOUS BALANCE (already net of every payment made to date)
        color, prev_text = color_and_text(prev_balance)
        pdf.set_text_color(*color)
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(PREV_W, row_height, txt=prev_text, border=1, align='R', fill=True)
        
        # Reset color to black for Month Bill
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", '', 9)
        pdf.cell(BILL_W, row_height, txt=f"{month_bill:.2f}", border=1, align='R', fill=True)
        
        pdf.ln()
        
        fill = not fill 
        
        # Increment the serial number only for people who are actually printed
        sl_no += 1

    # ---- TOTAL ROW (bottom of the bill) ----
    # Same no-split rule as the staff rows: if it won't fit on this page it
    # moves to a fresh page together with the repeated header.
    if sl_no > 1:
        if pdf.get_y() + row_height > page_bottom:
            pdf.add_page()
            draw_page_header()

        pdf.set_fill_color(210, 225, 245)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", 'B', 9)
        # SL NO + STAFF NAME columns merged into one "TOTAL" label cell
        pdf.cell(SL_W + NAME_W, row_height, txt="TOTAL", border=1, align='C', fill=True)

        # Total amount payable (red if pending, green if net credit)
        color, tdue_text = color_and_text(total_due)
        pdf.set_text_color(*color)
        pdf.cell(DUE_W, row_height, txt=tdue_text, border=1, align='R', fill=True)
        pdf.set_text_color(0, 0, 0)

        # Total quantity served per item
        pdf.set_font("Arial", 'B', item_font_size)
        for item in month_items:
            pdf.cell(item_w, row_height, txt=f"{total_item_qty[item]}", border=1, align='C', fill=True)

        pdf.set_font("Arial", 'B', 9)
        color, tprev_text = color_and_text(total_prev)
        pdf.set_text_color(*color)
        pdf.cell(PREV_W, row_height, txt=tprev_text, border=1, align='R', fill=True)

        pdf.set_text_color(0, 0, 0)
        pdf.cell(BILL_W, row_height, txt=f"{total_bill:.2f}", border=1, align='R', fill=True)
        pdf.ln()

        # Plain-language summary line under the table
        pdf.ln(3)
        if pdf.get_y() + 8 > page_bottom + 10:
            pdf.add_page()
            draw_page_header()
        pdf.set_font("Arial", 'B', 11)
        pdf.cell(0, 8, txt=f"TOTAL AMOUNT PAYABLE (Rs): {total_due:.2f}", ln=True, align='R')

    # Working days summary (bottom of the bill)
    day_labels = []
    for d in working_dates:
        try:
            day_labels.append(datetime.datetime.strptime(d, '%Y-%m-%d').strftime('%d-%m-%Y (%a)'))
        except (ValueError, TypeError):
            day_labels.append(str(d))
    pdf.ln(4)
    if pdf.get_y() + 26 > page_bottom + 10:
        pdf.add_page()
        draw_page_header()
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(0, 6, txt=f"TOTAL WORKING DAYS: {len(working_dates)}  (days on which entries were made)", ln=True, align='L')
    pdf.set_font("Arial", '', 9)
    pdf.multi_cell(0, 5, txt="Dates: " + (", ".join(day_labels) if day_labels else "No entries this month"))

    pdf_path = f"Tiffin_Bill_{display_month.replace(' ', '_')}.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))
# --- NEW: BALANCE SHEET PDF GENERATOR ---
# --- NEW: BALANCE SHEET PDF GENERATOR ---
@app.route("/download-balance-sheet")
def download_balance_sheet():
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m') 
    display_month = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y').upper()
    start_of_month = f"{target_month}-01"

    conn = connect_db()
    cursor = conn.cursor()

    # 1. Calculate TRUE Opening Balance for this specific month
    cursor.execute("SELECT canteen_opening_balance FROM Global_Settings WHERE id = 1")
    base_bal = cursor.fetchone()[0] or 0.0

    # Get Historical Staff Payments
    cursor.execute("SELECT SUM(amount) FROM Payments WHERE payment_date < ?", (start_of_month,))
    hist_paid = cursor.fetchone()[0] or 0.0

    # NEW: Get Historical Misc Income
    try:
        cursor.execute("SELECT SUM(amount) FROM Misc_Income WHERE income_date < ?", (start_of_month,))
        hist_misc = cursor.fetchone()[0] or 0.0
    except:
        hist_misc = 0.0

    hist_in = hist_paid + hist_misc

    cursor.execute("SELECT SUM(amount) FROM Purchases WHERE purchase_date < ?", (start_of_month,))
    hist_out = cursor.fetchone()[0] or 0.0

    month_opening_balance = base_bal + hist_in - hist_out

    # 2. Get this month's Inflows (Grouped by Staff Member + Misc Income)
    cursor.execute("""
        SELECT e.name, SUM(p.amount)
        FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id
        WHERE p.payment_date LIKE ? GROUP BY e.employee_id
    """, (f"{target_month}%",))
    staff_inflows = cursor.fetchall()

    # NEW: Add this month's Misc Income to the inflows list
    try:
        cursor.execute("""
            SELECT source, SUM(amount)
            FROM Misc_Income
            WHERE income_date LIKE ? GROUP BY source
        """, (f"{target_month}%",))
        misc_inflows = cursor.fetchall()
    except:
        misc_inflows = []

    # Combine both lists together for the PDF table
    inflows = staff_inflows + misc_inflows
    total_in = sum([row[1] for row in inflows])

    # 3. Get this month's Outflows (Grouped by Category)
    cursor.execute("""
        SELECT item_category, SUM(amount)
        FROM Purchases
        WHERE purchase_date LIKE ? GROUP BY item_category
    """, (f"{target_month}%",))
    outflows = cursor.fetchall()
    total_out = sum([row[1] for row in outflows])
    conn.close()

    # 4. Generate the PDF
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    
    # Header Styling
    pdf.set_fill_color(15, 60, 120) 
    pdf.set_text_color(255, 255, 255) 
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(0, 12, txt=" ESIC SUB-REGIONAL OFFICE, ERNAKULAM", ln=True, align='C', fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", 'B', 11)
    pdf.cell(0, 10, txt=f"CANTEEN BALANCE SHEET FOR {display_month}", ln=True, align='C')
    pdf.ln(5)

    # Opening Balance Block
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font("Arial", 'B', 11)
    pdf.cell(0, 12, txt=f"  OPENING BALANCE (As of 1st {display_month}): Rs. {month_opening_balance:.2f}", border=1, ln=True, fill=True)
    pdf.ln(5)

    # Section 1: INFLOWS (Receipts)
    pdf.set_fill_color(210, 225, 245)
    pdf.set_font("Arial", 'B', 9)
    # UPDATED: Changed the label since it's not just staff anymore
    pdf.cell(140, 10, txt="MONEY RECEIVED (STAFF & OTHER)", border=1, align='L', fill=True)
    pdf.cell(50, 10, txt="AMOUNT (Rs)", border=1, ln=True, align='R', fill=True)

    pdf.set_font("Arial", '', 9)
    fill = False
    for row in inflows:
        pdf.set_fill_color(248, 250, 252) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(140, 10, txt=f"  {row[0]}", border=1, align='L', fill=True)
        pdf.cell(50, 10, txt=f"{row[1]:.2f} ", border=1, ln=True, align='R', fill=True)
        fill = not fill

    pdf.set_font("Arial", 'B', 9)
    pdf.cell(140, 10, txt="TOTAL RECEIPTS ", border=1, align='R')
    pdf.cell(50, 10, txt=f"{total_in:.2f} ", border=1, ln=True, align='R')
    pdf.ln(8)

    # Section 2: OUTFLOWS (Expenses)
    pdf.set_fill_color(255, 228, 228) 
    pdf.set_font("Arial", 'B', 9)
    pdf.cell(140, 10, txt="EXPENSES INCURRED (BY CATEGORY)", border=1, align='L', fill=True)
    pdf.cell(50, 10, txt="AMOUNT (Rs)", border=1, ln=True, align='R', fill=True)

    pdf.set_font("Arial", '', 9)
    fill = False
    for row in outflows:
        pdf.set_fill_color(255, 248, 248) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(140, 10, txt=f"  {row[0]}", border=1, align='L', fill=True)
        pdf.cell(50, 10, txt=f"{row[1]:.2f} ", border=1, ln=True, align='R', fill=True)
        fill = not fill

    pdf.set_font("Arial", 'B', 9)
    pdf.cell(140, 10, txt="TOTAL EXPENSES ", border=1, align='R')
    pdf.cell(50, 10, txt=f"{total_out:.2f} ", border=1, ln=True, align='R')
    pdf.ln(8)

    # Closing Balance Block
    month_closing_balance = month_opening_balance + total_in - total_out
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(0, 12, txt=f"  CLOSING BALANCE (As of end of {display_month}): Rs. {month_closing_balance:.2f}", border=1, ln=True, fill=True)

    pdf_path = f"Balance_Sheet_{display_month.replace(' ', '_')}.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))


@app.route("/add-category", methods=["POST"])
def add_category():
    cat_name = request.form.get("category_name")
    next_page = request.form.get("next")  # lets Purchases quick-add return to Purchases, not Settings
    if cat_name:
        conn = connect_db()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO Expense_Categories (category_name) VALUES (?)", (cat_name.strip(),))
            conn.commit()
        except sqlite3.IntegrityError:
            pass 
        conn.close()
    if next_page == 'purchases':
        return redirect(url_for('purchases'))
    return redirect(url_for('settings'))

@app.route("/edit-category", methods=["POST"])
def edit_category():
    cat_id = request.form.get("category_id")
    new_name = request.form.get("new_name")
    next_page = request.form.get("next")
    if cat_id and new_name:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Expense_Categories SET category_name = ? WHERE category_id = ?", (new_name.strip(), cat_id))
        conn.commit()
        conn.close()
    if next_page == 'purchases':
        return redirect(url_for('purchases'))
    return redirect(url_for('settings'))

@app.route("/deactivate-category", methods=["POST"])
def deactivate_category():
    cat_id = request.form.get("category_id")
    next_page = request.form.get("next")
    if cat_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Expense_Categories SET is_active = 0 WHERE category_id = ?", (cat_id,))
        conn.commit()
        conn.close()
    if next_page == 'purchases':
        return redirect(url_for('purchases'))
    return redirect(url_for('settings'))

@app.route("/reactivate-category", methods=["POST"])
def reactivate_category():
    cat_id = request.form.get("category_id")
    next_page = request.form.get("next")
    if cat_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Expense_Categories SET is_active = 1 WHERE category_id = ?", (cat_id,))
        conn.commit()
        conn.close()
    if next_page == 'purchases':
        return redirect(url_for('purchases'))
    return redirect(url_for('settings'))

@app.route("/deactivate-vendor", methods=["POST"])
def deactivate_vendor():
    vendor_id = request.form.get("vendor_id")
    if vendor_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Vendors SET is_active = 0 WHERE vendor_id = ?", (vendor_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

@app.route("/reactivate-vendor", methods=["POST"])
def reactivate_vendor():
    vendor_id = request.form.get("vendor_id")
    if vendor_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Vendors SET is_active = 1 WHERE vendor_id = ?", (vendor_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

@app.route("/deactivate-purchase-description", methods=["POST"])
def deactivate_purchase_description():
    description_id = request.form.get("description_id")
    if description_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Purchase_Descriptions SET is_active = 0 WHERE description_id = ?", (description_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

@app.route("/reactivate-purchase-description", methods=["POST"])
def reactivate_purchase_description():
    description_id = request.form.get("description_id")
    if description_id:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Purchase_Descriptions SET is_active = 1 WHERE description_id = ?", (description_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('purchases'))

# --- V3: AUTOMATED EXCEL AUDIT EXPORT ---
@app.route("/download-excel")
def download_excel():
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m') 
        
    conn = connect_db()
    
    # We pass the complex PDF query directly into Pandas!
    query = """
        SELECT e.employee_id as ID, e.name as 'Staff Name', 
            SUM(CASE WHEN m.item_name = 'Breakfast' THEN m.quantity ELSE 0 END) as 'Breakfast',
            SUM(CASE WHEN m.item_name = 'Lunch' THEN m.quantity ELSE 0 END) as 'Lunch',
            SUM(CASE WHEN m.item_name = 'Tea' THEN m.quantity ELSE 0 END) as 'Tea',
            SUM(CASE WHEN m.item_name = 'Snacks' THEN m.quantity ELSE 0 END) as 'Snacks',
            SUM(CASE WHEN m.item_name = 'Omelette' THEN m.quantity ELSE 0 END) as 'Omelette',
            SUM(CASE WHEN m.item_name = 'Egg' THEN m.quantity ELSE 0 END) as 'Egg',
            SUM(m.quantity * m.locked_price) as 'Month Bill',
            e.opening_balance,
            (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id) as total_meals_ever,
            (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id) as total_payments_ever
        FROM Employees e 
        LEFT JOIN Meal_Log m ON e.employee_id = m.employee_id AND m.log_date LIKE ?
        WHERE e.is_active = 1
        GROUP BY e.employee_id
    """
    
    df = pd.read_sql_query(query, conn, params=(f"{target_month}%",))
    conn.close()
    
    # Pandas handles the complex math instantly
    df['Net Due'] = df['opening_balance'] + df['total_meals_ever'] - df['total_payments_ever']
    
    # Clean up the output by dropping background math columns
    df = df.drop(columns=['opening_balance', 'total_meals_ever', 'total_payments_ever'])
    df = df.fillna(0) # Replace empty database cells with zeros
    
    # Save to a virtual file so we don't clutter the hard drive
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=f'Ledger {target_month}')
        
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"Tiffin_Audit_{target_month}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
@app.route("/edit-payment", methods=["POST"])
def edit_payment():
    payment_id = request.form.get("payment_id")
    new_amount = request.form.get("new_amount")
    
    if payment_id and new_amount:
        conn = connect_db()
        cursor = conn.cursor()
        if cursor.execute("SELECT 1 FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (payment_id,)).fetchone():
            conn.close()
            abort(409, description="This payment belongs to a staff loan. Manage it from Staff Loans.")
        # Update the payment amount
        cursor.execute("UPDATE Payments_Raw SET amount = ? WHERE payment_id = ?", 
                       (float(new_amount), payment_id))
        conn.commit()
        conn.close()
    return redirect(url_for('payments'))

@app.route("/delete-payment", methods=["POST"])
def delete_payment():
    payment_id = request.form.get("payment_id")
    if payment_id:
        conn = connect_db()
        cursor = conn.cursor()
        if cursor.execute("SELECT 1 FROM Staff_Loans_Raw WHERE linked_payment_id = ?", (payment_id,)).fetchone():
            conn.close()
            abort(409, description="This payment belongs to a staff loan. Manage it from Staff Loans.")
        # Soft-delete -> recoverable from the Recycle Bin for 7 days
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE Payments_Raw SET is_deleted = 1, deleted_at = ? WHERE payment_id = ?", (now_str, payment_id))
        conn.commit()
        conn.close()
    return redirect(url_for('payments'))
# --- EXPORT EXPENSE TRACKER TO EXCEL ---
@app.route("/export-expenses")
def export_expenses():
    target_month = request.args.get('month')
    conn = connect_db()
    
    # If a month is requested, filter the Excel sheet to just that month
    if target_month:
        query = """
            SELECT purchase_date as 'Date', 
                   vendor as 'Vendor / Shop',
                   item_category as 'Category', 
                   description as 'Description', 
                   amount as 'Amount (₹)' 
            FROM Purchases 
            WHERE purchase_date LIKE ?
            ORDER BY purchase_date DESC
        """
        df = pd.read_sql_query(query, conn, params=(f"{target_month}%",))
        file_name = f"Expense_Tracker_{target_month}.xlsx"
    else:
        query = """
            SELECT purchase_date as 'Date', 
                   vendor as 'Vendor / Shop',
                   item_category as 'Category', 
                   description as 'Description', 
                   amount as 'Amount (₹)' 
            FROM Purchases 
            ORDER BY purchase_date DESC
        """
        df = pd.read_sql_query(query, conn)
        file_name = "Expense_Tracker_All_Time.xlsx"
        
    conn.close()
    
    output = build_excel_with_dmy_dates(df, 'Expense Tracker')
    return send_file(output, as_attachment=True, download_name=file_name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# NEW: Export the Staff Payments Ledger to Excel, optionally filtered by month
@app.route("/export-payments")
def export_payments():
    target_month = request.args.get('month')
    conn = connect_db()

    if target_month:
        query = """
            SELECT p.payment_date as 'Date',
                   e.name as 'Staff Member',
                   p.amount as 'Amount Paid (₹)'
            FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id
            WHERE p.payment_date LIKE ?
            ORDER BY p.payment_date DESC, p.payment_id DESC
        """
        df = pd.read_sql_query(query, conn, params=(f"{target_month}%",))
        file_name = f"Payments_Ledger_{target_month}.xlsx"
        sheet_name = f"Payments {target_month}"
    else:
        query = """
            SELECT p.payment_date as 'Date',
                   e.name as 'Staff Member',
                   p.amount as 'Amount Paid (₹)'
            FROM Payments p JOIN Employees e ON p.employee_id = e.employee_id
            ORDER BY p.payment_date DESC, p.payment_id DESC
        """
        df = pd.read_sql_query(query, conn)
        file_name = "Payments_Ledger_All_Time.xlsx"
        sheet_name = "Payments All Time"

    conn.close()

    output = build_excel_with_dmy_dates(df, sheet_name)
    return send_file(output, as_attachment=True, download_name=file_name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
# --- Monthly Verification Summary (Ledger History) -> Excel ---
@app.route("/export-verification-summary")
def export_verification_summary():
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    target_month = request.args.get('month') or datetime.date.today().strftime('%Y-%m')
    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')
    like = f"{target_month}%"

    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT item_name FROM Meal_Log WHERE log_date LIKE ?", (like,))
    used_items = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT item_name, display_order FROM Items_Master")
    order_map = dict(cursor.fetchall())
    items = sorted(used_items, key=lambda n: (order_map.get(n, 9999), n))

    cursor.execute("""
        SELECT e.name, m.item_name, SUM(m.quantity), SUM(m.quantity * m.locked_price)
        FROM Employees e JOIN Meal_Log m ON e.employee_id = m.employee_id
        WHERE m.log_date LIKE ? GROUP BY e.employee_id, m.item_name ORDER BY e.name ASC
    """, (like,))
    by_emp = {}
    for name, item, qty, amt in cursor.fetchall():
        rec = by_emp.setdefault(name, {"qty": {}, "amt": {}, "total": 0.0})
        rec["qty"][item] = qty
        rec["amt"][item] = amt or 0
        rec["total"] += (amt or 0)
    cursor.execute("SELECT COUNT(DISTINCT log_date) FROM Meal_Log WHERE log_date LIKE ?", (like,))
    working_days = cursor.fetchone()[0] or 0
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = f"Summary {target_month}"
    ncols = len(items) + 2
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="E0E7FF")
    total_fill = PatternFill("solid", fgColor="EEF2FF")

    ws.cell(row=1, column=1, value=f"Monthly Verification Summary - {display_month_name}").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value="Total Working Days (days with entries):").font = Font(bold=True)
    ws.cell(row=2, column=3, value=working_days).font = Font(bold=True)

    hdr_row = 4
    for c, title in enumerate(["Staff Name"] + items + ["Total Billed (Rs)"], start=1):
        cell = ws.cell(row=hdr_row, column=c, value=title)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center" if c > 1 else "left", vertical="center", wrap_text=True)

    r = hdr_row + 1
    for name, rec in by_emp.items():
        ws.cell(row=r, column=1, value=name).border = border
        for c, item in enumerate(items, start=2):
            q = rec["qty"].get(item, 0)
            cell = ws.cell(row=r, column=c, value=q if q > 0 else None)
            cell.alignment = Alignment(horizontal="center")
            cell.border = border
        t = ws.cell(row=r, column=ncols, value=round(rec["total"], 2))
        t.number_format = '#,##0.00'
        t.border = border
        r += 1

    first, last = hdr_row + 1, r - 1
    # Footer rows use live formulas so they stay correct if a cell is edited in Excel
    ws.cell(row=r, column=1, value=f"Total Qty ({len(by_emp)} staff)")
    ws.cell(row=r + 1, column=1, value="Total Amount (Rs)")
    for c, item in enumerate(items, start=2):
        L = get_column_letter(c)
        ws.cell(row=r, column=c, value=f"=SUM({L}{first}:{L}{last})" if by_emp else 0).alignment = Alignment(horizontal="center")
        tot_amt = sum(rec["amt"].get(item, 0) for rec in by_emp.values())
        a = ws.cell(row=r + 1, column=c, value=round(tot_amt, 2))
        a.number_format = '#,##0.00'
        a.alignment = Alignment(horizontal="center")
    L = get_column_letter(ncols)
    g2 = ws.cell(row=r + 1, column=ncols, value=f"=SUM({L}{first}:{L}{last})" if by_emp else 0)
    g2.number_format = '#,##0.00'
    for rr in (r, r + 1):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=rr, column=c)
            cell.font = Font(bold=True)
            cell.fill = total_fill
            cell.border = border

    ws.column_dimensions["A"].width = 30
    for c in range(2, ncols):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.column_dimensions[get_column_letter(ncols)].width = 18
    ws.freeze_panes = ws.cell(row=hdr_row + 1, column=2)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"Verification_Summary_{target_month}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- PHASE 3: WHATSAPP BILLING ENGINE ---
def _compute_weekly_confirmation_data(ref_date_str):
    """Shared by the page view and the PDF export so they can never drift apart."""
    if ref_date_str:
        ref_date = datetime.datetime.strptime(ref_date_str, '%Y-%m-%d').date()
    else:
        ref_date = datetime.date.today()

    week_monday = ref_date - datetime.timedelta(days=ref_date.weekday())
    # Only Mon-Fri -- canteen is closed Sat/Sun, nothing to confirm those days
    weekdays = [week_monday + datetime.timedelta(days=i) for i in range(5)]
    week_start_str = weekdays[0].strftime('%Y-%m-%d')
    week_end_str = weekdays[-1].strftime('%Y-%m-%d')

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("SELECT employee_id, name FROM Employees WHERE is_active = 1 ORDER BY name ASC")
    employees = cursor.fetchall()

    cursor.execute("""
        SELECT employee_id, log_date, item_name, quantity FROM Meal_Log
        WHERE log_date BETWEEN ? AND ?
        ORDER BY employee_id, log_date
    """, (week_start_str, week_end_str))
    rows = cursor.fetchall()
    conn.close()

    # emp_id -> {log_date -> [ (item_name, quantity), ... ]}
    activity = {}
    for emp_id, log_date, item_name, qty in rows:
        activity.setdefault(emp_id, {}).setdefault(log_date, []).append((item_name, qty))

    # Consistent item ordering (matches the rest of the app's tables) so the
    # same combination always reads the same way day to day.
    ITEM_ORDER = ["Breakfast", "Lunch", "Tea", "Snacks", "Omelette", "Egg"]
    def item_sort_key(pair):
        name = pair[0]
        return (ITEM_ORDER.index(name) if name in ITEM_ORDER else len(ITEM_ORDER), name)

    weekly_data = []
    for emp_id, name in employees:
        if emp_id not in activity:
            continue  # nobody to confirm for someone with zero activity that week
        days = []
        for d in weekdays:
            d_str = d.strftime('%Y-%m-%d')
            day_items = sorted(activity.get(emp_id, {}).get(d_str, []), key=item_sort_key)
            formatted = [f"{n} x{q}" if q and q > 1 else n for n, q in day_items]
            days.append({
                "label": d.strftime('%a %d'),
                "foods": ", ".join(formatted) if formatted else None,
            })
        weekly_data.append({"id": emp_id, "name": name, "days": days})

    display_range = f"{weekdays[0].strftime('%d-%m-%Y')} to {weekdays[-1].strftime('%d-%m-%Y')}"

    return {
        "weekly_data": weekly_data,
        "display_range": display_range,
        "weekdays": weekdays,
        "week_monday": week_monday,
    }

@app.route("/weekly-confirmation")
def weekly_confirmation():
    ctx = _compute_weekly_confirmation_data(request.args.get('week_start'))
    week_monday = ctx["week_monday"]
    return render_template(
        "weekly_confirmation.html",
        weekly_data=ctx["weekly_data"], display_range=ctx["display_range"],
        week_monday=week_monday.strftime('%Y-%m-%d'),
        prev_week=(week_monday - datetime.timedelta(days=7)).strftime('%Y-%m-%d'),
        next_week=(week_monday + datetime.timedelta(days=7)).strftime('%Y-%m-%d'),
    )

@app.route("/download-weekly-confirmation-pdf")
def download_weekly_confirmation_pdf():
    ctx = _compute_weekly_confirmation_data(request.args.get('week_start'))
    weekly_data = ctx["weekly_data"]
    display_range = ctx["display_range"].replace('–', '-')  # FPDF base font is latin-1 only
    weekdays = ctx["weekdays"]

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_margins(left=10, top=10, right=10)
    pdf.set_auto_page_break(False)

    name_w = 55
    day_w = (277 - name_w) / 5  # spread remaining width evenly across 5 weekdays
    line_height = 4.2
    page_bottom = 195  # leave room for the bottom margin on landscape A4

    def draw_header():
        pdf.set_fill_color(15, 60, 120)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Arial", 'B', 14)
        pdf.cell(0, 12, txt=" ESIC SUB-REGIONAL OFFICE, ERNAKULAM", ln=True, align='C', fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", 'B', 11)
        pdf.cell(0, 10, txt=f"WEEKLY CONSUMPTION CONFIRMATION ({display_range})", ln=True, align='C')
        pdf.set_font("Arial", '', 9)
        pdf.cell(0, 7, txt="Please verify your entries below and report any discrepancy to the canteen manager.", ln=True, align='C')

        pdf.ln(4)
        pdf.set_fill_color(210, 225, 245)
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(name_w, 10, txt="STAFF MEMBER", border=1, align='L', fill=True)
        for d in weekdays:
            pdf.cell(day_w, 10, txt=d.strftime('%a %d'), border=1, align='C', fill=True)
        pdf.ln()

    pdf.add_page()
    draw_header()

    pdf.set_font("Arial", '', 8)
    fill = False
    for emp in weekly_data:
        cell_texts = [(day['foods'] if day['foods'] else '-') for day in emp['days']]

        # Figure out how many lines each cell will need at this width/font,
        # then use the tallest one as the row height so every cell lines up.
        max_lines = 1
        for text in cell_texts:
            lines = pdf.multi_cell(day_w - 2, line_height, text, border=0, align='C', split_only=True)
            max_lines = max(max_lines, len(lines))
        row_height = max(10, max_lines * line_height + 2)

        # Page break if this row won't fit, repeating the header on the new page
        if pdf.get_y() + row_height > page_bottom:
            pdf.add_page()
            draw_header()
            pdf.set_font("Arial", '', 8)

        x0, y0 = pdf.get_x(), pdf.get_y()
        fill_rgb = (245, 245, 245) if fill else (255, 255, 255)
        pdf.set_fill_color(*fill_rgb)

        # Name cell
        pdf.rect(x0, y0, name_w, row_height, 'DF')
        pdf.set_xy(x0, y0 + (row_height - 4) / 2)
        pdf.set_font("Arial", '', 9)
        pdf.cell(name_w, 4, txt=f" {emp['name']}", align='L')

        # Day cells, vertically centered text, wrapped within the column
        cx = x0 + name_w
        pdf.set_font("Arial", '', 8)
        for text in cell_texts:
            pdf.rect(cx, y0, day_w, row_height, 'DF')
            lines = pdf.multi_cell(day_w - 2, line_height, text, border=0, align='C', split_only=True)
            text_height = len(lines) * line_height
            pdf.set_xy(cx, y0 + (row_height - text_height) / 2)
            pdf.multi_cell(day_w, line_height, text, border=0, align='C')
            cx += day_w

        pdf.set_xy(x0, y0 + row_height)
        fill = not fill

    if not weekly_data:
        pdf.set_font("Arial", 'I', 10)
        pdf.cell(0, 10, txt="No meal entries logged for this week.", border=1, align='C')

    pdf_path = f"Weekly_Confirmation_{ctx['week_monday'].strftime('%Y-%m-%d')}.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))

@app.route("/billing", methods=["GET"])
def billing():
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m')

    conn = connect_db()
    cursor = conn.cursor()

    # UPDATED QUERY: Now fetches lifetime meals and lifetime payments
    query = """
        SELECT 
            e.employee_id, 
            e.name, 
            e.opening_balance,
            (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id AND log_date < ?) as hist_meals,
            (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id AND payment_date < ?) as hist_paid,
            (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id AND log_date LIKE ?) as month_meals,
            (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id AND payment_date LIKE ?) as month_paid,
            (SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = e.employee_id) as lifetime_meals,
            (SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = e.employee_id) as lifetime_paid
        FROM Employees e 
        ORDER BY e.name ASC
    """
    
    first_day_of_month = f"{target_month}-01"
    cursor.execute(query, (first_day_of_month, first_day_of_month, f"{target_month}%", f"{target_month}%"))
    records = cursor.fetchall()

    billing_data = []
    for r in records:
        emp_id, name = r[0], r[1]
        opening_balance = r[2]
        
        # Monthly Snapshot Math
        brought_forward = opening_balance + r[3] - r[4]
        month_bill = r[5]
        month_paid = r[6]
        total_due = brought_forward + month_bill - month_paid
        
        # SMART LOGIC: Real-world balance checking across all time
        lifetime_meals = r[7]
        lifetime_paid = r[8]
        overall_real_world_due = opening_balance + lifetime_meals - lifetime_paid
        
        # If their absolute balance right now is 0 or negative (credit), they are settled
        is_settled = True if overall_real_world_due <= 0.05 else False

        # Skip people with absolutely zero debt and zero activity this month
        if brought_forward == 0 and month_bill == 0 and total_due == 0:
            continue 

        billing_data.append({
            "id": emp_id,
            "name": name,
            "brought_forward": brought_forward,
            "month_bill": month_bill,
            "month_paid": month_paid,
            "total_due": total_due,
            "is_settled": is_settled  # Sending the smart flag to HTML
        })

    display_month_name = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y')

    # Get available months for the dropdown
    cursor.execute('''
        SELECT DISTINCT strftime('%Y-%m', payment_date) FROM Payments
        UNION SELECT DISTINCT strftime('%Y-%m', log_date) FROM Meal_Log
        UNION SELECT ? ORDER BY 1 DESC
    ''', (target_month,))
    available_months = [row[0] for row in cursor.fetchall() if row[0]]
    
    conn.close()

    return render_template("billing.html", billing_data=billing_data, target_month=target_month, display_month_name=display_month_name, available_months=available_months)
@app.route("/download-slip/<int:emp_id>")
@app.route("/download-slip/<int:emp_id>")
def download_slip(emp_id):
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.date.today().strftime('%Y-%m') 
    display_month = datetime.datetime.strptime(target_month, '%Y-%m').strftime('%B %Y').upper()
    start_of_month = f"{target_month}-01"

    conn = connect_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name, department, opening_balance FROM Employees WHERE employee_id = ?", (emp_id,))
    emp = cursor.fetchone()
    if not emp:
        conn.close()
        return "Staff member not found", 404
    name, dept, opening_bal = emp
    
    cursor.execute("SELECT COALESCE(SUM(quantity * locked_price), 0) FROM Meal_Log WHERE employee_id = ? AND log_date < ?", (emp_id, start_of_month))
    past_meals = cursor.fetchone()[0] or 0.0
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = ? AND payment_date < ?", (emp_id, start_of_month))
    past_paid = cursor.fetchone()[0] or 0.0
    prev_balance = opening_bal + past_meals - past_paid
    
    cursor.execute("""
        SELECT item_name, SUM(quantity), locked_price, SUM(quantity * locked_price)
        FROM Meal_Log 
        WHERE employee_id = ? AND log_date LIKE ?
        GROUP BY item_name, locked_price
    """, (emp_id, f"{target_month}%"))
    meals = cursor.fetchall()

    # Work out the "next month" (e.g. target=July -> next=August), since
    # that is the month staff actually pay off THIS month's bill in.
    year, month = int(target_month.split('-')[0]), int(target_month.split('-')[1])
    if month == 12:
        next_month = f"{year + 1}-01"
    else:
        next_month = f"{year}-{month + 1:02d}"
    next_month_display = datetime.datetime.strptime(next_month, '%Y-%m').strftime('%B %Y').upper()

    # Check 1: was the PREVIOUS balance settled? -> look at payments made
    # DURING the target month itself (e.g. July payments clear June's bill).
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = ? AND payment_date LIKE ?", (emp_id, f"{target_month}%"))
    paid_toward_prev_balance = cursor.fetchone()[0] or 0.0

    # Check 2: was the CURRENT month's bill settled? -> look at payments
    # made DURING next month (e.g. August payments clear July's bill).
    cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM Payments WHERE employee_id = ? AND payment_date LIKE ?", (emp_id, f"{next_month}%"))
    paid_toward_current_bill = cursor.fetchone()[0] or 0.0
    conn.close()
    
    month_bill = sum([row[3] for row in meals])

    # Each balance is checked independently against its own settlement
    # window -- no cascading between them.
    remaining_prev_balance = prev_balance - paid_toward_prev_balance          # >0 still due, <0 credit, 0 settled
    remaining_current_bill = month_bill - paid_toward_current_bill            # >0 still due, <0 advance credit, 0 settled

    # Net the two buckets together so an existing credit actually offsets a
    # current due (and vice versa) instead of being discarded.
    overall_balance = remaining_prev_balance + remaining_current_bill
    net_due = max(overall_balance, 0.0)

    pdf = FPDF(orientation='P', unit='mm', format='A5')
    pdf.set_margins(left=10, top=10, right=10)
    pdf.add_page()
    
    pdf.set_fill_color(15, 60, 120) 
    pdf.set_text_color(255, 255, 255) 
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(0, 10, txt="ESIC SRO ERNAKULAM - TIFFIN ROOM", ln=True, align='C', fill=True)
    
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(0, 6, txt=f"STATEMENT FOR: {name.upper()}", ln=True)
    pdf.set_font("Arial", '', 9)
    pdf.cell(0, 5, txt=f"Department: {dept}", ln=True)
    pdf.cell(0, 5, txt=f"Billing Month: {display_month}", ln=True)
    pdf.cell(0, 5, txt=f"Generated Date: {datetime.date.today().strftime('%d-%m-%Y')}", ln=True)
    pdf.ln(4)
    
    pdf.set_fill_color(230, 235, 245)
    pdf.set_font("Arial", 'B', 9)
    pdf.cell(55, 7, txt="ITEM NAME", border=1, fill=True)
    pdf.cell(20, 7, txt="QTY", border=1, align='C', fill=True)
    pdf.cell(25, 7, txt="RATE (Rs)", border=1, align='R', fill=True)
    pdf.cell(28, 7, txt="TOTAL (Rs)", border=1, ln=True, align='R', fill=True)
    
    pdf.set_font("Arial", '', 9)
    for item, qty, price, total in meals:
        pdf.cell(55, 7, txt=f" {item}", border=1)
        pdf.cell(20, 7, txt=str(qty), border=1, align='C')
        pdf.cell(25, 7, txt=f"{price:.2f}", border=1, align='R')
        pdf.cell(28, 7, txt=f"{total:.2f}", border=1, ln=True, align='R')
        
    pdf.ln(4)
    pdf.set_font("Arial", 'B', 9)

    # Previous Balance / Carry Over -- checked against THIS month's payments
    if remaining_prev_balance > 0.005:
        prev_label = "Previous Balance / Carry Over (Due):"
        pdf.set_text_color(200, 0, 0)
    elif remaining_prev_balance < -0.005:
        prev_label = "Previous Balance / Carry Over (Credit):"
        pdf.set_text_color(0, 130, 0)
    else:
        prev_label = "Previous Balance / Carry Over (Settled):"
    pdf.cell(100, 6, txt=prev_label, align='R')
    pdf.cell(28, 6, txt=f"{'+' if remaining_prev_balance >= 0 else '-'}{abs(remaining_prev_balance):.2f}", border=1, ln=True, align='R')
    pdf.set_text_color(0, 0, 0)

    pdf.cell(100, 6, txt=f"Current Month Bill ({display_month}):", align='R')
    pdf.cell(28, 6, txt=f"{month_bill:.2f}", border=1, ln=True, align='R')

    # Current month's bill is checked against NEXT month's payments
    pdf.cell(100, 6, txt=f"Paid Towards This Bill (in {next_month_display}):", align='R')
    pdf.cell(28, 6, txt=f"{paid_toward_current_bill:.2f}", border=1, ln=True, align='R')

    if overall_balance < -0.005:
        pdf.set_text_color(0, 130, 0)
        pdf.cell(100, 6, txt="Advance Credit (Carried Forward):", align='R')
        pdf.cell(28, 6, txt=f"{abs(overall_balance):.2f}", border=1, ln=True, align='R')
        pdf.set_text_color(0, 0, 0)
    
    pdf.ln(2)
    pdf.set_font("Arial", 'B', 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(100, 8, txt="NET AMOUNT PAYABLE NOW (Rs):", align='R', fill=True)
    
    if net_due <= 0.005:
        pdf.set_text_color(0, 130, 0)
    else:
        pdf.set_text_color(200, 0, 0)
        
    pdf.cell(28, 8, txt=f"{net_due:.2f}", border=1, ln=True, align='R', fill=True)
    
    pdf_path = f"Slip_{name.replace(' ', '_')}_{target_month}.pdf"
    return send_file(io.BytesIO(bytes(pdf.output())), mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))
@app.route("/backup-db")
def backup_db():
    # Generates a name like: Tiffin_Backup_2026-07-12_05-30.db
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M')
    filename = f"Tiffin_Backup_{timestamp}.db"
    
    # Sends the actual active tiffin.db file as a download
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "backup.db")
        with connect_db() as source, sqlite3.connect(path) as destination:
            source.backup(destination)
        with open(path, "rb") as backup:
            payload = io.BytesIO(backup.read())
    return send_file(payload, mimetype="application/octet-stream", as_attachment=True, download_name=filename)

# --- NEW: Automatic backups on every app launch, keeping the last 10 ---
BACKUP_DIR = os.path.join(os.path.dirname(DB_FILE), "backups")
BACKUP_KEEP_COUNT = 10

def auto_backup_db():
    if not os.path.exists(DB_FILE):
        return  # nothing to back up yet (very first run)
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')
        backup_path = os.path.join(BACKUP_DIR, f"tiffin_auto_{timestamp}.db")

        with connect_db() as source, sqlite3.connect(backup_path) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")

        # Prune to the most recent BACKUP_KEEP_COUNT backups
        existing = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith("tiffin_auto_") and f.endswith(".db")]
        )
        while len(existing) > BACKUP_KEEP_COUNT:
            oldest = existing.pop(0)
            try:
                os.remove(os.path.join(BACKUP_DIR, oldest))
            except OSError:
                pass
    except Exception as e:
        raise RuntimeError("Backup failed. Startup stopped before changing the database.") from e

from features import register as register_features
register_features(app, connect_db, lambda: DB_FILE)

if __name__ == "__main__":
    auto_backup_db()
    init_db()
    
    # FlaskWebGUI uses Python 3.12 syntax in current releases.
    # Older Python installations can still use the same local app in a browser.
    if sys.version_info >= (3, 12):
        from flaskwebgui import FlaskUI
        FlaskUI(app=app, server="flask", port=5000, width=1400, height=900).run()
    else:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
