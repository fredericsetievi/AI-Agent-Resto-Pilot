"""Exercise the exported Atoms proxy without native cloud services or Postgres."""
import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv('RESTOPILOT_HOSTED', '1')
    monkeypatch.setenv('SUPABASE_URL', 'https://offline.supabase.co')
    path = Path(__file__).resolve().parents[1] / 'deploy/atoms/backend/routers/restopilot.py'
    spec = importlib.util.spec_from_file_location('atoms_restopilot_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client_for(adapter, upstream):
    adapter._app = upstream
    wrapper = FastAPI()
    wrapper.include_router(adapter.router)
    return TestClient(wrapper, base_url='https://atoms.test')


def test_prefixed_proxy_preserves_query_body_headers_and_multiple_cookies(adapter):
    upstream = FastAPI()

    @upstream.post('/v1/check')
    async def check(request: Request):
        response = JSONResponse({
            'query': request.query_params.getlist('tag'),
            'body': await request.json(),
            'key': request.headers['idempotency-key'],
            'session': request.cookies['restopilot_session'],
        }, status_code=202, headers={'X-Upstream': 'kept'})
        response.set_cookie('first', 'one', secure=True, httponly=True, samesite='lax')
        response.set_cookie('second', 'two', secure=True)
        return response

    with client_for(adapter, upstream) as client:
        response = client.post('/api/v1/restopilot/v1/check?tag=a&tag=b',
                               json={'items': [1, 2]},
                               headers={'Idempotency-Key': 'test-key', 'Cookie': 'restopilot_session=offline'})
    assert response.status_code == 202
    assert response.json() == {'query': ['a', 'b'], 'body': {'items': [1, 2]},
                               'key': 'test-key', 'session': 'offline'}
    assert response.headers['x-upstream'] == 'kept'
    assert len(response.headers.get_list('set-cookie')) == 2
    assert 'Secure' in response.headers.get_list('set-cookie')[0]
    assert 'HttpOnly' in response.headers.get_list('set-cookie')[0]


def test_upstream_auth_error_is_returned_without_rewrapping(adapter):
    upstream = FastAPI()

    @upstream.get('/v1/overview')
    def denied():
        return JSONResponse({'error': {'code': 'unauthenticated', 'message': 'Login required.'}},
                            status_code=401, headers={'Cache-Control': 'no-store'})

    with client_for(adapter, upstream) as client:
        response = client.get('/api/v1/restopilot/v1/overview')
    assert response.status_code == 401
    assert response.json()['error']['code'] == 'unauthenticated'
    assert response.headers['cache-control'] == 'no-store'


def test_hosted_storage_failure_survives_platform_proxy(adapter, monkeypatch):
    from service import pg
    monkeypatch.delenv('SUPABASE_DB_PASSWORD', raising=False)
    pg.reset_pool()
    try:
        wrapper = FastAPI()
        wrapper.include_router(adapter.router)
        with TestClient(wrapper, base_url='https://atoms.test') as client:
            assert client.get('/api/v1/restopilot/health/live').json()['data']['storage'] == 'unavailable'
            response = client.get('/api/v1/restopilot/health/ready')
        assert response.status_code == 500
        assert response.json()['error']['code'] == 'storage_unavailable'
    finally:
        pg.reset_pool()
