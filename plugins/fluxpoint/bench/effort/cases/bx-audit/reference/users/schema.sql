CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, role TEXT);
CREATE TABLE role_audit (
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  old_role TEXT,
  new_role TEXT NOT NULL,
  at INTEGER NOT NULL
);
