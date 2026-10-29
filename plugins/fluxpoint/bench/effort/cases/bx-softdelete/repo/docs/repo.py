def get(db, doc_id):
    return db.execute("SELECT id, owner, title, body FROM documents WHERE id = ?", (doc_id,)).fetchone()


def list_for(db, owner):
    return db.execute("SELECT id, title FROM documents WHERE owner = ?", (owner,)).fetchall()


def delete(db, doc_id):
    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
