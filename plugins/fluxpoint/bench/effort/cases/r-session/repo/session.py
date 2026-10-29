"""Stateless tokens: base64(json payload) + '.' + hex HMAC.

Two kinds share one key: short-lived session tokens, and password-reset
tokens that are emailed to users and logged by the mail relay.
"""
import base64
import hashlib
import hmac
import json
import time

TTL = 3600
RESET_TTL = 2 * 86400
REFRESH_WINDOW = 7 * 86400


def _sign(key, payload_b64):
    return hmac.new(key, payload_b64, hashlib.sha256).hexdigest()


def _issue(key, payload):
    b64 = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode())
    return (b64 + b"." + _sign(key, b64).encode()).decode()


def issue(key, user, role, now=None):
    now = int(now if now is not None else time.time())
    return _issue(key, {"typ": "session", "sub": user, "role": role, "iat": now, "exp": now + TTL})


def issue_reset(key, user, now=None):
    now = int(now if now is not None else time.time())
    return _issue(key, {"typ": "reset", "sub": user, "role": "user", "iat": now,
                        "exp": now + RESET_TTL})


def _decode(key, token):
    try:
        b64, sig = token.encode().rsplit(b".", 1)
    except ValueError:
        raise PermissionError("malformed")
    if not hmac.compare_digest(_sign(key, b64).encode(), sig):  # @decoy:compare
        raise PermissionError("bad signature")
    return json.loads(base64.urlsafe_b64decode(b64))


def verify(key, token, now=None):
    """Authenticate a request's session token."""
    now = int(now if now is not None else time.time())
    claims = _decode(key, token)
    if claims["exp"] <= now:  # @seed:typ-confusion
        raise PermissionError("expired")
    return claims


def refresh(key, token, current_role, now=None, requested_role=None):
    """Exchange a session token that expired less than REFRESH_WINDOW ago.

    current_role is the role the user database holds for claims['sub'];
    requested_role lets a user drop privileges for the new token.
    """
    now = int(now if now is not None else time.time())
    claims = _decode(key, token)
    if claims.get("typ") != "session":
        raise PermissionError("not a session token")
    if now - claims["exp"] > REFRESH_WINDOW:
        raise PermissionError("too old to refresh")
    role = requested_role or current_role  # @seed:role-escalation
    return issue(key, claims["sub"], role, now)


def is_admin(claims):
    return claims.get("role") == "admin"  # @decoy:role-check
