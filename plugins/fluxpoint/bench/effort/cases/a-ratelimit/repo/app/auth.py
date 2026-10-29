def login(request):
    user, pw = request["form"].get("user"), request["form"].get("password")
    ok = user == "demo" and pw == "demo"
    return {"status": 200 if ok else 401, "headers": {}, "body": b""}
