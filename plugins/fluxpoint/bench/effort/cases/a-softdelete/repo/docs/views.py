from docs import repo


def show(db, user, doc_id):
    row = repo.get(db, doc_id)
    if not row or row[1] != user:
        return 404, None
    return 200, row
