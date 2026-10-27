import sqlite3
import csv
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "tiffin.db")
CSV_FILE = os.path.join(BASE_DIR, "staff.csv")

def import_employees():
    # 1. Check if the CSV exists
    if not os.path.exists(CSV_FILE):
        print(f"Error: Could not find {CSV_FILE} in this folder.")
        return

    # 2. Connect to your database
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    added_count = 0

    # 3. Open and read the CSV
    with open(CSV_FILE, mode='r', encoding='utf-8') as file:
        csv_reader = csv.reader(file)
        header = next(csv_reader) # Skips the first row (Name, Department, Balance)
        
        for row in csv_reader:
            if len(row) >= 2:
                name = row[0].strip()
                department = row[1].strip()
                # Defaults to 0.0 if the balance column is empty
                try:
                    balance = float(row[2].strip()) if len(row) > 2 and row[2].strip() else 0.0
                except ValueError:
                    balance = 0.0
                
                # Re-running the importer must not duplicate the same roster entry.
                if cursor.execute("SELECT 1 FROM Employees WHERE name = ? AND department = ?", (name, department)).fetchone():
                    continue
                # 4. Inject into the database
                cursor.execute("""
                    INSERT INTO Employees (name, department, is_active, opening_balance) 
                    VALUES (?, ?, 1, ?)
                """, (name, department, balance))
                added_count += 1

    # 5. Save the changes
    conn.commit()
    conn.close()
    
    print(f"Success! {added_count} employees have been injected into the database.")

if __name__ == "__main__":
    import_employees()