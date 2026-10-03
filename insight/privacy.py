"""Author hashing and secret redaction."""
import hashlib
import os
import re

SALT_ENV = "INSIGHT_AUTHOR_SALT"

_SECRET_KEYS = {"access_token", "token", "client_secret", "app_secret", "appsecret_proof", "refresh_token"}
_SECRET_IN_URL = re.compile(r"((?:access_token|client_secret|appsecret_proof|refresh_token)=)[^&\s\"']+", re.I)


def _load_salt():
    salt = os.environ.get(SALT_ENV)
    if not salt:
        # .env is optional at import time; read it lazily the first time we need the salt.
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
        except ImportError:
            pass
        salt = os.environ.get(SALT_ENV)
    return salt


def hash_author(username, salt=None):
    """SHA-256 hex of salt + normalized username. Raises if no salt is configured:
    an unsalted hash of a public username is trivially reversible."""
    if username is None:
        return None
    salt = salt if salt is not None else _load_salt()
    if not salt:
        raise RuntimeError(f"{SALT_ENV} is not set; refusing to hash authors without a salt")
    normalized = username.strip().lstrip("@").lower()
    return hashlib.sha256((salt + "\x00" + normalized).encode("utf-8")).hexdigest()


def redact_secrets(payload):
    """Deep copy of payload with token-like keys removed and tokens in URLs masked."""
    if isinstance(payload, dict):
        return {k: redact_secrets(v) for k, v in payload.items() if k.lower() not in _SECRET_KEYS}
    if isinstance(payload, list):
        return [redact_secrets(v) for v in payload]
    if isinstance(payload, str):
        return _SECRET_IN_URL.sub(r"\1REDACTED", payload)
    return payload


_USER_IDENTIFIER_KEYS = {
    "username", "user_id", "author_id", "author_username", "owner_id", "account_id",
}


def redact_user_identifiers(payload):
    """Strip or redact every user identifier (username, from.id, from.username, user_id, etc.) at any depth.
    Keeps comment IDs intact.
    """
    if isinstance(payload, dict):
        new_d = {}
        for k, v in payload.items():
            k_lower = k.lower()
            if k_lower in _USER_IDENTIFIER_KEYS:
                new_d[k] = "<redacted>"
            elif k_lower in ("from", "user", "owner", "author"):
                if isinstance(v, dict):
                    new_d[k] = {
                        sub_k: ("<redacted>" if sub_k.lower() in ("id", "username", "name", "handle", "pk") else redact_user_identifiers(sub_v))
                        for sub_k, sub_v in v.items()
                    }
                else:
                    new_d[k] = "<redacted>"
            else:
                new_d[k] = redact_user_identifiers(v)
        return new_d
    if isinstance(payload, list):
        return [redact_user_identifiers(v) for v in payload]
    return payload

