# Deploy ReliefLink to Vercel

This deploys the React interface and FastAPI API together at one URL. PostgreSQL holds users, sessions, inventory, plans, receipts and activity independently of Vercel function instances. The local SQLite file remains available for offline development; it is not uploaded or automatically copied into the hosted database.

## 1. Create the hosted database

Create a PostgreSQL project in [Neon](https://console.neon.tech/). Choose a region close to the Vercel function region. Open **Connect**, select the application database and copy the PostgreSQL connection string. A pooled connection is supported; retain `sslmode=require` and any other connection options Neon supplies.

In this project's `relieflink` directory:

```bash
cp .env.example .env.local
```

Open `.env.local` in your editor and set the real connection string:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Keep this value server-side. Do not prefix it with `VITE_`, commit it, or put it in frontend source. `.env.local` is ignored by Git and Vercel uploads. Existing shell environment variables override values in this file.

## 2. Initialise the database once

If dependencies are not installed yet, run `./setup.sh`. It builds the interface and runs the same initialisation command shown below. For an existing installation, run:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m backend.seed
.venv/bin/python -m backend.seed --check
```

This applies missing schema migrations and creates the fictional five-location, three-warehouse, three-vehicle scenario only when the database has not been initialised. Repeating the command preserves subsequent edits, reservations and receipts. PostgreSQL writes use a transaction-scoped advisory lock so concurrent function instances cannot reserve the same snapshot twice. Planning runs outside this lock.

Do not add seeding or `--reset` to the Vercel build command. Future schema changes are also applied explicitly with `python -m backend.seed` before deploying code that requires them. Back up a populated database before applying migrations. The current migration also adopts existing SQLite records without resetting them.

## 3. Put the project on GitHub

Create an empty GitHub repository. Upload the contents of **`relieflink/`**, making `package.json`, `requirements.txt`, `app.py` and `vercel.json` the repository's top-level files. Include hidden configuration files such as `.python-version` and `.gitignore`.

Use Git from this directory:

```bash
git init -b main
git add .
git diff --cached --stat
git commit -m "Prepare ReliefLink for Vercel"
git remote add origin https://github.com/YOUR_USERNAME/relieflink.git
git push -u origin main
```

If the directory is already a Git repository, keep its existing repository and branch instead of initialising again. Review staged files before committing. `.gitignore` excludes `.venv/`, `node_modules/`, `dist/`, `data/`, environment files and local Vercel state. Keep database credentials in `.env.local` rather than other tracked files.

## 4. Import into Vercel

1. Open [Vercel](https://vercel.com/new), select **Add New → Project** and import the GitHub repository.
2. Confirm these settings:

   | Setting | Value |
   | --- | --- |
   | Root Directory | `./` if the repository contains the contents of `relieflink/`; otherwise select `relieflink` |
   | Framework Preset | **FastAPI** |
   | Build Command | `npm ci && npm run build` (already in `vercel.json`) |
   | Output Directory | Leave the FastAPI framework default; do not override it with `dist` |
   | Python | `3.12` (already in `.python-version`) |

3. Add **`DATABASE_URL`** as a server environment variable for **Production**, with the same connection string used in step 2.
4. Select **Deploy**. The root `app.py` exports the FastAPI application. It serves `/api/*` plus the built React interface and assets; no separate API URL or CORS setup is needed.

For Preview deployments, use a separate Neon branch/database and initialise it independently. Set its own `DATABASE_URL` for Preview. A preview sharing the production database also shares every edit and reset. Do not set `RELIEFLINK_DB` on Vercel: hosted mode requires PostgreSQL and fails explicitly if `DATABASE_URL` is missing.

The function timeout is set to 60 seconds in `vercel.json`. The solver is intended for the small demonstration scenario. Larger disaster networks require separate capacity testing and may need a background worker.

## 5. Verify the published application

Open `https://YOUR_PROJECT.vercel.app/api/health`. Expected response:

```json
{"status":"ok","database":"postgresql","schema_version":1,"demo":true}
```

A 503 response means connectivity or schema setup needs attention; run `python -m backend.seed --check` locally with the same database configured and inspect the Vercel function logs. Changing a Vercel environment variable requires a new deployment. An import/build error about `DATABASE_URL` means that variable was not configured for the deployment environment.

Open the main URL and use either demo account:

| Role | Email | Password |
| --- | --- | --- |
| Coordinator | `coordinator@relieflink.local` | `ReliefLinkDemo!` |
| Field worker | `field@relieflink.local` | `ReliefLinkDemo!` |

Report a need, generate and approve an allocation, dispatch a tour and verify a receipt. Refresh the page and redeploy the same commit: inventory, reservations, receipts and activity should remain. The header should show **Shared workspace**. The health endpoint verifies database readiness; this workflow verifies the deployment end to end.

These accounts and the reset action are deliberately shared for the public demonstration. Store fictional data only while they remain enabled.

## Local development afterwards

To return to SQLite, clear `DATABASE_URL` from your shell and `.env.local`; then run `./run.sh`. Your original local database remains at `data/relieflink.db`. To continue testing the hosted database locally, leave `DATABASE_URL` configured.

To run the full workflow suite against both database engines, set `RELIEFLINK_TEST_DATABASE_URL` in your shell to a direct connection to a disposable test database (not the hosted demo database) and run:

```bash
.venv/bin/python -m pytest -q
```

## Platform references

- [Vercel FastAPI deployment and custom build commands](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel Python runtime and dependency configuration](https://vercel.com/docs/functions/runtimes/python)
- [Neon connection instructions](https://neon.com/docs/connect/connect-from-any-app)
