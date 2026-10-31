from datetime import date


def test_registration_and_hold_expose_exact_observation_dates(client, guest_headers, today):
    response = client.post('/api/v1/items/batch', headers={**guest_headers, 'Idempotency-Key': 'expo-contract-1'}, json={'items': [{'name': '키보드', 'price': 20000, 'category': 'DIGITAL', 'purchase_reasons': ['NECESSITY'], 'question_mode': 'LOCAL'}]})
    assert response.status_code == 201
    item = response.json()['items'][0]
    assert item['simulation']['observation_dates'] == ['2026-11-01', '2026-11-02', '2026-11-03']
    today.value = date(2026, 11, 9)
    held = client.put(f"/api/v1/items/{item['id']}/decision", headers={**guest_headers, 'Idempotency-Key': 'expo-hold-1'}, json={'decision': 'HOLD'})
    assert held.status_code == 200
    dates = held.json()['simulation']['observation_dates']
    assert len(dates) == 10
    assert dates[:3] == item['simulation']['observation_dates']
    assert dates[3] == '2026-11-10' and dates[-1] == '2026-11-16'
    assert '2026-11-09' not in dates
    replay = client.put(f"/api/v1/items/{item['id']}/decision", headers={**guest_headers, 'Idempotency-Key': 'expo-hold-1'}, json={'decision': 'HOLD'})
    assert replay.json()['simulation']['observation_dates'] == dates


def test_today_deadline_exposes_empty_schedule(client, guest_headers, today):
    response = client.post('/api/v1/items/batch', headers={**guest_headers, 'Idempotency-Key': 'expo-deadline-1'}, json={'items': [{'name': '한정 물품', 'price': 150000, 'category': 'HOBBY', 'purchase_reasons': ['DISCOUNT_LIMITED'], 'sale_ends_on': today.value.isoformat(), 'question_mode': 'LOCAL'}]})
    assert response.status_code == 201
    simulation = response.json()['items'][0]['simulation']
    assert simulation['duration_days'] == 0
    assert simulation['observation_dates'] == []
    assert simulation['status'] == 'COMPLETED'
