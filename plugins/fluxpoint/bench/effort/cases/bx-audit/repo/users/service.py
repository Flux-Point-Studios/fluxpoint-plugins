from users import store, notify


def set_role(db, actor, user_id, role):
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    store.update_role(db, user_id, role)
    db.commit()
    notify.role_changed(user_id, role)
