import io
import json
import sqlite3
from pathlib import Path
import pytest
from test_regressions import app, post

@pytest.mark.parametrize('path',['/','/overview','/classic-dashboard','/calendar','/inventory','/closing?month=2020-01','/timeline/1','/audit','/reports','/preferences','/backups','/api/overview','/api/commands','/api/drilldown'])
def test_feature_pages(app,path):
    assert app.app.test_client().get(path).status_code==200


def test_inventory_flow_and_nonnegative_stock(app):
    c=app.app.test_client()
    assert post(app,c,'/inventory',{'action':'item','name':'Rice','unit':'kg','reorder_level':'3'}).status_code==302
    assert post(app,c,'/inventory',{'item_id':'1','kind':'received','quantity':'10','movement_date':'2020-01-01'}).status_code==302
    assert post(app,c,'/inventory',{'item_id':'1','kind':'used','quantity':'7','movement_date':'2020-01-02'}).status_code==302
    assert post(app,c,'/inventory',{'item_id':'1','kind':'wasted','quantity':'4','movement_date':'2020-01-03'}).status_code==409
    assert c.get('/api/overview?month=2020-01').get_json()['low_stock'][0]['balance']==3
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Purchases').fetchone()[0]==0


def close_month(app,c,month='2020-01',counted='0'):
    return post(app,c,'/closing',dict(month=month,note='Verified against source records',counted_balance=counted,review_days='yes',review_dues='yes',review_reports='yes'))


def test_closing_reconciliation_and_backdated_guards(app):
    c=app.app.test_client()
    assert close_month(app,c,counted='2').status_code==409
    assert close_month(app,c).status_code==302
    for date in ['2020-01-10','2019-12-31']:
        assert post(app,c,'/payments',dict(employee_id='1',amount='10',payment_date=date)).status_code==409
    assert post(app,c,'/update-global-balance',{'canteen_opening_balance':'100'}).status_code==409
    assert post(app,c,'/closing',{'month':'2020-01','action':'reopen','note':'Correct missed receipt'}).status_code==302
    assert post(app,c,'/payments',dict(employee_id='1',amount='10',payment_date='2020-01-10')).status_code==303


def test_closed_updates_deletes_and_move_date(app):
    c=app.app.test_client()
    post(app,c,'/log-meals',{'employee_id':'1','log_date':'2020-01-02','qty_Tea':'1'})
    assert close_month(app,c).status_code==302
    assert post(app,c,'/edit-log',{'log_id':'1','new_qty':'2','new_date':'2020-02-02'}).status_code==409
    assert post(app,c,'/history',{'delete_id':'1'}).status_code==409
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT quantity,log_date FROM Meal_Log').fetchone()==(1,'2020-01-02')


def test_audit_undo_and_repeated_undo(app):
    c=app.app.test_client()
    post(app,c,'/payments',dict(employee_id='1',amount='100',payment_date='2020-01-10'))
    with sqlite3.connect(app.DB_FILE) as db:
        event=db.execute("SELECT event_id FROM Audit_Events WHERE table_name='Payments_Raw' ORDER BY event_id DESC").fetchone()[0]
    assert post(app,c,'/audit/undo',{'event_id':str(event),'reason':'Duplicate manual entry'}).status_code==302
    assert post(app,c,'/audit/undo',{'event_id':str(event),'reason':'Again'}).status_code==409
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM Payments_Raw').fetchone()[0]==1


def test_undo_rejects_stale_event_and_linked_payment(app):
    c=app.app.test_client()
    post(app,c,'/payments',dict(employee_id='1',amount='100',payment_date='2020-01-10'))
    with sqlite3.connect(app.DB_FILE) as db:event=db.execute('SELECT MAX(event_id) FROM Audit_Events').fetchone()[0]
    post(app,c,'/edit-payment',dict(payment_id='1',new_amount='120'))
    assert post(app,c,'/audit/undo',dict(event_id=str(event),reason='Old event')).status_code==409
    post(app,c,'/staff-loans',dict(employee_id='1',amount='50',loan_date='2020-01-10',repay_method='bill_credit'))
    with sqlite3.connect(app.DB_FILE) as db:event=db.execute("SELECT MAX(event_id) FROM Audit_Events WHERE table_name='Payments_Raw'").fetchone()[0]
    assert post(app,c,'/audit/undo',dict(event_id=str(event),reason='Linked credit')).status_code==409


def test_calendar_marks_holiday(app):
    c=app.app.test_client()
    assert post(app,c,'/calendar',{'day':'2020-01-01','status':'closed','note':'Holiday'}).status_code==302
    data=c.get('/api/overview?month=2020-01').get_json()
    assert data['days'][0]['expected'] is False
    assert data['days'][0]['missing'] is False


