RETENTION = 30 * 86400


def nightly(db, now):
    """Runs at 02:00: purge expired soft deletes, then vacuum."""
    db.execute("DELETE FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < ?", (now - RETENTION,))
    db.commit()
    db.execute("VACUUM")
