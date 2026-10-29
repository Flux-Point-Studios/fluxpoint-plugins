import json


def export_owner(db, owner):
    rows = db.execute("SELECT id, title, body FROM documents WHERE owner = ? AND deleted_at IS NULL", (owner,))
    return json.dumps([{"id": r[0], "title": r[1], "body": r[2]} for r in rows])
