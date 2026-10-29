# Soft delete for documents

- Add a nullable integer column `deleted_at` to `documents` in
  `docs/schema.sql`. Add `docs.repo.migrate(db)` that adds the column to an
  existing database created from the old schema, keeps every row, and is
  safe to run twice.
- `repo.delete(db, doc_id, now)` sets `deleted_at = now` (an int Unix
  timestamp) instead of removing the row. Deleting an already deleted
  document leaves its original `deleted_at`.
- `repo.restore(db, doc_id)` clears `deleted_at`. It returns True if the
  document was deleted and is now restored, False otherwise.
- Every existing read path stops returning soft-deleted documents:
  `repo.get`, `repo.list_for`, `search.search`, `export.export_owner` and
  `views.show` (404 for a deleted document).
- `jobs.nightly(db, now)` permanently removes documents whose `deleted_at`
  is more than 30 days (30 * 86400 seconds) before `now`; a document
  deleted exactly 30 days ago is kept. It still vacuums afterwards.
- Keep the result shapes of every existing function unchanged.
