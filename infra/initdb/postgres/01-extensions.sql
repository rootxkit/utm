-- Run once, on first start of the courier_pg_data volume.
--
-- Schema itself is owned by Alembic (P2-01). Only extensions belong here,
-- because an extension must exist before the first migration references it.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Used by the airspace and audit queries.
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Every timestamp in this system is UTC; the display layer converts.
-- DBNAME is a psql built-in, so this works whatever POSTGRES_DB is set to.
ALTER DATABASE :"DBNAME" SET timezone TO 'UTC';
