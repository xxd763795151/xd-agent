from types import SimpleNamespace

from fastapi.testclient import TestClient

from xd_agent.core.config import load_settings
from xd_agent.web.app import create_app
from xd_agent.web.dependencies import get_runtime


def test_greeting_contract(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()
    with TestClient(create_app()) as client:
        response = client.post("/greetings", json={"name": "Ada"})
        assert response.status_code == 200
        assert response.json() == {"message": "Hello, Ada!"}
        assert response.headers["X-Request-ID"]
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {"status": "ok"}
    load_settings.cache_clear()


def test_application_error_maps_to_http(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()
    with TestClient(create_app()) as client:
        response = client.post("/greetings", json={"name": " "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_name"
    load_settings.cache_clear()


def test_wecom_verification_and_message_routes(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()

    class FakeChannel:
        async def verify_webhook(self, query):
            assert query == {"echostr": "encrypted"}
            return 200, b"decrypted"

        async def handle_webhook(self, headers, body, query):
            assert body == b"<xml/>"
            assert query == {"nonce": "123"}
            return 200, b"success"

    app = create_app()
    app.dependency_overrides[get_runtime] = lambda: SimpleNamespace(
        channel=FakeChannel(), settings=SimpleNamespace(channel=SimpleNamespace(type="wecom"))
    )
    with TestClient(app) as client:
        echo = client.get("/webhook/event?echostr=encrypted")
        callback = client.post("/webhook/event?nonce=123", content=b"<xml/>")
    assert echo.status_code == 200 and echo.text == "decrypted"
    assert callback.status_code == 200 and callback.text == "success"
    assert callback.headers["content-type"].startswith("text/plain")
    load_settings.cache_clear()
