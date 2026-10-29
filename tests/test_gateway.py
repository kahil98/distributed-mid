from fastapi.testclient import TestClient

from gateway_service.app import create_app


def test_jwt_login_protects_api_and_rate_limit_is_enforced():
    app = create_app(initialize_db=False, secret="test-secret", username="student", password="exam", rate_limit=5)
    with TestClient(app) as client:
        assert client.get("/v1").status_code == 401
        assert client.post("/auth/token", json={"username": "student", "password": "wrong"}).status_code == 401
        token = client.post("/auth/token", json={"username": "student", "password": "exam"})
        assert token.status_code == 200
        assert token.json()["token_type"] == "bearer"
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        assert client.get("/v1", headers=headers).json()["user"] == "student"
        assert client.get("/v1", headers=headers).status_code == 200
        limited = client.get("/v1", headers=headers)
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"


def test_invalid_jwt_is_rejected():
    app = create_app(initialize_db=False, secret="test-secret", rate_limit=10)
    with TestClient(app) as client:
        assert client.get("/v1", headers={"Authorization": "Bearer forged.token.value"}).status_code == 401
