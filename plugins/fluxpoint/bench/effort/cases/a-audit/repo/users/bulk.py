import csv
from users import store


def import_roles(db, actor, path):
    """Admin CSV import: user_id,role per line."""
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    with open(path) as f:
        for user_id, role in csv.reader(f):
            store.update_role(db, int(user_id), role)
    db.commit()
