import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from vipercapture.render_errors import RenderError, install_render_error_layer


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

    def test_request_id_is_generated_and_safe_id_is_preserved(self):
        generated = self.client.post("/validate", json={"value": 1})
        self.assertRegex(generated.headers["x-request-id"], r"^[0-9a-f-]{36}$")
        preserved = self.client.post(
            "/validate", json={"value": 1}, headers={"X-Request-Id": "client.id:42"}
        )
        self.assertEqual(preserved.headers["x-request-id"], "client.id:42")
        replaced = self.client.post(
            "/validate", json={"value": 1}, headers={"X-Request-Id": "bad id"}
        )
        self.assertNotEqual(replaced.headers["x-request-id"], "bad id")

    def test_reserved_project_request_id_is_rejected(self):
        reserved = f"_project-{'a' * 24}:caller"
        response = self.client.post(
            "/validate",
            json={"value": 1},
            headers={"X-Request-Id": reserved},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "request_id_reserved")
        self.assertNotEqual(response.headers["x-request-id"], reserved)

    def test_render_status_families_share_the_envelope(self):
        for status in (400, 401, 403, 409, 413, 422, 429, 502, 504, 500):
            with self.subTest(status=status):
                response = self.client.get(f"/render/{status}")
                self.assertEqual(response.status_code, status)
                body = response.json()["error"]
                self.assertEqual(body["code"], "test_error")
                self.assertEqual(body["request_id"], response.headers["x-request-id"])
                self.assertIsInstance(body["details"], dict)

    def test_validation_details_do_not_echo_input(self):
        response = self.client.post("/validate", json={"value": "secret-cookie=value"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("secret-cookie", response.text)

    def test_http_errors_are_normalized(self):
        response = self.client.get("/http/429")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "rate_limited")
        self.assertTrue(response.json()["error"]["retryable"])

    def test_unexpected_error_leaks_no_exception(self):
        response = self.client.get("/boom")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("secret-cookie", response.text)
        self.assertNotIn("Traceback", response.text)


if __name__ == "__main__":
    unittest.main()
