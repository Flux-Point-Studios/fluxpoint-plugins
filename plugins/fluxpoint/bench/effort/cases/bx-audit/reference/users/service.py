from users import store, notify


def set_role(db, actor, user_id, role, now):
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    try:
        changed = store.change_role(db, actor, user_id, role, now)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if changed:
        notify.role_changed(user_id, role)
