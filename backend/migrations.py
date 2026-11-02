"""Additive schema migrations, shared by SQLite and PostgreSQL.

Migration 1 adopts the original local database without replacing its data.
Append new versions instead of changing an applied migration.
"""

MIGRATIONS = ((1, (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('coordinator','field_worker')),password_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
      csrf_token TEXT NOT NULL,expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS locations (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,lat DOUBLE PRECISION NOT NULL,lng DOUBLE PRECISION NOT NULL,
      population INTEGER NOT NULL CHECK(population>=0),injured INTEGER NOT NULL CHECK(injured>=0),
      severity TEXT NOT NULL,contact TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',
      demands TEXT NOT NULL,received TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS warehouses (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,lat DOUBLE PRECISION NOT NULL,lng DOUBLE PRECISION NOT NULL,
      stock TEXT NOT NULL,reserved TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS vehicles (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
      capacity_kg DOUBLE PRECISION NOT NULL CHECK(capacity_kg>0),status TEXT NOT NULL DEFAULT 'available')""",
    """CREATE TABLE IF NOT EXISTS roads (
      id TEXT PRIMARY KEY,from_id TEXT NOT NULL,to_id TEXT NOT NULL,
      minutes DOUBLE PRECISION NOT NULL CHECK(minutes>0),distance_km DOUBLE PRECISION NOT NULL CHECK(distance_km>0),
      blocked INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS plans (
      id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,input_version INTEGER NOT NULL,
      result TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS deliveries (
      id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES plans(id),vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
      warehouse_id TEXT NOT NULL REFERENCES warehouses(id),location_id TEXT NOT NULL REFERENCES locations(id),
      assigned_user_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL,
      manifest TEXT NOT NULL,received TEXT,recipient TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',
      sequence INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,verified_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS activity (
      id TEXT PRIMARY KEY,created_at TEXT NOT NULL,user_name TEXT NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(status)",
    "CREATE INDEX IF NOT EXISTS deliveries_plan_vehicle ON deliveries(plan_id,vehicle_id)",
)),)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
