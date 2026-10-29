def search(db, owner, term):
    q = "SELECT id, title FROM documents WHERE owner = ? AND body LIKE ?"
    return db.execute(q, (owner, f"%{term}%")).fetchall()
