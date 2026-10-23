CREATE TABLE IF NOT EXISTS hands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    room TEXT NOT NULL,
    hand_number INTEGER NOT NULL,
    played_at TEXT NOT NULL,
    title TEXT NOT NULL,
    events TEXT NOT NULL CHECK(json_valid(events)),
    event_hash TEXT NOT NULL,
    UNIQUE(session_id, hand_number)
);
CREATE INDEX IF NOT EXISTS idx_hands_room ON hands(room, id DESC);
