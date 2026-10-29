import unittest
from app.server import handle


class T(unittest.TestCase):
    def test_bad_password(self):
        r = handle({"method": "POST", "path": "/login", "form": {"user": "demo", "password": "x"},
                    "remote_addr": "1.2.3.4", "headers": {}})
        self.assertEqual(r["status"], 401)
