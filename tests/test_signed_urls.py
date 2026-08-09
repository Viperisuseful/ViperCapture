import unittest

from vipercapture.render_contract import RenderRequest
from vipercapture.render_errors import RenderError
from vipercapture.signed_urls import (
    decode_render_request,
    encode_render_request,
    sign_render_request,
    verify_render_request,
)


class SignedUrlTests(unittest.TestCase):
    def test_round_trip_is_canonical_and_preserves_options(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "webp",
                "full_page": False,
                "actions": [{"type": "click", "selector": "#open"}],
            }
        )
        encoded = encode_render_request(request)
        self.assertEqual(encoded, encode_render_request(request))
        self.assertEqual(decode_render_request(encoded), request)

        payload, expires, signature = sign_render_request(
            request,
            secret="test-secret-with-enough-entropy",
            ttl_seconds=60,
            now=1_000,
        )
        verified = verify_render_request(
            payload,
            expires,
            signature,
            secret="test-secret-with-enough-entropy",
            now=1_030,
        )
        self.assertEqual(verified, request)

    def test_tampering_and_expiry_fail_closed(self):
        request = RenderRequest.model_validate({"url": "https://example.com"})
        payload, expires, signature = sign_render_request(
            request, secret="secret", ttl_seconds=60, now=1_000
        )
        with self.assertRaises(RenderError) as tampered:
            verify_render_request(
                payload + "x",
                expires,
                signature,
                secret="secret",
                now=1_001,
            )
        self.assertEqual(tampered.exception.code, "signed_url_invalid")
        with self.assertRaises(RenderError) as expired:
            verify_render_request(
                payload,
                expires,
                signature,
                secret="secret",
                now=1_061,
            )
        self.assertEqual(expired.exception.code, "signed_url_expired")

    def test_large_sources_use_post_instead_of_a_signed_url(self):
        request = RenderRequest(html="x" * 30_000)
        with self.assertRaisesRegex(ValueError, "too large"):
            sign_render_request(request, secret="s" * 32)
