"""Static pages: the landing owns /, the dashboard serves at /app and /app.html."""
from fastapi.testclient import TestClient

from service.api import create_app


def client(tmp_path):
    # Pages are loopback-only unless PUBLIC_DEMO, so request from 127.0.0.1.
    return TestClient(create_app(tmp_path / "pages.sqlite3"), base_url="http://127.0.0.1")


def test_landing_serves_at_root_without_login(tmp_path):
    page = client(tmp_path).get("/")
    assert page.status_code == 200
    assert "RestoPilot · From purchase to peace of mind" in page.text
    assert "/landing-assets/styles.css" in page.text
    # The landing links to the dashboard's static Atoms path, not to /app.
    assert 'href="/app.html"' in page.text
    assert 'href="/app"' not in page.text
    # The landing may carry its own lightweight scripts (the ID/EN language
    # toggle), but must not load the dashboard bundle.
    assert "/assets/app.js" not in page.text


def test_dashboard_serves_at_app_and_app_html(tmp_path):
    c = client(tmp_path)
    for path in ("/app", "/app.html"):
        page = c.get(path)
        assert page.status_code == 200, path
        assert "/assets/app.js" in page.text
        assert "landing-assets" not in page.text
