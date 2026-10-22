"""initial schema

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id          UUID PRIMARY KEY,
            balance     BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
            created_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE models (
            id     SERIAL PRIMARY KEY,
            slug   TEXT NOT NULL UNIQUE,
            price  BIGINT NOT NULL CHECK (price > 0)
        );

        CREATE TABLE model_legs (
            id                  SERIAL PRIMARY KEY,
            model_id            INT NOT NULL REFERENCES models(id),
            provider            TEXT NOT NULL CHECK (provider IN ('alpha', 'beta')),
            priority            INT NOT NULL,
            max_ref_images      INT NOT NULL CHECK (max_ref_images >= 0),
            skip_on_moderation  BOOLEAN NOT NULL DEFAULT false,
            UNIQUE (model_id, priority)
        );

        CREATE TABLE tasks (
            id                  UUID PRIMARY KEY,
            user_id             UUID NOT NULL REFERENCES users(id),
            model_id            INT NOT NULL REFERENCES models(id),
            prompt              TEXT NOT NULL,
            ref_images          JSONB NOT NULL DEFAULT '[]',
            price               BIGINT NOT NULL CHECK (price > 0),
            status              TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            current_attempt_id  UUID NULL,
            result_url          TEXT NULL,
            failure_reason      TEXT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            finished_at         timestamptz NULL
        );
        CREATE INDEX tasks_running_by_user ON tasks (user_id) WHERE status = 'running';

        CREATE TABLE attempts (
            id                    UUID PRIMARY KEY,
            task_id               UUID NOT NULL REFERENCES tasks(id),
            leg_id                INT NOT NULL REFERENCES model_legs(id),
            provider              TEXT NOT NULL,
            status                TEXT NOT NULL CHECK (status IN
                                    ('pending', 'sent', 'completed', 'failed',
                                     'timed_out', 'abandoned')),
            error_type            TEXT NULL CHECK (error_type IN
                                    ('technical', 'moderation', 'payload', 'timeout')),
            error_source          TEXT NULL CHECK (error_source IN ('webhook', 'sync', 'watchdog')),
            error_detail          TEXT NULL,
            external_id           TEXT NULL,
            result_url            TEXT NULL,
            late_result_url       TEXT NULL,
            deadline_at           timestamptz NOT NULL,
            dispatch_lease_until  timestamptz NULL,
            dispatch_tries        INT NOT NULL DEFAULT 0,
            created_at            timestamptz NOT NULL DEFAULT now(),
            sent_at               timestamptz NULL,
            finished_at           timestamptz NULL,
            UNIQUE (provider, external_id)
        );
        CREATE INDEX attempts_by_task ON attempts (task_id, created_at);
        CREATE INDEX attempts_active_deadline ON attempts (deadline_at)
            WHERE status IN ('pending', 'sent');
        CREATE INDEX attempts_pending ON attempts (created_at) WHERE status = 'pending';

        ALTER TABLE tasks ADD CONSTRAINT tasks_current_attempt_fk
            FOREIGN KEY (current_attempt_id) REFERENCES attempts(id);

        CREATE TABLE ledger (
            id               BIGSERIAL PRIMARY KEY,
            user_id          UUID NOT NULL REFERENCES users(id),
            delta            BIGINT NOT NULL CHECK (delta <> 0),
            kind             TEXT NOT NULL CHECK (kind IN ('payment', 'charge', 'refund')),
            idempotency_key  TEXT NOT NULL UNIQUE,
            task_id          UUID NULL REFERENCES tasks(id),
            payment_id       TEXT NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ledger_by_user ON ledger (user_id, id DESC);

        CREATE TABLE payments (
            payment_id   TEXT PRIMARY KEY,
            user_id      UUID NOT NULL REFERENCES users(id),
            tokens       BIGINT NOT NULL CHECK (tokens > 0),
            status       TEXT NOT NULL,
            credited_at  timestamptz NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE webhook_events (
            id           BIGSERIAL PRIMARY KEY,
            source       TEXT NOT NULL,
            body         JSONB NOT NULL,
            outcome      TEXT NOT NULL,
            received_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE FUNCTION ledger_apply_delta() RETURNS trigger AS $$
        BEGIN
            UPDATE users SET balance = balance + NEW.delta WHERE id = NEW.user_id;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER ledger_apply_delta AFTER INSERT ON ledger
            FOR EACH ROW EXECUTE FUNCTION ledger_apply_delta();

        CREATE FUNCTION ledger_forbid_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger is append-only';
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger
            FOR EACH ROW EXECUTE FUNCTION ledger_forbid_change();
        CREATE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger
            FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_change();

        -- из ledger_apply_delta мы уже на глубине 2
        CREATE FUNCTION users_guard_balance() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.balance <> 0 THEN
                RAISE EXCEPTION 'balance must start at 0, use ledger';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.balance IS DISTINCT FROM OLD.balance
               AND pg_trigger_depth() < 2 THEN
                RAISE EXCEPTION 'balance is changed only through ledger';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER users_guard_balance BEFORE INSERT OR UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION users_guard_balance();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE webhook_events;
        DROP TABLE payments;
        DROP TABLE ledger;
        ALTER TABLE tasks DROP CONSTRAINT tasks_current_attempt_fk;
        DROP TABLE attempts;
        DROP TABLE tasks;
        DROP TABLE model_legs;
        DROP TABLE models;
        DROP TABLE users;
        DROP FUNCTION users_guard_balance();
        DROP FUNCTION ledger_forbid_change();
        DROP FUNCTION ledger_apply_delta();
        """
    )
