-- Named, publicly readable journals. journal_hands.owner now holds the book name.
CREATE TABLE IF NOT EXISTS journal_books (
  name TEXT PRIMARY KEY,
  pin_salt TEXT NOT NULL,
  pin_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fail_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT
);

-- Hands saved under the old private-code scheme have no book (production had none when this ran).
DELETE FROM journal_hands WHERE owner NOT IN (SELECT name FROM journal_books);
