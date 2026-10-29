# Audit every role change

Every change to a user's role must write an audit record, atomically with
the change.

- Add a table `role_audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL,
  user_id INTEGER NOT NULL, old_role TEXT, new_role TEXT NOT NULL,
  at INTEGER NOT NULL)` to `users/schema.sql`. Add `users.store.migrate(db)`
  that creates the table on an existing database that already has `users`
  (it must be safe to run twice and keep existing rows).
- `service.set_role(db, actor, user_id, role, now)` and
  `bulk.import_roles(db, actor, path, now)` take `now` (an int timestamp
  stored in `at`). `actor["email"]` is stored in `actor`. `old_role` is the
  role before the change.
- A role "change" to the same role writes no audit row and no update.
  Setting the role of a user id that does not exist raises `KeyError` and
  writes nothing.
- The role update and its audit row commit together or not at all. In the
  bulk import, the whole file is one transaction: if any line fails (bad
  user id, unknown user, wrong number of fields), no role changes and no
  audit rows from that file are kept, and the error propagates.
- `notify.role_changed` is called only after a successful commit, once per
  actual change.
- Add `store.audit_for(db, user_id)` returning that user's audit rows as
  tuples `(actor, old_role, new_role, at)` in insertion order.
