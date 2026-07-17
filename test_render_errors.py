import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from render_errors import RenderError, install_render_error_layer


class Payload(BaseModel):
    value: int


def test_app() -> FastAPI:
    app = FastAPI()
    install_render_error_layer(app)

    @app.get("/render/{status}")
    async def render_error(status: int):
        raise RenderError("test_error", "Safe test error.", status, status >= 429)

    @app.get("/http/{status}")
    async def http_error(status: int):
        raise HTTPException(status, "Safe HTTP error.")

    @app.post("/validate")
    async def validate(payload: Payload):
        return payload

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret-cookie=value")

    return app


class RenderErrorLayerTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(test_app(), raise_server_exceptions=False)

    def test_request_ids_and_error_families(self):
        response = self.client.post(
            "/validate", json={"value": 1}, headers={"X-Request-Id": "client.id:42"}
        )
        self.assertEqual(response.headers["x-request-id"], "client.id:42")
        for status in (400, 401, 403, 409, 413, 422, 429, 502, 504, 500):
            with self.subTest(status=status):
                response = self.client.get(f"/render/{status}")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], "test_error")
                self.assertEqual(
                    response.json()["error"]["request_id"],
                    response.headers["x-request-id"],
                )

    def test_validation_http_and_unexpected_errors_are_safe(self):
        invalid = self.client.post("/validate", json={"value": "secret-cookie=value"})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("secret-cookie", invalid.text)
        limited = self.client.get("/http/429")
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")
        failure = self.client.get("/boom")
        self.assertEqual(failure.status_code, 500)
        self.assertEqual(failure.json()["error"]["code"], "internal_error")
        self.assertNotIn("secret-cookie", failure.text)
        self.assertNotIn("Traceback", failure.text)


if __name__ == "__main__":
    unittest.main()
