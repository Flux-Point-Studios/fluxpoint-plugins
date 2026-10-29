def nightly(db):
    """Runs at 02:00. Currently only vacuums."""
    db.execute("VACUUM")
