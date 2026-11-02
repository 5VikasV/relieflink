"""Explicitly fictional, reproducible Bengaluru-area training scenario."""
from .db import ZERO, dumps, log, now, password_hash, put_meta


def seed_demo(db, reset=False):
    if reset:
        for table in ("deliveries", "plans", "roads", "vehicles", "warehouses", "locations", "activity"):
            db.execute(f"DELETE FROM {table}")
    users = [
        ("user-coordinator", "Demo Coordinator", "coordinator@relieflink.local", "coordinator"),
        ("user-field", "Demo Field Worker", "field@relieflink.local", "field_worker"),
    ]
    for user in users:
        if not db.execute("SELECT id FROM users WHERE id=?", (user[0],)).fetchone():
            db.execute("INSERT INTO users VALUES(?,?,?,?,?)", (*user, password_hash("ReliefLinkDemo!")))
    settings = {"food_per_person": 1.0, "water_per_person": 2.0, "medical_per_injured": 1.0}
    put_meta(db, "settings", dumps(settings))
    if not reset:
        put_meta(db, "version", "1")
    locations = [
        ("camp-a", "Riverside Shelter", 12.994, 77.623, 400, 48, "critical", "Field team · Sector A", "Fictional flood-response camp. Water supply interrupted.", {"food":450,"water":700,"medical":70}),
        ("camp-b", "Eastside Community Hall", 12.982, 77.651, 250, 21, "high", "Field team · Sector B", "Fictional camp with a growing food shortage.", {"food":350,"water":500,"medical":40}),
        ("camp-c", "North School Relief Camp", 13.027, 77.604, 320, 35, "high", "Field team · Sector C", "Fictional school serving as temporary accommodation.", {"food":400,"water":600,"medical":55}),
        ("camp-d", "West Ridge Settlement", 12.999, 77.566, 180, 12, "moderate", "Field team · Sector D", "Fictional settlement; road access can be simulated.", {"food":250,"water":400,"medical":25}),
        ("camp-e", "South Community Centre", 12.949, 77.588, 150, 8, "low", "Field team · Sector E", "Fictional community centre with a small buffer.", {"food":200,"water":300,"medical":20}),
    ]
    for row in locations:
        db.execute("INSERT INTO locations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*row[:-1], dumps(row[-1]), dumps(ZERO)))
    warehouses = [
        ("depot-alpha", "Central Depot", 12.974,77.603,{"food":800,"water":1100,"medical":100}),
        ("depot-beta", "North Supply Hub", 13.016,77.630,{"food":600,"water":800,"medical":80}),
        ("depot-gamma", "West Relief Store", 12.969,77.565,{"food":500,"water":900,"medical":80}),
    ]
    for row in warehouses:
        db.execute("INSERT INTO warehouses VALUES(?,?,?,?,?,?)", (*row[:-1], dumps(row[-1]), dumps(ZERO)))
    for row in [("vehicle-1","RL-01 · Relief truck","depot-alpha",1400),("vehicle-2","RL-02 · Cargo van","depot-beta",1000),("vehicle-3","RL-03 · All-terrain truck","depot-gamma",800)]:
        db.execute("INSERT INTO vehicles VALUES(?,?,?,?,?)", (*row,"available"))
    links = [
        ("depot-alpha","camp-a",12,3.8), ("depot-alpha","camp-b",16,5.6),
        ("depot-alpha","camp-e",13,4.1), ("depot-alpha","depot-gamma",17,5.0),
        ("depot-beta","camp-a",11,3.4), ("depot-beta","camp-c",10,3.2),
        ("depot-beta","camp-b",18,6.1), ("depot-gamma","camp-d",12,3.5),
        ("depot-gamma","camp-e",14,4.4), ("camp-a","camp-b",12,3.6),
        ("camp-a","camp-c",14,4.2), ("camp-c","camp-d",19,5.8),
        ("camp-d","camp-e",22,6.4), ("camp-d","depot-alpha",15,4.5),
    ]
    for i,row in enumerate(links,1):
        db.execute("INSERT INTO roads VALUES(?,?,?,?,?,?)", (f"road-{i}",*row,0))
    put_meta(db, "initialized", now())
    log(db,"System","demo_seeded","Loaded fictional flood scenario: 5 locations, 3 warehouses and 3 vehicles. Road times are modelled, not live navigation.")


def main():
    import argparse
    from .db import (DatabaseConfigurationError, DatabaseIntegrityError,
                     DatabaseUnavailableError, bump, check_ready, database_kind,
                     database_target, initialize, transaction)
    parser = argparse.ArgumentParser(description="Migrate and initialise ReliefLink once, without replacing existing records.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--reset", action="store_true", help="Reset all fictional operational records; retain users and sessions.")
    actions.add_argument("--check", action="store_true", help="Check connectivity and schema without changing any records.")
    args = parser.parse_args()
    try:
        target = database_target()
        if args.check:
            check_ready(target)
        else:
            initialize(target)
            if args.reset:
                with transaction(target) as db:
                    seed_demo(db, reset=True)
                    bump(db)
    except (DatabaseConfigurationError, DatabaseIntegrityError, DatabaseUnavailableError) as exc:
        parser.exit(1, f"Database setup failed: {exc}\n")
    # Never print a PostgreSQL connection URL: it can contain credentials.
    label = "PostgreSQL database" if database_kind(target) == "postgresql" else str(target)
    print(f"ReliefLink demonstration database {'reset' if args.reset else 'ready'}: {label}")


if __name__ == "__main__":
    main()
