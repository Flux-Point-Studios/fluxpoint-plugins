import csv
from users import store, notify


def import_roles(db, actor, path, now):
    """Admin CSV import: user_id,role per line."""
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    changed = []
    try:
        with open(path) as f:
            for rec in csv.reader(f):
                user_id, role = rec
                if store.change_role(db, actor, int(user_id), role, now):
                    changed.append((int(user_id), role))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    for user_id, role in changed:
        notify.role_changed(user_id, role)
