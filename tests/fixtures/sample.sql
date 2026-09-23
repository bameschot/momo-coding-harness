CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL
);
CREATE VIEW active_users AS SELECT * FROM users;
CREATE INDEX idx_users_email ON users (email);
CREATE FUNCTION add_one(x integer) RETURNS integer AS $$ SELECT x + 1 $$ LANGUAGE sql;
