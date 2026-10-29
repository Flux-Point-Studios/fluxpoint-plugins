from app import auth


def dispatch(request):
    if request["path"] == "/login":
        return auth.login(request)
    return {"status": 404, "headers": {}, "body": b""}
