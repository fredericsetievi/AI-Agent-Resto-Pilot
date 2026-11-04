"""Inventory writes use the real isolated API/ledger; no external providers."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from conftest import login, login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT_TIMEZONE, Store


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / 'inventory.sqlite3', seed=False)


@pytest.fixture
def client(app):
    with TestClient(app, base_url='http://127.0.0.1', raise_server_exceptions=False) as c:
        login_owner(c)
        yield c


def post(client, path, payload, key=None):
    return client.post('/v1/inventory/' + path, json=payload, headers={'Idempotency-Key': key or uuid4().hex})


def event(client, kind='usage', amount='1', ingredient='beras', key=None, **changes):
    payload = {'kind': kind, 'note': 'Kitchen test', 'confirmed': True,
               'items': [{'ingredient_id': ingredient, 'quantity': amount}]}
    payload.update(changes)
    return post(client, 'events', payload, key)


def count(client, amount='10', ingredient='beras', expected='0'):
    return event(client, 'count', items=[{'ingredient_id': ingredient, 'quantity': amount, 'expected_quantity': expected}])


def stock(client, ingredient='beras'):
    return next(Decimal(i['quantity']) for i in client.get('/v1/overview').json()['data']['inventory'] if i['ingredient_id'] == ingredient)


def workspace(client):
    r = client.get('/v1/inventory/workspace')
    assert r.status_code == 200, r.text
    return r.json()['data']


def test_half_box_replay_and_movement_audit(client, app):
    assert count(client).status_code == 201
    key = uuid4().hex
    payload = {'kind': 'usage', 'note': 'Half a box used', 'confirmed': True,
               'items': [{'ingredient_id': 'beras', 'quantity': '0.5', 'pack_size': '12'}]}
    first = post(client, 'events', payload, key)
    assert first.status_code == 201, first.text
    assert post(client, 'events', payload, key).json() == first.json()
    assert stock(client) == 4
    assert first.json()['data']['items'][0] == {'ingredient_id': 'beras', 'quantity': '6', 'delta': '-6', 'before': '10', 'after': '4'}
    with app.state.store.transaction() as conn:
        rows = conn.execute('SELECT quantity FROM movements WHERE inventory_event_id=?', (first.json()['data']['id'],)).fetchall()
        assert [r[0] for r in rows] == ['-6']
        assert conn.execute("SELECT count(*) FROM activity WHERE event='inventory_usage'").fetchone()[0] == 1
    assert len(workspace(client)['events']) == 2
    payload['items'][0]['quantity'] = '0.25'
    assert post(client, 'events', payload, key).status_code == 409
    assert stock(client) == 4


def test_count_is_absolute_and_stale_count_rejected(client):
    assert count(client).status_code == 201
    assert event(client, amount='2').status_code == 201
    assert count(client, '7', expected='10').status_code == 409
    assert stock(client) == 8
    assert count(client, '7', expected='8').status_code == 201
    assert stock(client) == 7
    assert workspace(client)['events'][0]['items'][0]['delta'] == '-1'
    assert count(client, '0', expected='7').status_code == 201
    assert stock(client) == 0


def test_multiline_insufficient_stock_rolls_back_everything(client, app):
    count(client)
    before = workspace(client)['events']
    response = event(client, items=[{'ingredient_id': 'beras', 'quantity': '1'}, {'ingredient_id': 'telur', 'quantity': '1'}])
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'insufficient_stock'
    assert stock(client) == 10
    assert workspace(client)['events'] == before


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0', '1e1000', '0.00000000001'])
def test_invalid_quantities_do_not_write(client, value):
    count(client)
    response = event(client, amount=value)
    assert response.status_code == 422, response.text
    assert stock(client) == 10


def test_confirmation_duplicates_unknown_and_admin_guard(client, app):
    count(client)
    assert event(client, confirmed=False).status_code == 422
    assert event(client, ingredient='foreign-custom').status_code == 422
    assert event(client, items=[{'ingredient_id': 'beras', 'quantity': '1'}] * 2).status_code == 422
    assert event(client, revenue_idr=100).status_code == 422
    assert stock(client) == 10
    with TestClient(app, base_url='http://127.0.0.1') as admin:
        assert admin.get('/v1/inventory/workspace').status_code == 401
        login_admin(admin)
        assert event(admin).status_code == 403
        assert admin.get('/v1/inventory/workspace').status_code == 403


def test_recipe_sale_snapshot_and_unmapped_revenue(client):
    count(client)
    recipe_payload = {'name': 'Nasi', 'items': [{'ingredient_id': 'beras', 'quantity': '0.125'}]}
    r = post(client, 'recipes', recipe_payload)
    assert r.status_code == 200, r.text
    recipe = r.json()['data']
    sale = event(client, 'sale', items=[], recipe_id=recipe['id'], expected_recipe_version=recipe['version'], portions='8', revenue_idr=80000)
    assert sale.status_code == 201, sale.text
    assert stock(client) == 9
    changed = post(client, 'recipes', dict(recipe_payload, id=recipe['id'], items=[{'ingredient_id': 'beras', 'quantity': '0.25'}]))
    assert changed.status_code == 200
    stale = event(client, 'sale', items=[], recipe_id=recipe['id'], expected_recipe_version=recipe['version'], portions='8')
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'recipe_conflict'
    assert stock(client) == 9
    assert workspace(client)['events'][0]['recipe']['items'][0]['quantity'] == '0.125'
    assert event(client, 'sale', items=[], revenue_idr=20000, portions='2').status_code == 201
    assert event(client, 'sale', items=[], portions='3').status_code == 201
    assert stock(client) == 9
    assert event(client, 'sale', items=[]).status_code == 422


def test_other_tenant_cannot_use_recipe_and_keys_are_scoped(client, app):
    recipe = post(client, 'recipes', {'name': 'Private recipe', 'items': [{'ingredient_id': 'beras', 'quantity': '1'}]}).json()['data']
    with TestClient(app, base_url='http://127.0.0.1') as other:
        login_admin(other)
        r = other.post('/v1/admin/restaurants', headers={'Idempotency-Key': uuid4().hex}, json={'name': 'Other', 'owner_username': 'other', 'owner_password': 'otherpass'})
        assert r.status_code == 201, r.text
        login(other, 'other', 'otherpass')
        assert workspace(other)['recipes'] == []
        assert event(other, 'sale', items=[], recipe_id=recipe['id'], portions='1').status_code == 404
        payload = {'kind': 'sale', 'note': 'Cash sales', 'items': [], 'confirmed': True, 'revenue_idr': 100}
        a = post(client, 'events', payload, 'same-tenant-independent-key')
        b = post(other, 'events', payload, 'same-tenant-independent-key')
        assert a.status_code == b.status_code == 201
        assert a.json()['data']['id'] != b.json()['data']['id']


def test_concurrent_stock_reductions_and_same_key(app, client):
    count(client)
    store = app.state.store
    payload = {'kind': 'usage', 'note': 'Concurrent', 'confirmed': True, 'items': [{'ingredient_id': 'beras', 'quantity': '1'}]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.record_inventory_event('same-key', payload, 'demo-resto'), range(4)))
    assert len({r['id'] for r in results}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.record_inventory_event(uuid4().hex, payload, 'demo-resto'), range(4)))
    assert stock(client) == 5


def test_manual_forecast_and_no_false_history(client):
    count(client)
    event(client, amount='1')
    assert all(f['status'] == 'insufficient_history' for f in workspace(client)['forecasts'])
    r = post(client, 'settings', {'lead_days': 2, 'safety_days': 1, 'daily_usage': {'beras': '2'}})
    assert r.status_code == 200, r.text
    f = next(f for f in workspace(client)['forecasts'] if f['ingredient_id'] == 'beras')
    assert Decimal(f['days_left']) == Decimal('4.5')
    assert Decimal(f['reorder_in_days']) == Decimal('1.5')
    assert f['source'] == 'manual'
    event(client, amount='4')
    f = next(f for f in workspace(client)['forecasts'] if f['ingredient_id'] == 'beras')
    assert f['status'] == 'reorder_now'
    assert Decimal(f['reorder_in_days']) == 0


def test_complete_history_uses_all_calendar_days_and_excludes_counts(client, app):
    count(client, '30')
    usage = event(client, amount='6').json()['data']
    correction = count(client, '12', expected='24').json()['data']
    today = datetime.now(RESTAURANT_TIMEZONE).date()
    start, end = today - timedelta(days=3), today - timedelta(days=1)
    stamp = datetime.combine(start, datetime.min.time(), RESTAURANT_TIMEZONE).astimezone(timezone.utc).isoformat()
    with app.state.store.transaction() as conn:
        for entry in (usage, correction):
            entry['created_at'] = stamp
            entry.pop('business_date', None)  # Legacy entries derive their Jakarta business date from posting time.
            conn.execute('UPDATE inventory_events SET body=?,created_at=? WHERE id=?', (json.dumps(entry), stamp, entry['id']))
    settings = {'lead_days': 2, 'safety_days': 1, 'daily_usage': {}, 'history_start': str(start), 'history_end': str(end), 'history_complete': True}
    r = post(client, 'settings', settings)
    assert r.status_code == 200, r.text
    f = next(f for f in workspace(client)['forecasts'] if f['ingredient_id'] == 'beras')
    assert f['source'] == 'recorded' and f['evidence_days'] == 3
    assert Decimal(f['daily_usage']) == 2
    assert Decimal(f['days_left']) == 6
    assert post(client, 'settings', dict(settings, history_complete=False)).status_code == 422
    assert post(client, 'settings', dict(settings, history_end=str(today + timedelta(days=1)))).status_code == 422


def test_restart_retains_events_stock_and_recipes(app, client):
    count(client)
    event(client, 'waste', amount='0.5')
    post(client, 'recipes', {'name': 'Nasi', 'items': [{'ingredient_id': 'beras', 'quantity': '0.1'}]})
    reopened = Store(app.state.store.path, seed=False)
    state = reopened.inventory_workspace('demo-resto')
    assert len(state['events']) == 2 and len(state['recipes']) == 1
    assert next(i['quantity'] for i in reopened.overview('demo-resto')['inventory'] if i['ingredient_id'] == 'beras') == '9.5'
