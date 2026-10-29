CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  deleted_at INTEGER
);
