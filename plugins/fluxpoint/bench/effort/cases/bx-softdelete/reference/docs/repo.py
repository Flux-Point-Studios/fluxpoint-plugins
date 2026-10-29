def migrate(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(documents)")]
    if "deleted_at" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN deleted_at INTEGER")


def get(db, doc_id):
    return db.execute("SELECT id, owner, title, body FROM documents WHERE id = ? AND deleted_at IS NULL",
                      (doc_id,)).fetchone()


def list_for(db, owner):
    return db.execute("SELECT id, title FROM documents WHERE owner = ? AND deleted_at IS NULL",
                      (owner,)).fetchall()


def delete(db, doc_id, now):
    db.execute("UPDATE documents SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now, doc_id))


def restore(db, doc_id):
    cur = db.execute("UPDATE documents SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
                     (doc_id,))
    return cur.rowcount == 1
