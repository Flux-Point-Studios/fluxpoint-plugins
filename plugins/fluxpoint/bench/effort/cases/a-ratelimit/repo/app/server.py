from app import routes
from app.middleware import chain

MIDDLEWARE = []


def handle(request):
    return chain(MIDDLEWARE, routes.dispatch)(request)
