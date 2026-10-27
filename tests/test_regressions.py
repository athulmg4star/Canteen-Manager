import importlib.util
from pathlib import Path
import sqlite3
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('tiffin_v3', ROOT / 'app.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.app.template_folder = str(ROOT / "templates")
module.app.static_folder = str(ROOT / "static")

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'DB_FILE', str(tmp_path / 'test.db'))
    monkeypatch.setattr(module, 'BACKUP_DIR', str(tmp_path / 'backups'))
    module.app.config.update(TESTING=True)
    module.init_db()
    return module

def post(app, client, path, data, token=None):
    import secrets
    with client.session_transaction() as session:
        session['csrf_token'] = 'test-csrf'
    return client.post(path, data=dict(data, csrf_token='test-csrf', submission_token=token or secrets.token_hex(24)))

def test_fresh_and_repeat_initialization(app):
    app.init_db()
    with sqlite3.connect(app.DB_FILE) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM Employees').fetchone()[0] == 6

@pytest.mark.parametrize('path', ['/', '/roster', '/bulk-log', '/log-meals', '/history', '/billing', '/payments', '/purchases', '/consumption', '/staff-loans', '/manager-fund', '/weekly-confirmation', '/recycle-bin', '/settings', '/employee/1'])
def test_pages_render(app, path):
    assert app.app.test_client().get(path).status_code == 200

def test_old_price_preserved_and_retry_rejected(app):
    c=app.app.test_client()
    with sqlite3.connect(app.DB_FILE) as db:
        db.execute("INSERT INTO Meal_Log_Raw (employee_id,item_name,quantity,locked_price,log_date) VALUES (1,'Tea',1,5,'2026-09-01')")
        db.execute("UPDATE Items_Master SET current_price=15 WHERE item_name='Tea'")
    data={'log_date':'2026-09-01','qty_1_Tea':'3'}
    assert post(app,c,'/bulk-log',data,'a'*48).status_code==303
    assert post(app,c,'/bulk-log',data,'a'*48).status_code==409
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT quantity,locked_price FROM Meal_Log').fetchall()==[(3,5)]

@pytest.mark.parametrize('amount', ['-1','nan','inf','bad','0'])
def test_invalid_payment_rejected(app,amount):
    r=post(app,app.app.test_client(),'/payments',{'employee_id':'1','amount':amount,'payment_date':'2026-10-01'})
    assert r.status_code==400
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==0

def test_employee_history_protected(app):
    c=app.app.test_client()
    post(app,c,'/payments',{'employee_id':'1','amount':'100','payment_date':'2026-10-01'})
    assert post(app,c,'/hard-delete',{'emp_id':'1'}).status_code==409

def test_loan_lifecycle(app):
    c=app.app.test_client()
    post(app,c,'/staff-loans',{'employee_id':'1','amount':'100','loan_date':'2026-10-01','repay_method':'bill_credit'})
    assert post(app,c,'/edit-payment',{'payment_id':'1','new_amount':'200'}).status_code==409
    assert post(app,c,'/delete-payment',{'payment_id':'1'}).status_code==409
    post(app,c,'/delete-loan',{'loan_id':'1'})
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==0
    assert post(app,c,'/restore-item',{'table':'Payments','item_id':'1'}).status_code==409
    post(app,c,'/restore-item',{'table':'Staff_Loans','item_id':'1'})
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT amount FROM Payments').fetchone()[0]==100
    post(app,c,'/delete-loan',{'loan_id':'1'})
    post(app,c,'/purge-item',{'table':'Staff_Loans','item_id':'1'})
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments_Raw').fetchone()[0]==0

def test_backup_restores(app,tmp_path):
    r=app.app.test_client().get('/backup-db')
    assert r.status_code==200
    backup=tmp_path/'restored.db';backup.write_bytes(r.data)
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT COUNT(*) FROM Employees').fetchone()[0]==6

def test_csrf_required(app):
    assert app.app.test_client().post('/hard-delete',data={'emp_id':'1'}).status_code==400

def test_manager_fund_negative_entry_preserved(app):
    r=post(app,app.app.test_client(),'/manager-fund',{'entry_date':'2026-10-01','amount':'-100','remarks':'Cash advanced'})
    assert r.status_code==302
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT amount FROM Manager_Fund_Ledger').fetchone()[0]==-100

@pytest.mark.parametrize('path', ['/download-pdf?month=2026-10','/download-balance-sheet?month=2026-10','/download-payments-report-pdf?month=2026-10','/download-manager-fund-pdf?month=2026-10','/download-weekly-confirmation-pdf','/download-slip/1?month=2026-10','/download-excel?month=2026-10','/export-expenses?month=2026-10','/export-payments?month=2026-10'])
def test_reports(app,path):
    r=app.app.test_client().get(path)
    assert r.status_code==200
    assert r.data.startswith(b'%PDF') or r.data.startswith(b'PK')

def test_existing_copy_migration_keeps_active_accounting(tmp_path,monkeypatch):
    target=tmp_path/'existing.db'
    with sqlite3.connect(f'file:{ROOT / "tiffin.db"}?mode=ro',uri=True) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    monkeypatch.setattr(module,'DB_FILE',str(target))
    queries=['SELECT employee_id,opening_balance FROM Employees ORDER BY employee_id','SELECT employee_id,item_name,quantity,locked_price,log_date FROM Meal_Log ORDER BY log_id','SELECT employee_id,amount,payment_date FROM Payments ORDER BY payment_id','SELECT item_category,amount,purchase_date FROM Purchases ORDER BY purchase_id']
    with sqlite3.connect(target) as db:
        before=[db.execute(q).fetchall() for q in queries]
    module.init_db()
    with sqlite3.connect(target) as db:
        assert [db.execute(q).fetchall() for q in queries]==before
    c=module.app.test_client()
    with sqlite3.connect(target) as db:
        emp_id=db.execute('SELECT employee_id FROM Employees LIMIT 1').fetchone()[0]
    for path in ['/','/billing?month=2026-10','/history?month=2026-10','/payments','/purchases','/download-pdf?month=2026-10',f'/download-slip/{emp_id}?month=2026-10','/export-verification-summary?month=2026-10']:
        assert c.get(path).status_code==200, path

def test_bulk_grid_returns_historical_rate(app):
    with sqlite3.connect(app.DB_FILE) as db:
        db.execute("INSERT INTO Meal_Log_Raw (employee_id,item_name,quantity,locked_price,log_date) VALUES (1,'Tea',2,5,'2026-09-01')")
    data=app.app.test_client().get('/api/bulk-log-existing?date=2026-09-01&include_prices=1').get_json()
    assert data['1']['Tea']=={'quantity':2,'price':5}

def test_bulk_restore_parent_and_child_together(app):
    c=app.app.test_client()
    post(app,c,'/staff-loans',{'employee_id':'1','amount':'100','loan_date':'2026-10-01','repay_method':'bill_credit'})
    post(app,c,'/delete-loan',{'loan_id':'1'})
    assert post(app,c,'/bulk-restore-items',{'selected_items':['Payments|1','Staff_Loans|1']}).status_code==302
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM Staff_Loans').fetchone()[0]==1

def test_bad_month_and_missing_payment_date(app):
    c=app.app.test_client()
    assert c.get('/billing?month=invalid').status_code==400
    assert post(app,c,'/payments',{'employee_id':'1','amount':'100'}).status_code==400
