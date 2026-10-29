CREATE TABLE orders (id INTEGER PRIMARY KEY, account TEXT, created_at TEXT, total INTEGER);
CREATE INDEX orders_account ON orders(account);
