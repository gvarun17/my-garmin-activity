"""Tests for garmin_sync.auth. All Garmin calls are mocked.

No real API key is used here, and no network call is made.
Run from the repo root:  PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from garmin_sync import auth


def make_jwt(exp: datetime) -> str:
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'exp': int(exp.timestamp())})}.sig"


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakePost:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        # The key must reach the token endpoint as an ApiKey header.
        assert headers["Authorization"].startswith("ApiKey "), headers
        return FakeResponse(200, self.token)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "token.json"
        os.environ["GARMIN_TOKEN_CACHE"] = str(self.cache)
        os.environ["GARMIN_API_KEY"] = "test-key-not-real"
        os.environ.pop("GARMIN_ENV_FILE", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("GARMIN_TOKEN_CACHE", None)
        os.environ.pop("GARMIN_API_KEY", None)

    def test_decode_token_expiry(self):
        exp = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(auth.decode_token_expiry(make_jwt(exp)), exp)

    def test_decode_rejects_non_jwt(self):
        with self.assertRaises(auth.AuthError):
            auth.decode_token_expiry("not-a-jwt")

    def test_mint_and_cache_reuse(self):
        # JWT exp is whole seconds, so drop microseconds before comparing.
        exp = (datetime.now(timezone.utc) + timedelta(hours=20)).replace(microsecond=0)
        fake = FakePost(make_jwt(exp))
        token, expires, source = auth.get_access_token(http_post=fake)
        self.assertEqual(source, "minted")
        self.assertEqual(expires, exp)
        token2, _expires2, source2 = auth.get_access_token(http_post=fake)
        self.assertEqual(source2, "cache")
        self.assertEqual(token2, token)
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(self.cache.is_file())

    def test_near_expiry_token_is_reminted(self):
        near = datetime.now(timezone.utc) + timedelta(minutes=30)
        fake_old = FakePost(make_jwt(near))
        auth.get_access_token(http_post=fake_old)
        self.assertEqual(len(fake_old.calls), 1)
        far = datetime.now(timezone.utc) + timedelta(hours=20)
        fake_new = FakePost(make_jwt(far))
        _token, _exp, source = auth.get_access_token(http_post=fake_new)
        self.assertEqual(source, "minted")
        self.assertEqual(len(fake_new.calls), 1)

    def test_force_refresh_mints_again(self):
        exp = datetime.now(timezone.utc) + timedelta(hours=20)
        fake = FakePost(make_jwt(exp))
        auth.get_access_token(http_post=fake)
        auth.get_access_token(force_refresh=True, http_post=fake)
        self.assertEqual(len(fake.calls), 2)

    def test_request_failure_is_wrapped_without_secrets(self):
        def raising_post(url, headers, timeout):
            raise RuntimeError("simulated SSL failure")

        with self.assertRaises(auth.AuthError) as ctx:
            auth.mint_token("test-key-not-real", http_post=raising_post)
        self.assertIn("Python 3.12", str(ctx.exception))
        self.assertNotIn("test-key-not-real", str(ctx.exception))

    def test_token_endpoint_error_is_reported_without_secrets(self):
        def failing_post(url, headers, timeout):
            return FakeResponse(401, "unauthorized")

        with self.assertRaises(auth.AuthError) as ctx:
            auth.mint_token("test-key-not-real", http_post=failing_post)
        self.assertNotIn("test-key-not-real", str(ctx.exception))

    def test_missing_key_raises(self):
        os.environ["GARMIN_API_KEY"] = "change-me-in-env"
        with self.assertRaises(auth.AuthError):
            auth.get_api_key()

    def test_auth_headers_use_bearer(self):
        self.assertEqual(
            auth.auth_headers("abc123"), {"Authorization": "Bearer abc123"}
        )


if __name__ == "__main__":
    unittest.main()
