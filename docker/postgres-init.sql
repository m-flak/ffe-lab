-- Runs once, when the pgdata volume is first created.
-- ffelab creates its own tables; it only needs the extension available.
CREATE EXTENSION IF NOT EXISTS vector;
-- A separate database for FFELAB_TEST_PG, so test runs never touch the real ledger.
CREATE DATABASE ffelab_test OWNER ffelab;
\connect ffelab_test
CREATE EXTENSION IF NOT EXISTS vector;
