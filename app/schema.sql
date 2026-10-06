-- Bus transfer fare clearing schema. Idempotent: safe to run on every boot.

CREATE TABLE IF NOT EXISTS trips (
    trip_no      TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    board_time   TIMESTAMPTZ NOT NULL,
    base_fare    INTEGER NOT NULL CHECK (base_fare >= 0),
    version      INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    op_date      DATE NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trips_account_day
    ON trips (account_id, op_date)
    WHERE status = 'active';

-- One settlement row per account / operating day.
CREATE TABLE IF NOT EXISTS daily_settlements (
    account_id   TEXT NOT NULL,
    op_date      DATE NOT NULL,
    raw_fare     INTEGER NOT NULL DEFAULT 0,
    charged_fare INTEGER NOT NULL DEFAULT 0,
    cap_cents    INTEGER NOT NULL DEFAULT 2000,
    trip_count   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'settled'
                 CHECK (status IN ('settled', 'recalculated')),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, op_date)
);

CREATE TABLE IF NOT EXISTS transfer_groups (
    group_id     BIGSERIAL PRIMARY KEY,
    account_id   TEXT NOT NULL,
    op_date      DATE NOT NULL,
    group_index  INTEGER NOT NULL,
    group_fare   INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, op_date, group_index),
    FOREIGN KEY (account_id, op_date)
        REFERENCES daily_settlements (account_id, op_date)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id     BIGINT NOT NULL REFERENCES transfer_groups (group_id) ON DELETE CASCADE,
    trip_no      TEXT NOT NULL REFERENCES trips (trip_no),
    leg_order    INTEGER NOT NULL,
    base_fare    INTEGER NOT NULL,
    PRIMARY KEY (group_id, trip_no)
);

-- Append-only money ledger. seq is a globally monotonic sequence in
-- submission order; previously returned rows can never be rewritten.
CREATE TABLE IF NOT EXISTS journals (
    seq          BIGSERIAL PRIMARY KEY,
    account_id   TEXT NOT NULL,
    op_date      DATE NOT NULL,
    amount       INTEGER NOT NULL,
    entry_type   TEXT NOT NULL CHECK (entry_type IN ('charge', 'reversal')),
    reason       TEXT NOT NULL,
    trip_no      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_journals_account_seq
    ON journals (account_id, seq);
CREATE INDEX IF NOT EXISTS idx_journals_day
    ON journals (account_id, op_date);

-- Enforce append-only semantics at the database level.
CREATE OR REPLACE FUNCTION journals_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'journals is append-only; % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_journals_no_update ON journals;
CREATE TRIGGER trg_journals_no_update BEFORE UPDATE ON journals
    FOR EACH ROW EXECUTE FUNCTION journals_block_mutation();

DROP TRIGGER IF EXISTS trg_journals_no_delete ON journals;
CREATE TRIGGER trg_journals_no_delete BEFORE DELETE ON journals
    FOR EACH ROW EXECUTE FUNCTION journals_block_mutation();

-- TRUNCATE is blocked via a separate statement-level trigger.
DROP TRIGGER IF EXISTS trg_journals_no_truncate ON journals;
CREATE TRIGGER trg_journals_no_truncate BEFORE TRUNCATE ON journals
    FOR EACH STATEMENT EXECUTE FUNCTION journals_block_mutation();