@pytest.mark.parametrize('format,magic',[('csv',b'\xef\xbb\xbf'),('xlsx',b'PK'),('pdf',b'%PDF')])
def test_filtered_reports(app,format,magic):
    c=app.app.test_client()
    post(app,c,'/payments',dict(employee_id='1',amount='100',payment_date='2020-01-10'))
    post(app,c,'/payments',dict(employee_id='2',amount='200',payment_date='2020-01-10'))
    r=c.get('/reports?kind=payments&month=2020-01&staff=1&format='+format)
    assert r.status_code==200
    assert r.data.startswith(magic)
    if format=='csv':
        assert b'100.0' in r.data and b'200.0' not in r.data


def test_timeline_and_overview_match_ledger(app):
    c=app.app.test_client()
    post(app,c,'/log-meals',dict(employee_id='1',log_date='2020-01-01',qty_Tea='2'))
    post(app,c,'/payments',dict(employee_id='1',amount='10',payment_date='2020-01-01'))
    data=c.get('/api/overview?month=2020-01').get_json()
    assert data['totals']['meals']==16
    assert data['totals']['closing']==10
    assert b'6.00' in c.get('/timeline/1').data


def test_verified_backup_restore_and_safety_copy(app):
    c=app.app.test_client()
    post(app,c,'/backups',{})
    directory=Path(app.DB_FILE).parent/'backups'
    backup=next(directory.glob('verified_*.db'))
    import hashlib
    digest=hashlib.sha256(backup.read_bytes()).hexdigest()
    post(app,c,'/payments',dict(employee_id='1',amount='100',payment_date='2020-01-10'))
    assert c.get('/backups?selected='+backup.name).status_code==200
    assert post(app,c,'/backups/restore',dict(name=backup.name,sha256='wrong',confirmation='RESTORE')).status_code==409
    assert post(app,c,'/backups/restore',dict(name=backup.name,sha256=digest,confirmation='RESTORE')).status_code==302
    with sqlite3.connect(app.DB_FILE) as db:
        assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM Audit_Events WHERE action='RESTORE'").fetchone()[0]==1
    safety=next(directory.glob('before_restore_*.db'))
    with sqlite3.connect(safety) as db:assert db.execute('SELECT COUNT(*) FROM Payments').fetchone()[0]==1


def test_startup_preserves_closed_recycle_rows(app):
    c=app.app.test_client()
    post(app,c,'/payments',dict(employee_id='1',amount='10',payment_date='2020-01-01'))
    post(app,c,'/delete-payment',dict(payment_id='1'))
    with sqlite3.connect(app.DB_FILE) as db:db.execute("UPDATE Payments_Raw SET deleted_at='2020-01-01 00:00:00' WHERE payment_id=1")
    close_month(app,c)
    app.init_db()
    with sqlite3.connect(app.DB_FILE) as db:assert db.execute('SELECT COUNT(*) FROM Payments_Raw').fetchone()[0]==1

def test_stock_cannot_use_future_receipt(app):
    c=app.app.test_client()
    post(app,c,'/inventory',dict(action='item',name='Milk',unit='litre',reorder_level='2'))
    post(app,c,'/inventory',dict(item_id='1',kind='received',quantity='10',movement_date='2020-01-10'))
    assert post(app,c,'/inventory',dict(item_id='1',kind='used',quantity='1',movement_date='2020-01-09')).status_code==409
    assert post(app,c,'/inventory',dict(item_id='1',kind='used',quantity='9',movement_date='2020-01-11')).status_code==302
    assert post(app,c,'/inventory',dict(item_id='1',kind='used',quantity='2',movement_date='2020-01-10')).status_code==409


def test_closed_month_blocks_new_opening_balance(app):
    c=app.app.test_client();close_month(app,c)
    assert post(app,c,'/add-employee',dict(name='New employee',opening_balance='50')).status_code==409


def test_failed_bulk_change_rolls_back_and_releases_token(app):
    c=app.app.test_client()
    post(app,c,'/payments',dict(employee_id='1',amount='10',payment_date='2020-01-10'))
    post(app,c,'/payments',dict(employee_id='1',amount='20',payment_date='2020-02-10'))
    close_month(app,c,counted='10')
    token='c'*48
    assert post(app,c,'/delete-payment',dict(payment_id='1'),token).status_code==409
    post(app,c,'/closing',dict(month='2020-01',action='reopen',note='Correct payment'))
    assert post(app,c,'/delete-payment',dict(payment_id='1'),token).status_code==302
