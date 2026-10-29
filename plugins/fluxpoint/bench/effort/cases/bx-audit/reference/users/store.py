AUDIT = """CREATE TABLE IF NOT EXISTS role_audit (
  id INTEGER PRIMARY KEY, actor TEXT NOT NULL, user_id INTEGER NOT NULL,
  old_role TEXT, new_role TEXT NOT NULL, at INTEGER NOT NULL)"""


def migrate(db):
    db.execute(AUDIT)
    db.commit()


def update_role(db, user_id, role):
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def get_role(db, user_id):
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def change_role(db, actor, user_id, role, now):
    """Update and audit inside the caller's transaction. True if changed."""
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise KeyError(user_id)
    if row[0] == role:
        return False
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.execute("INSERT INTO role_audit (actor, user_id, old_role, new_role, at) VALUES (?, ?, ?, ?, ?)",
               (actor["email"], user_id, row[0], role, now))
    return True


def audit_for(db, user_id):
    return db.execute("SELECT actor, old_role, new_role, at FROM role_audit WHERE user_id = ? ORDER BY id",
                      (user_id,)).fetchall()
