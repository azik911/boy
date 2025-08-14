-- ===============================
--  Schema for Telegram loans bot
--  Postgres 13+ (TIMESTAMPTZ, BRIN supported)
-- ===============================

-- Safety: run inside a transaction
BEGIN;

-- ---------- Extensions (optional but safe) ----------
-- Uncomment if you want convenient gen_random_uuid(), etc.
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Offers (catalog) ----------
CREATE TABLE IF NOT EXISTS offers (
  slug        TEXT PRIMARY KEY,              -- stable key, e.g. 'vivus'
  title       TEXT NOT NULL,                 -- human readable name
  url         TEXT NOT NULL,                 -- partner URL (can be your redirect target later)
  active      BOOLEAN NOT NULL DEFAULT TRUE, -- to hide from UI without deleting
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at on UPDATE
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'trg_offers_set_updated_at'
  ) THEN
    CREATE TRIGGER trg_offers_set_updated_at
      BEFORE UPDATE ON offers
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_offers_active ON offers(active);

-- ---------- Users (hashed identifiers, no PII) ----------
CREATE TABLE IF NOT EXISTS users (
  id          BIGSERIAL PRIMARY KEY,
  uid_hash    TEXT UNIQUE NOT NULL,          -- sha256(user_id + ':' + USER_HASH_SALT)
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  blocked     BOOLEAN NOT NULL DEFAULT FALSE,
  blocked_at  TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_users_last_seen ON users(last_seen);
CREATE INDEX IF NOT EXISTS ix_users_blocked  ON users(blocked);

-- ---------- Clicks (event store) ----------
CREATE TABLE IF NOT EXISTS clicks (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  offer_slug  TEXT NOT NULL REFERENCES offers(slug) ON UPDATE CASCADE ON DELETE RESTRICT,
  country     TEXT NOT NULL CHECK (country IN ('RU','KZ')),
  uid_hash    TEXT NULL                      -- may be NULL if unknown/anonymous
);
CREATE INDEX IF NOT EXISTS ix_clicks_ts           ON clicks(ts);
CREATE INDEX IF NOT EXISTS ix_clicks_offer_ts     ON clicks(offer_slug, ts);
CREATE INDEX IF NOT EXISTS ix_clicks_country_ts   ON clicks(country, ts);
CREATE INDEX IF NOT EXISTS ix_clicks_uid_ts       ON clicks(uid_hash, ts);

-- For large volumes it’s efficient to add a BRIN index on time:
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE c.relname='brin_clicks_ts' AND n.nspname='public') THEN
    CREATE INDEX brin_clicks_ts ON clicks USING BRIN (ts);
  END IF;
END $$;

-- ---------- Deliveries (optional: message delivery outcomes) ----------
CREATE TABLE IF NOT EXISTS deliveries (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  uid_hash    TEXT NOT NULL,
  event       TEXT NOT NULL,                  -- 'ok' | 'blocked' | 'chat_not_found' | ...
  context     TEXT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_ts      ON deliveries(ts);
CREATE INDEX IF NOT EXISTS ix_deliveries_uid_ts  ON deliveries(uid_hash, ts);

-- ---------- Helpful views for analytics ----------
-- Daily clicks per offer
CREATE OR REPLACE VIEW v_clicks_by_offer_day AS
SELECT
  date_trunc('day', ts) AS d,
  offer_slug,
  COUNT(*) AS clicks
FROM clicks
GROUP BY 1, 2;

-- Daily total clicks (all offers)
CREATE OR REPLACE VIEW v_clicks_total_day AS
SELECT
  date_trunc('day', ts) AS d,
  COUNT(*) AS clicks
FROM clicks
GROUP BY 1;

-- ---------- Seed (example offers) ----------
-- Remove or change as needed
INSERT INTO offers (slug, title, url, active)
VALUES
  ('boostra',        'BOOSTRA',                        'https://clck.ru/3NatJf', TRUE),
  ('privet-sosed',   'Привет, сосед!',                 'https://clck.ru/3NauHa', TRUE),
  ('one-click-money','One Click Money',                'https://clck.ru/3NauMR', TRUE),
  ('vivus',          'Vivus',                          'https://clck.ru/3NauPz', TRUE),
  ('podbor-0',       'Подбор займа без процентов',     'https://clck.ru/3NauZp', TRUE),
  ('calc-potential','https://clck.ru/3NbeMg', true),
  ('best-terms','https://clck.ru/3NbeU8', true)
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title, url = EXCLUDED.url, active = EXCLUDED.active;

COMMIT;
-- users: кто запускал бота (если уже есть — пропусти)
CREATE TABLE IF NOT EXISTS users (
  telegram_id BIGINT PRIMARY KEY,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- персонализация short_links: к какому пользователю привязана короткая ссылка
ALTER TABLE short_links
  ADD COLUMN IF NOT EXISTS user_id BIGINT;

-- уникальные клики по пользователю и дню
CREATE TABLE IF NOT EXISTS clicks (
  id            BIGSERIAL PRIMARY KEY,
  short_link_id BIGINT NOT NULL REFERENCES short_links(id) ON DELETE CASCADE,
  user_id       BIGINT NOT NULL,
  day           DATE   NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- в сутки один клик на пару (ссылка, пользователь)
CREATE UNIQUE INDEX IF NOT EXISTS uq_clicks_unique_day
  ON clicks(short_link_id, user_id, day);

-- ускорим выборки по ссылке и пользователю
CREATE INDEX IF NOT EXISTS ix_clicks_link ON clicks(short_link_id);
CREATE INDEX IF NOT EXISTS ix_clicks_user ON clicks(user_id);

-- если у тебя есть offers и short_links.offer_id — сделай уникальность на пару (offer,user)
-- чтобы /s/new не плодил дубликаты для одного пользователя:
-- CREATE UNIQUE INDEX IF NOT EXISTS uq_short_links_offer_user
--   ON short_links(offer_id, user_id) WHERE user_id IS NOT NULL;

