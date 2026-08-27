-- Database initialization script
-- Creates the app runtime role and migration role with appropriate privileges.
-- This runs ONCE when the PostgreSQL container is first created.

-- Create migration role (has DDL grants; used only by Alembic)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'aidoc_migration') THEN
    CREATE ROLE aidoc_migration WITH LOGIN PASSWORD 'aidoc_migration_password';
  END IF;
END
$$;

-- Grant migration role full privileges on the database
GRANT ALL PRIVILEGES ON DATABASE aidoc_db TO aidoc_migration;

-- Ensure the app user (aidoc_user) is created (it's already created by POSTGRES_USER env var)
-- Grant connect and usage
GRANT CONNECT ON DATABASE aidoc_db TO aidoc_user;

-- The migration role will own all objects; after each migration run, grant
-- read/write to aidoc_user via Alembic post-migration scripts.
-- For V1 development simplicity, aidoc_user gets full data manipulation rights
-- but NO DDL (CREATE TABLE, ALTER TABLE, etc.).
-- This is enforced by the Alembic env.py using a separate connection string for migrations.
