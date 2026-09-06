-- VerseWell unified D1 schema (task-03)
-- One schema for every Bible version; footnotes/section_intros are simply
-- empty for versions that lack them. See ARCHITECTURE.md for source mapping.

CREATE TABLE IF NOT EXISTS versions (
  code          TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  language      TEXT NOT NULL DEFAULT 'en',
  description   TEXT,
  has_footnotes INTEGER NOT NULL DEFAULT 0,
  has_intros    INTEGER NOT NULL DEFAULT 0,
  source_file   TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  verse_count   INTEGER NOT NULL DEFAULT 0,
  imported_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- NOTE: no REFERENCES versions(code) FK here. D1 enforces foreign keys and
-- the import pipeline inserts the versions row LAST (it is the atomic
-- completion marker), so an enforced FK would reject every verses INSERT.
-- Referential integrity is guaranteed by the import pipeline itself.
CREATE TABLE IF NOT EXISTS verses (
  version    TEXT NOT NULL,
  book       TEXT NOT NULL,
  book_name  TEXT NOT NULL,
  book_order INTEGER NOT NULL,
  chapter    INTEGER NOT NULL,
  verse      INTEGER NOT NULL,
  text       TEXT NOT NULL,
  PRIMARY KEY (version, book, chapter, verse)
);

CREATE TABLE IF NOT EXISTS footnotes (
  version   TEXT NOT NULL,
  book      TEXT NOT NULL,
  chapter   INTEGER NOT NULL,
  verse     INTEGER NOT NULL,
  marker    TEXT,
  note_text TEXT NOT NULL,
  PRIMARY KEY (version, book, chapter, verse, marker)
);

CREATE TABLE IF NOT EXISTS section_intros (
  version     TEXT NOT NULL,
  book        TEXT NOT NULL,
  chapter     INTEGER NOT NULL,
  start_verse INTEGER NOT NULL,
  end_verse   INTEGER NOT NULL,
  intro_text  TEXT NOT NULL,
  PRIMARY KEY (version, book, chapter, start_verse)
);

CREATE INDEX IF NOT EXISTS idx_verses_lookup    ON verses(version, book, chapter, verse);
CREATE INDEX IF NOT EXISTS idx_footnotes_lookup ON footnotes(version, book, chapter, verse);
CREATE INDEX IF NOT EXISTS idx_intros_lookup    ON section_intros(version, book, chapter);
