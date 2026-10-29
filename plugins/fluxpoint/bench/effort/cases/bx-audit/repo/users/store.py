def update_role(db, user_id, role):
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def get_role(db, user_id):
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None
