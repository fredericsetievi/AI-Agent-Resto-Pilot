"""Open WhatsApp enrollment uses explicit selection before any purchase work."""
from uuid import uuid4

from fastapi.testclient import TestClient

from service.api import create_app
from service.db import Store, RESTAURANT
from service.onboarding import restaurant_selection
from test_tenant_isolation import META, FakeGraphClient, meta_payload, webhook


NEW_SENDER = '628888123456'


def ingest(store, text, *, sender=NEW_SENDER, key=None, **kwargs):
    return store.whatsapp_ingest(key or uuid4().hex, {'sender': sender, 'text': text}, open_onboarding=True, **kwargs)


def test_new_sender_must_select_restaurant_before_any_draft(tmp_path):
    store = Store(tmp_path / 'onboarding.sqlite3')
    before = len(store.overview(RESTAURANT['id'])['drafts'])
    response = ingest(store, 'beli cabai 2 kg 100rb')
    assert response['result']['action'] == 'restaurant_selection_required'
    assert 'Warung Bu Sari' in response['messages'][-1]['body']
    assert store.restaurant_for_sender(NEW_SENDER) is None
    assert len(store.overview(RESTAURANT['id'])['drafts']) == before
    selected = ingest(store, 'Warung Bu Sari')
    assert selected['result']['action'] == 'restaurant_selected'
    assert 'draft' not in selected
    assert store.restaurant_for_sender(NEW_SENDER)['id'] == RESTAURANT['id']
    assert len(store.overview(RESTAURANT['id'])['drafts']) == before
    draft = ingest(store, 'beli cabai 2 kg 100rb')
    assert draft['result']['action'] == 'draft_created'
    assert len(store.overview(RESTAURANT['id'])['drafts']) == before + 1


def test_invalid_selection_and_event_retries_do_not_relink_or_create_drafts(tmp_path):
    store = Store(tmp_path / 'retry.sqlite3')
    assert ingest(store, 'unknown')['result']['action'] == 'restaurant_selection_required'
    key = uuid4().hex
    first = ingest(store, RESTAURANT['name'], key=key)
    again = ingest(store, RESTAURANT['name'], key=key)
    assert first == again
    assert len([row for row in store.linked_numbers(RESTAURANT['id']) if row['phone'] == NEW_SENDER]) == 1
    before = store.restaurant_for_sender(NEW_SENDER)
    ingest(store, 'another restaurant')
    assert store.restaurant_for_sender(NEW_SENDER) == before


def test_duplicate_names_require_stable_code_not_menu_position():
    choices = [{'id': 'one', 'name': 'Warung Sama'}, {'id': 'two', 'name': 'Warung Sama'}]
    selected, menu = restaurant_selection(choices, 'Warung Sama')
    assert selected is None and 'kode: one' in menu and 'kode: two' in menu
    assert restaurant_selection(list(reversed(choices)), 'two')[0]['id'] == 'two'
    assert restaurant_selection(choices, '1')[0] is None
    assert restaurant_selection([], 'anything')[0] is None


def test_open_webhook_prompts_then_links_without_media_or_model_calls(tmp_path):
    provider = FakeGraphClient(content_json=None)
    app = create_app(tmp_path / 'webhook.sqlite3', meta_http_client=provider,
                     whatsapp_open_onboarding=True, vision_api_key='offline-model-key', **META)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert webhook(client, meta_payload(NEW_SENDER, kind='image')).status_code == 200
        assert len(provider.calls) == 1  # response only; no media download before selection
        assert 'Pilih restoran' in provider.calls[0]['json']['text']['body']
        assert webhook(client, meta_payload(NEW_SENDER, 'Warung Bu Sari')).status_code == 200
    assert app.state.store.restaurant_for_sender(NEW_SENDER)['id'] == RESTAURANT['id']


def test_simulator_enrollment_cannot_select_another_owners_restaurant(tmp_path):
    app = create_app(tmp_path / 'sim.sqlite3', whatsapp_open_onboarding=True)
    store = app.state.store
    with store.transaction() as conn:
        conn.execute("INSERT INTO restaurants (id,name,created_at) VALUES ('other','Other Restaurant','2026-01-01')")
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.post('/v1/auth/login', json={'username':'owner','password':'owner123'}).status_code == 200
        response = client.post('/v1/whatsapp/simulate', headers={'Idempotency-Key':uuid4().hex},
                               json={'sender':NEW_SENDER,'text':'Other Restaurant'})
        assert response.status_code == 200
        assert response.json()['data']['result']['action'] == 'restaurant_selection_required'
        assert 'Other Restaurant' not in response.json()['data']['messages'][-1]['body']
    assert store.restaurant_for_sender(NEW_SENDER) is None
