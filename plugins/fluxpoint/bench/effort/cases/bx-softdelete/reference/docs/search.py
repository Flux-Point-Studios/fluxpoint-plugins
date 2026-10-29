def search(db, owner, term):
    q = "SELECT id, title FROM documents WHERE owner = ? AND body LIKE ? AND deleted_at IS NULL"
    return db.execute(q, (owner, f"%{term}%")).fetchall()
