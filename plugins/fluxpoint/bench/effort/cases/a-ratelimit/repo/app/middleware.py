def chain(middleware, handler):
    for m in reversed(middleware):
        handler = m(handler)
    return handler


def logging_mw(next_handler):
    def h(request):
        resp = next_handler(request)
        print(request["method"], request["path"], resp["status"])
        return resp
    return h
