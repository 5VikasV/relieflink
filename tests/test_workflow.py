"""Behavior-level checks shared by SQLite and PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


PASSWORD = 'ReliefLinkDemo!'
ITEMS = ('food', 'water', 'medical')


@pytest.fixture
def app(db_target):
    return create_app(db_target)


def client_for(app):
    # Some restricted runners suppress cross-thread socket wakeups. A periodic
    # timer keeps the test portal responsive without changing application code.
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(.02, tick)
        loop.call_soon(tick)
        return loop
    return TestClient(app, backend_options={'loop_factory': loop_factory})


def login(client, role='coordinator'):
    response = client.post('/api/auth/login', json={
        'email': f'{"field" if role == "field_worker" else "coordinator"}@relieflink.local',
        'password': PASSWORD,
    })
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response.json()['user']


def state(client):
    response = client.get('/api/state')
    assert response.status_code == 200, response.text
    return response.json()


def generate(client):
    response = client.post('/api/plans/generate', json={})
    assert response.status_code in (200, 201), response.text
    return response.json()


def approve(client, plan):
    response = client.post(f'/api/plans/{plan["id"]}/approve', json={})
    assert response.status_code == 200, response.text
    return response


def test_authentication_csrf_and_field_permissions(app):
    with client_for(app) as client:
        assert client.get('/api/state').status_code == 401
        assert client.get('/api/auth/me').status_code == 401
        bad = client.post('/api/auth/login', json={'email': 'coordinator@relieflink.local', 'password': 'wrong'})
        assert bad.status_code == 401
        user = login(client, 'field_worker')
        assert user['role'] == 'field_worker'
        assert client.post('/api/plans/generate', json={}).status_code == 403
        assert client.post('/api/warehouses', json={'name': 'Forbidden', 'lat': 12.9, 'lng': 77.6, 'stock': dict.fromkeys(ITEMS, 1)}).status_code == 403
        client.headers.pop('X-CSRF-Token')
        assert client.post('/api/requests', json={'location_id': state(client)['locations'][0]['id'], 'food': 10, 'water': 10, 'medical': 10}).status_code == 403


def test_https_sessions_and_origin_checks(app):
    with client_for(app) as client:
        client.base_url = 'https://relieflink.example'
        response = client.post('/api/auth/login', json={
            'email': 'coordinator@relieflink.local', 'password': PASSWORD,
        }, headers={'Origin': 'https://relieflink.example'})
        assert response.status_code == 200, response.text
        cookie = response.headers['set-cookie'].lower()
        assert 'secure' in cookie and 'httponly' in cookie and 'samesite=lax' in cookie
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        current = state(client)
        assert current['storage'] in ('sqlite', 'postgresql')
        payload = {'location_id': current['locations'][0]['id'], 'food': 500, 'water': 800, 'medical': 100}
        assert client.post('/api/requests', json=payload, headers={'Origin': 'https://unrelated.example'}).status_code == 403
        assert client.post('/api/requests', json=payload, headers={'Origin': 'https://relieflink.example'}).status_code == 200


def test_demo_and_revised_needs_survive_restart(app):
    with client_for(app) as client:
        login(client)
        initial = state(client)
        assert (len(initial['locations']), len(initial['warehouses']), len(initial['vehicles'])) == (5, 3, 3)
        location = initial['locations'][0]
        quantities = {item: location['demands'][item] + 13 for item in ITEMS}
        response = client.post('/api/requests', json={'location_id': location['id'], **quantities, 'notes': 'Confirmed field survey'})
        assert response.status_code == 200, response.text
        response = client.post('/api/requests', json={'location_id': location['id'], **quantities, 'notes': 'Same figures reconfirmed'})
        assert response.status_code == 200, response.text
        assert next(x for x in state(client)['locations'] if x['id'] == location['id'])['demands'] == quantities
    with client_for(create_app(app.state.db_path)) as reopened:
        login(reopened)
        persisted = next(x for x in state(reopened)['locations'] if x['id'] == location['id'])
        assert persisted['demands'] == quantities


def test_zero_available_fleet_produces_no_allocations(app):
    with client_for(app) as client:
        login(client)
        for vehicle in state(client)['vehicles']:
            response = client.patch(f'/api/vehicles/{vehicle["id"]}', json={'status': 'maintenance'})
            assert response.status_code == 200, response.text
        result = generate(client)['result']
        assert result['routes'] == []
        assert result['total_kg'] == 0
        assert all(result['total_allocated'][item] == 0 for item in ITEMS)


def test_stale_plan_cannot_reserve_changed_inventory(app):
    with client_for(app) as client:
        login(client)
        plan = generate(client)
        depot = state(client)['warehouses'][0]
        assert client.post(f'/api/warehouses/{depot["id"]}/restock', json={'food': 1, 'water': 0, 'medical': 0}).status_code == 200
        rejected = client.post(f'/api/plans/{plan["id"]}/approve', json={})
        assert rejected.status_code == 409, rejected.text
        assert all(not any(w['reserved'].values()) for w in state(client)['warehouses'])


def test_approval_reserves_and_cancellation_releases(app):
    with client_for(app) as client:
        login(client)
        original = state(client)
        plan = generate(client)
        assert plan['result']['routes']
        approve(client, plan)
        approved = state(client)
        assert sum(sum(w['reserved'].values()) for w in approved['warehouses']) > 0
        assert client.post(f'/api/plans/{plan["id"]}/cancel', json={}).status_code == 200
        after = state(client)
        assert all(not any(w['reserved'].values()) for w in after['warehouses'])
        assert {w['id']: w['stock'] for w in after['warehouses']} == {w['id']: w['stock'] for w in original['warehouses']}
        assert not any(d['status'] in ('reserved', 'dispatched') for d in after['deliveries'])


def test_delivery_partial_receipt_is_counted_exactly_once(app):
    with client_for(app) as client:
        login(client)
        plan = generate(client)
        approve(client, plan)
        delivery = state(client)['deliveries'][0]
        location_before = next(c for c in state(client)['locations'] if c['id'] == delivery['location_id'])
        response = client.post(f'/api/deliveries/{delivery["id"]}/dispatch', json={})
        assert response.status_code == 200, response.text
        # Overstated receipts cannot be accepted.
        too_many = {item: delivery['manifest'][item] + 1 for item in ITEMS}
        assert client.post(f'/api/deliveries/{delivery["id"]}/verify', json={**too_many, 'recipient': 'Field Lead'}).status_code in (400, 409, 422)
        actual = dict(delivery['manifest'])
        missing_item = next(item for item in ITEMS if actual[item] > 0)
        actual[missing_item] -= 1
        payload = {**actual, 'recipient': 'Field Lead', 'notes': 'One unit damaged in transit'}
        response = client.post(f'/api/deliveries/{delivery["id"]}/verify', json=payload)
        assert response.status_code == 200, response.text
        after = state(client)
        loc = next(c for c in after['locations'] if c['id'] == delivery['location_id'])
        assert loc['received'] == {item: location_before['received'][item] + actual[item] for item in ITEMS}
        assert client.post(f'/api/deliveries/{delivery["id"]}/verify', json=payload).status_code in (200, 409)
        assert next(c for c in state(client)['locations'] if c['id'] == delivery['location_id'])['received'] == loc['received']
        assert client.post(f'/api/plans/{plan["id"]}/cancel', json={}).status_code == 200
        assert next(d for d in state(client)['deliveries'] if d['id'] == delivery['id'])['status'] == 'verified'


def test_competing_approvals_cannot_reserve_the_same_snapshot_twice(app):
    with client_for(app) as client:
        login(client)
        first, second = generate(client), generate(client)
        assert first['input_version'] == second['input_version']
        cookies = dict(client.cookies)
        csrf = client.headers['X-CSRF-Token']

        def perform(plan):
            with client_for(app) as other:
                other.cookies.update(cookies)
                return other.post(f'/api/plans/{plan["id"]}/approve', json={}, headers={'X-CSRF-Token': csrf}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(perform, (first, second)))
        assert sorted(codes) == [200, 409], codes
        snapshot = state(client)
        for warehouse in snapshot['warehouses']:
            assert all(0 <= warehouse['reserved'][item] <= warehouse['stock'][item] for item in ITEMS)


def test_invalid_quantities_and_coordinates_are_rejected(app):
    with client_for(app) as client:
        login(client)
        location = state(client)['locations'][0]
        assert client.post('/api/requests', json={'location_id': location['id'], 'food': -1, 'water': 0, 'medical': 0}).status_code == 422
        assert client.post('/api/locations', json={'name': 'Invalid latitude', 'lat': 100, 'lng': 77, 'population': 1, 'injured': 0, 'severity': 'low', 'demands': dict.fromkeys(ITEMS, 0)}).status_code == 422


def test_exports_are_real_and_unknown_api_paths_are_not_spa_html(app):
    with client_for(app) as client:
        login(client)
        response = client.get('/api/export/needs.csv')
        assert response.status_code == 200
        assert 'text/csv' in response.headers['content-type']
        assert state(client)['locations'][0]['name'] in response.text
        unknown = client.get('/api/does-not-exist')
        assert unknown.status_code == 404
        assert 'application/json' in unknown.headers['content-type']


def test_road_change_does_not_trap_remaining_reservations(app):
    with client_for(app) as client:
        login(client)
        plan = generate(client)
        approve(client, plan)
        deliveries = state(client)['deliveries']
        first = deliveries[0]
        assert client.post(f'/api/deliveries/{first["id"]}/dispatch', json={}).status_code == 200
        remaining_delivery = next(d for d in deliveries if d['vehicle_id'] != first['vehicle_id'])
        target = remaining_delivery['location_id']
        for road in state(client)['roads']:
            if target in (road['from_id'], road['to_id']):
                assert client.patch(f'/api/roads/{road["id"]}', json={'blocked': True}).status_code == 200
        rejected = client.post(f'/api/deliveries/{remaining_delivery["id"]}/dispatch', json={})
        assert rejected.status_code == 409, rejected.text
        # Cancel only the tours which have not left; keep the dispatched history.
        assert client.post(f'/api/plans/{plan["id"]}/cancel', json={}).status_code == 200
        snapshot = state(client)
        assert all(not any(w['reserved'].values()) for w in snapshot['warehouses'])
        assert next(d for d in snapshot['deliveries'] if d['id'] == first['id'])['status'] == 'dispatched'
        assert not any(d['status'] == 'reserved' for d in snapshot['deliveries'])


def test_resource_creation_settings_and_explicit_reset(app):
    with client_for(app) as client:
        login(client)
        response = client.post('/api/locations', json={'name': 'Test community', 'lat': 12.98, 'lng': 77.61, 'population': 70, 'injured': 2, 'severity': 'moderate', 'demands': {'food': 70, 'water': 140, 'medical': 2}})
        assert response.status_code in (200, 201), response.text
        new_location = next(c for c in state(client)['locations'] if c['name'] == 'Test community')
        assert sum(new_location['id'] in (r['from_id'], r['to_id']) for r in state(client)['roads']) >= 2
        response = client.post('/api/warehouses', json={'name': 'Test depot', 'lat': 12.97, 'lng': 77.60, 'stock': {'food': 50, 'water': 50, 'medical': 5}})
        assert response.status_code in (200, 201), response.text
        depot = next(w for w in state(client)['warehouses'] if w['name'] == 'Test depot')
        response = client.post('/api/vehicles', json={'name': 'Test vehicle', 'warehouse_id': depot['id'], 'capacity_kg': 150})
        assert response.status_code in (200, 201), response.text
        coefficients = {'food_per_person': 1.5, 'water_per_person': 2.5, 'medical_per_injured': 1.0}
        assert client.patch('/api/settings', json=coefficients).status_code == 200
        assert state(client)['settings'] == coefficients
        before_reset = state(client)['version']
        assert client.post('/api/demo/reset', json={'confirm': False}).status_code == 422
        assert client.post('/api/demo/reset', json={'confirm': True}).status_code == 200
        reset = state(client)
        assert (len(reset['locations']), len(reset['warehouses']), len(reset['vehicles'])) == (5, 3, 3)
        assert reset['version'] > before_reset


def test_assigned_field_worker_completes_a_tour_and_releases_vehicle(app):
    with client_for(app) as coordinator_client:
        login(coordinator_client)
        before = state(coordinator_client)
        plan = generate(coordinator_client)
        approve(coordinator_client, plan)
        route = plan['result']['routes'][0]
        deliveries = [d for d in state(coordinator_client)['deliveries'] if d['vehicle_id'] == route['vehicle_id']]
        first = deliveries[0]
        assert coordinator_client.post(f'/api/deliveries/{first["id"]}/dispatch', json={}).status_code == 200
        after_dispatch = state(coordinator_client)
        original_stock = next(w for w in before['warehouses'] if w['id'] == route['warehouse_id'])['stock']
        expected_stock = {item: original_stock[item] - sum(d['manifest'][item] for d in deliveries) for item in ITEMS}
        assert next(w for w in after_dispatch['warehouses'] if w['id'] == route['warehouse_id'])['stock'] == expected_stock
        repeat = coordinator_client.post(f'/api/deliveries/{first["id"]}/dispatch', json={})
        assert repeat.status_code in (200, 409)
        assert next(w for w in state(coordinator_client)['warehouses'] if w['id'] == route['warehouse_id'])['stock'] == expected_stock
    with client_for(app) as worker:
        user = login(worker, 'field_worker')
        current = state(worker)['locations'][0]
        assert worker.post('/api/requests', json={'location_id': current['id'], **current['demands'], 'notes': 'Field survey reconfirmed.'}).status_code == 200
        for delivery in deliveries:
            assert delivery['assigned_user_id'] == user['id']
            response = worker.post(f'/api/deliveries/{delivery["id"]}/verify', json={**delivery['manifest'], 'recipient': 'Assigned Field Worker'})
            assert response.status_code == 200, response.text
        snapshot = state(worker)
        assert next(v for v in snapshot['vehicles'] if v['id'] == route['vehicle_id'])['status'] == 'available'
        assert all(d['status'] == 'verified' for d in snapshot['deliveries'] if d['vehicle_id'] == route['vehicle_id'])
