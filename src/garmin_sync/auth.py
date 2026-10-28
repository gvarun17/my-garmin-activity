"""Garmin Personal API authentication.

Story 2 only: mint and cache a short-lived access token.
No data fetching happens here. That is a later story.

How it works, per the Garmin Personal API FAQ:
- Your long-lived Personal API key is NOT used directly on data
  endpoints.
- You exchange it at the token endpoint for an access token.
  The token endpoint takes the key in an ``Authorization: ApiKey``
  header and returns the token as the raw response body, not JSON.
- The returned value is a JWT, valid for 24 hours.
- There are no refresh tokens. When the cached token is close to
  expiry, mint a new one the same way.
- Data endpoints (later stories) use the access token as a
  ``Bearer`` token. Do not confuse the two headers.

Secrets hygiene:
- The key comes from the environment, after loading a local ``.env``
  file if one exists. It is never printed, logged, or included in
  error messages.
- The cached access token is written to a local file with mode 0600
  and is gitignored. It is never committed.
- The ``--check`` command prints only whether a token is usable and
  when it expires. It never prints any token characters.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_ENDPOINT = (
    "https://personal.connectapi.garmin.com"
    "/personalapitoken-service/personal-api/personal-api-key/token"
)

# Mint a new token when the cached one has less than this long left.
# Garmin's own Python client uses the same 1-hour rule.
DEFAULT_REFRESH_THRESHOLD_SECONDS = 3600
DEFAULT_CACHE_PATH = ".garmin-token.json"
PLACEHOLDER_VALUES = {"", "change-me-in-env"}


class AuthError(Exception):
    """Raised for missing keys, bad tokens, or token-endpoint failures."""


def load_dotenv(env_file: str | Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Existing environment variables win over .env values. This is a
    small stdlib loader on purpose: no python-dotenv dependency.
    Lines starting with # and blank lines are ignored. Values may be
    optionally quoted with single or double quotes.
    """
    path = Path(env_file) if env_file else Path(".env")
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def get_api_key() -> str:
    """Return the Garmin Personal API key, or raise without echoing it."""
    load_dotenv(os.environ.get("GARMIN_ENV_FILE"))
    key = os.environ.get("GARMIN_API_KEY", "").strip()
    if key in PLACEHOLDER_VALUES:
        raise AuthError(
            "GARMIN_API_KEY is missing or still a placeholder. "
            "Set it in .env (never commit .env) and try again."
        )
    return key


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_token_expiry(token: str) -> datetime:
    """Return the expiry time (UTC) from a JWT's ``exp`` claim.

    The signature is not verified here: expiry checking is a local
    caching decision, and Garmin is the authority when the token is
    actually used on a data endpoint.
    """
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthError("Access token is not in JWT form (3 dot-separated parts).")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
        exp = int(payload["exp"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuthError("Access token has no usable exp claim.") from exc
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def _cache_path() -> Path:
    return Path(os.environ.get("GARMIN_TOKEN_CACHE", DEFAULT_CACHE_PATH))


def _refresh_threshold() -> timedelta:
    try:
        seconds = int(
            os.environ.get(
                "GARMIN_TOKEN_REFRESH_THRESHOLD_SECONDS",
                str(DEFAULT_REFRESH_THRESHOLD_SECONDS),
            )
        )
    except ValueError:
        seconds = DEFAULT_REFRESH_THRESHOLD_SECONDS
    return timedelta(seconds=seconds)


def _read_cached_token(path: Path) -> str | None:
    """Return a cached token if it is still usable, else None."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data["access_token"]
        expires_at = datetime.fromisoformat(data["expires_at"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at - datetime.now(timezone.utc) > _refresh_threshold():
        return token
    return None


def _write_cached_token(path: Path, token: str, expires_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(
            {"access_token": token, "expires_at": expires_at.isoformat()},
            indent=2,
        ),
        encoding="utf-8",
    )
    # Token file must not be world-readable, even on a shared machine.
    try:
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _default_http_post(url: str, headers: dict, timeout: int):
    """The real HTTP call, imported lazily so tests need no requests."""
    import requests

    return requests.post(url, headers=headers, timeout=timeout)


def mint_token(api_key: str, http_post=None) -> tuple[str, datetime]:
    """Exchange a Personal API key for a 24-hour access token.

    ``http_post`` is injectable for tests: a callable taking
    (url, headers, timeout) and returning an object with ``status_code``
    and ``text``. When omitted, ``requests`` is used.
    """
    post = http_post or _default_http_post
    try:
        response = post(
            TOKEN_ENDPOINT,
            headers={"Authorization": f"ApiKey {api_key}"},
            timeout=15,
        )
    except Exception as exc:
        # This is deliberately a short, actionable message, not the
        # raw requests/urllib3 traceback. The common cause on macOS is
        # the system Python 3.9 linked against old LibreSSL, which
        # Garmin's server rejects with a TLS protocol-version alert.
        # Fix: run with a modern Python (3.12+, Homebrew or python.org,
        # linked against OpenSSL 3) in a venv. Never set verify=False.
        raise AuthError(
            "Garmin token request failed before getting a response "
            f"({type(exc).__name__}). If this is an SSLError on macOS "
            "system Python 3.9 / LibreSSL, switch to Python 3.12+ "
            "with modern OpenSSL in a venv and try again."
        ) from exc
    if response.status_code != 200:
        # Deliberately no key, token, or response body in this message.
        raise AuthError(
            f"Garmin token endpoint returned HTTP {response.status_code}. "
            "Check the key is a Personal API key and try again."
        )
    token = response.text.strip()
    if not token:
        raise AuthError("Garmin token endpoint returned an empty body.")
    expires_at = decode_token_expiry(token)
    if expires_at <= datetime.now(timezone.utc):
        raise AuthError("Garmin token endpoint returned an already-expired token.")
    return token, expires_at


def get_access_token(force_refresh: bool = False, http_post=None) -> tuple[str, datetime, str]:
    """Return (token, expires_at, source) where source is cache or minted.

    Reuses a cached token while it has more than the refresh threshold
    left. Otherwise mints a new one and updates the cache.
    """
    path = _cache_path()
    if not force_refresh:
        cached = _read_cached_token(path)
        if cached is not None:
            return cached, decode_token_expiry(cached), "cache"
    api_key = get_api_key()
    token, expires_at = mint_token(api_key, http_post=http_post)
    _write_cached_token(path, token, expires_at)
    return token, expires_at, "minted"


def auth_headers(token: str) -> dict:
    """Headers for Garmin data endpoints (later stories)."""
    return {"Authorization": f"Bearer {token}"}


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m garmin_sync.auth [--check] [--force]

    --check (default) prints token usability and expiry only.
    --force mints a new token even if a usable cached token exists.
    Run from the repo root so .env and the token cache are found.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    force = "--force" in args
    try:
        _token, expires_at, source = get_access_token(force_refresh=force)
    except AuthError as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 1
    local_expiry = expires_at.astimezone()
    print(f"Token OK (source: {source}).")
    print(f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')} "
          f"({local_expiry.strftime('%Y-%m-%d %H:%M:%S %Z')}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
