# ReliefLink

A disaster relief coordination application for **Team Proxy**. It connects community needs, warehouse inventory, feasible vehicle plans and verified deliveries in a single persistent workflow. Run locally with SQLite, or deploy the complete application to Vercel with persistent PostgreSQL.

For hosting, follow [Deploy to Vercel](DEPLOYMENT.md). The repository includes the FastAPI entrypoint, frontend build configuration and explicit database setup command.

## Start locally

Requires Python 3.10+ (Vercel uses 3.12) and a recent Node.js LTS release (Node 20.19+ or 22.12+). The first setup downloads Python and npm dependencies. With `DATABASE_URL` unset, no Docker, PostgreSQL server, hosting account or external AI key is needed.

```bash
cd /home/vikas/Projects/Idea/relieflink
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

Open **http://localhost:8000**. The FastAPI server serves both the API and the built React interface. Set `RELIEFLINK_PORT` to choose another port.

### Demo accounts

| Role | Email | Password |
| --- | --- | --- |
| Coordinator | coordinator@relieflink.local | ReliefLinkDemo! |
| Field worker | field@relieflink.local | ReliefLinkDemo! |

These are shared demo accounts for fictional data. SQLite is created once locally; PostgreSQL is initialised through an explicit command. Changes survive restarts. No public registration or browser-side role impersonation is provided.

## Try the full workflow

1. Sign in as the coordinator. Explore the five fictional affected locations, three warehouses and three vehicles.
2. Review or revise community needs. Record additional warehouse stock or mark a vehicle as unavailable.
3. Generate a plan. Inspect vehicle manifests, paths, remaining shortages, travel time and the solver's optimal/feasible status.
4. Approve a current proposal to reserve stock and vehicles. Stale or competing proposals must be regenerated.
5. Dispatch a tour. Every stop on the same vehicle is dispatched together, and warehouse stock is deducted once.
6. Verify actual received quantities for each delivery as the assigned field worker or coordinator. Partial receipts remain visible as discrepancies and unmet demand.
7. Refresh the page or restart the server: inventory, receipts and activity history remain in the configured database. Export the records from Reports.

Cancelling an approved proposal releases its undelivered reservations. If a tour has already departed, cancellation releases only the remaining reserved tours while preserving dispatched deliveries and receipt history. A vehicle returns to the available pool once every stop on its tour is verified.

## Planning model

The backend uses OR-Tools CP-SAT to jointly decide item quantities and vehicle tours. Stock is shared across vehicles at the same warehouse; every manifest must fit its vehicle and have a reachable departure-and-return route. Cargo originates from the vehicle's home warehouse, with no intermediate depot pickups in this MVP. Camps can receive split deliveries.

The primary objective minimises urgency-weighted unmet demand; the secondary objective minimises travel time while retaining the best shortage result found. Urgency weights are critical 5, high 3, moderate 2, low 1. Item importance weights are medical 3, water 2, food 1. These are transparent demonstration assumptions, not a validated humanitarian policy. The interface distinguishes feasible plans from proven optimal plans.

Food is counted in packets (0.8 kg each), drinking water in whole litres (1 kg each) and medical supplies in kits (1.5 kg each). Demand estimates use editable demonstration coefficients. Confirmed needs are stored as the location's current total target, rather than summed again each time a request is revised.

Routes use a **simulated local road graph**, including configurable blocked connections. They are planning demonstrations, not live road navigation. The map is drawn locally from saved coordinates and route paths; no map key or online tile service is required. Fonts have a system fallback when offline. There is no trained ML forecast or conversational AI assistant.

## Development and tests

```bash
npm run dev                 # FastAPI :8000 and Vite :5173
npm run build               # TypeScript validation and production build
.venv/bin/python -m pytest -q
```

Interactive API documentation: **http://localhost:8000/docs**. Mutating API calls use a signed-in session and the CSRF token returned by login or `/api/auth/me`. Permission checks happen on the server.

SQLite is stored at `data/relieflink.db`. Override `RELIEFLINK_DB` to use an independent database, including a separate test instance. Back up the database while the application is stopped. `data/` is excluded from source control.

Set server-only `DATABASE_URL` to use PostgreSQL. Environment variables take precedence over `.env.local` and `.env`; an explicit database argument in tests takes precedence over all of them. PostgreSQL setup is documented in [DEPLOYMENT.md](DEPLOYMENT.md). Schema migrations preserve existing records and are tracked in `schema_migrations`. Hosted app startup never migrates or seeds the database.

The integration tests also run against PostgreSQL when `RELIEFLINK_TEST_DATABASE_URL` is set to a direct connection to a **separate disposable database**. Every test uses its own temporary schema and removes it afterwards; tests never use the application's `DATABASE_URL`. The PostgreSQL user must be able to create and drop schemas. Without this variable, PostgreSQL cases are reported as skipped.

An explicit reset is available to coordinators in the interface. From the terminal:

```bash
.venv/bin/python -m backend.seed --reset
```

Reset replaces operational demo data in the configured database. Ordinary startup does not reset it. `python -m backend.seed --check` checks the schema and connectivity without modifying records.
