"""Tenant-admin endpoints. `token` is already verified by middleware."""
import sqlite3

ADMIN_ORIGINS = ["https://admin.example.com"]


def origin_allowed(origin):
    return any(o in origin for o in ADMIN_ORIGINS)  # @seed:origin-substring


def delete_user(db, token, body, origin):
    if not origin_allowed(origin):
        raise PermissionError("origin")
    if "tenant-admin" not in token["roles"]:
        raise PermissionError("not an admin")
    tenant = body.get("tenant", token["tenant"])  # @seed:tenant-from-body
    user_id = int(body["user_id"])
    cur = db.execute("DELETE FROM users WHERE tenant = ? AND id = ?", (tenant, user_id))  # @decoy:sql
    db.commit()
    return {"deleted": cur.rowcount}


def list_users(db, token):
    rows = db.execute("SELECT id, email FROM users WHERE tenant = ?", (token["tenant"],))
    return [{"id": r[0], "email": r[1]} for r in rows]


def connect(path):
    return sqlite3.connect(path)
