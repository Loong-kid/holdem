-- Hand journal: hands a player records by hand from live games, keyed by a hashed personal code.
CREATE TABLE IF NOT EXISTS journal_hands (
    owner TEXT NOT NULL,
    hand_id TEXT NOT NULL,
    played_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL CHECK(json_valid(data)),
    PRIMARY KEY (owner, hand_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_owner_played ON journal_hands(owner, played_at DESC, updated_at DESC);
