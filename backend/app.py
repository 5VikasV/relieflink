"""ReliefLink API and its authoritative inventory workflow."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .db import (GRAMS, ITEMS, ZERO, DatabaseIntegrityError,
                 DatabaseUnavailableError, bump, check_ready, connect,
                 database_kind, database_target, dumps, initialize, is_postgres,
                 log, now, password_matches, public_user, read_state,
                 read_transaction, transaction, uid, version)
from .migrations import LATEST_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
Units = Annotated[int, Field(strict=True, ge=0, le=10_000_000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Severity = Literal["low", "moderate", "high", "critical"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Quantities(Input):
    food: Units = 0
    water: Units = 0
    medical: Units = 0


class Login(Input):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=512)]


class LocationCreate(Input):
    name: Name
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    population: Units
    injured: Units = 0
    severity: Severity = "moderate"
    contact: str = Field(default="", max_length=250)
    notes: str = Field(default="", max_length=2000)
    demands: Quantities


class LocationPatch(Input):
    name: Name | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    population: Units | None = None
    injured: Units | None = None
    severity: Severity | None = None
    contact: str | None = Field(default=None, max_length=250)
    notes: str | None = Field(default=None, max_length=2000)
    demands: Quantities | None = None


class NeedRequest(Quantities):
    location_id: Name
    notes: str = Field(default="", max_length=2000)


class WarehouseCreate(Input):
    name: Name
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    stock: Quantities


class VehicleCreate(Input):
    name: Name
    warehouse_id: Name
    capacity_kg: float = Field(gt=0, le=1_000_000)


class VehiclePatch(Input):
    status: Literal["available", "maintenance"]


class RoadPatch(Input):
    blocked: bool


class Receipt(Quantities):
    recipient: Name
    notes: str = Field(default="", max_length=2000)


class Settings(Input):
    food_per_person: float = Field(ge=0, le=100)
    water_per_person: float = Field(ge=0, le=100)
    medical_per_injured: float = Field(ge=0, le=100)


class Reset(Input):
    confirm: Literal[True]


def create_app(db_path: str | Path | None = None):
    path = database_target(db_path)
    if not is_postgres(path):
        initialize(path)
    app = FastAPI(title="ReliefLink relief coordination", version="1.1.0")
    app.state.db_path = path

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        messages = [f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})

    @app.exception_handler(DatabaseIntegrityError)
    async def integrity_error(_request, _exc):
        return JSONResponse(status_code=409, content={"detail": "This operation conflicts with existing records. Refresh and try again."})

    @app.exception_handler(DatabaseUnavailableError)
    async def database_error(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "The database is unavailable or not initialised. Check database setup and try again."})

    def authenticated(request: Request):
        token = request.cookies.get("relieflink_session", "")
        if not token:
            raise HTTPException(401, "Sign in to continue.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(connect(path)) as db:
            row = db.execute("SELECT s.csrf_token,s.expires_at,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (digest,)).fetchone()
        if not row or row["expires_at"] <= now():
            raise HTTPException(401, "Your session has expired. Please sign in again.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            submitted = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(submitted.encode(), row["csrf_token"].encode()):
                raise HTTPException(403, "The session security token is missing or invalid. Refresh and try again.")
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}
            if origin and origin not in allowed:
                raise HTTPException(403, "This request came from an untrusted origin.")
        return {**public_user(row), "csrf_token": row["csrf_token"]}

    def coordinator(user=Depends(authenticated)):
        if user["role"] != "coordinator":
            raise HTTPException(403, "A coordinator account is required for this action.")
        return user

    def require(db, table, ident):
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "The requested record does not exist.")
        return dict(row)

    def current_location(db, ident):
        require(db, "locations", ident)
        return next(l for l in read_state(db)["locations"] if l["id"] == ident)

    def check_demands(location, targets):
        for item in ITEMS:
            if targets[item] < location["received"][item] + location["committed"][item]:
                raise HTTPException(409, f"{item.title()} need cannot be below supplies already received or committed. Cancel undelivered reservations before reducing it.")

    def link_node(db, ident, lat, lng):
        nodes = list(db.execute("SELECT id,lat,lng FROM locations WHERE id<>? UNION ALL SELECT id,lat,lng FROM warehouses WHERE id<>?", (ident, ident)))
        def distance(node):
            p1, p2 = math.radians(lat), math.radians(node["lat"])
            dlat = p2-p1
            dlng = math.radians(node["lng"]-lng)
            a = math.sin(dlat/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dlng/2)**2
            return 6371*2*math.asin(min(1,math.sqrt(a)))
        for node in sorted(nodes, key=distance)[:2]:
            km = round(max(.2, distance(node)*1.3), 2)
            minutes = round(max(2, km/20*60), 1)
            db.execute("INSERT INTO roads VALUES(?,?,?,?,?,0)", (uid("road"), ident, node["id"], minutes, km))

    @app.get("/api/health")
    def health():
        check_ready(path)
        return {"status": "ok", "database": database_kind(path), "schema_version": LATEST_SCHEMA_VERSION, "demo": True}

    @app.post("/api/auth/login")
    def login(payload: Login, response: Response, request: Request):
        origin = request.headers.get("origin")
        if origin and origin not in {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}:
            raise HTTPException(403, "This sign-in request came from an untrusted origin.")
        with transaction(path) as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (payload.email.strip().lower(),)).fetchone()
            if not row or not password_matches(payload.password, row["password_hash"]):
                raise HTTPException(401, "Email or password is incorrect.")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expiry = (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()
            db.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], csrf, expiry))
            log(db, row["name"], "signed_in", "Signed in to the demonstration workspace.")
            user = public_user(row)
        response.set_cookie("relieflink_session", token, max_age=43200, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
        return {"user": user, "csrf_token": csrf}

    @app.get("/api/auth/me")
    def me(user=Depends(authenticated)):
        return {"user": {k:v for k,v in user.items() if k != "csrf_token"}, "csrf_token": user["csrf_token"]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(authenticated)):
        with transaction(path) as db:
            digest = hashlib.sha256(request.cookies.get("relieflink_session", "").encode()).hexdigest()
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        response.delete_cookie("relieflink_session", path="/")
        return {"ok": True}

    @app.get("/api/state")
    def state(user=Depends(authenticated)):
        with read_transaction(path) as db:
            return read_state(db, {k:v for k,v in user.items() if k != "csrf_token"})

    @app.post("/api/locations", status_code=201)
    def add_location(payload: LocationCreate, user=Depends(coordinator)):
        if payload.injured > payload.population:
            raise HTTPException(422, "Injured people cannot exceed the reported population.")
        ident = uid("camp")
        with transaction(path) as db:
            db.execute("INSERT INTO locations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident,payload.name.strip(),payload.lat,payload.lng,payload.population,payload.injured,payload.severity,payload.contact,payload.notes,dumps(payload.demands.model_dump()),dumps(ZERO)))
            link_node(db,ident,payload.lat,payload.lng)
            bump(db); log(db,user["name"],"location_created",f"Registered {payload.name}; modelled connections added to the two nearest nodes.")
            return current_location(db,ident)

    @app.patch("/api/locations/{ident}")
    def edit_location(ident: str, payload: LocationPatch, user=Depends(coordinator)):
        changes = payload.model_dump(exclude_unset=True,exclude_none=True)
        with transaction(path) as db:
            loc = current_location(db,ident)
            if changes.get("injured",loc["injured"]) > changes.get("population",loc["population"]):
                raise HTTPException(422,"Injured people cannot exceed the reported population.")
            if "demands" in changes:
                check_demands(loc,changes["demands"])
                changes["demands"] = dumps(changes["demands"])
            if changes:
                db.execute("UPDATE locations SET "+",".join(f"{k}=?" for k in changes)+" WHERE id=?", (*changes.values(),ident))
                if "lat" in changes or "lng" in changes:
                    db.execute("DELETE FROM roads WHERE from_id=? OR to_id=?", (ident,ident))
                    link_node(db,ident,changes.get("lat",loc["lat"]),changes.get("lng",loc["lng"]))
                bump(db); log(db,user["name"],"location_updated",f"Updated {loc['name']}.")
            return current_location(db,ident)

    @app.post("/api/requests")
    def submit_need(payload: NeedRequest, user=Depends(authenticated)):
        targets = {i:getattr(payload,i) for i in ITEMS}
        with transaction(path) as db:
            loc = current_location(db,payload.location_id)
            check_demands(loc,targets)
            db.execute("UPDATE locations SET demands=?,notes=? WHERE id=?", (dumps(targets),payload.notes or loc["notes"],payload.location_id))
            bump(db); log(db,user["name"],"needs_updated",f"Revised total need for {loc['name']}: {targets['food']} packets, {targets['water']} L, {targets['medical']} medical kits. {payload.notes}".strip())
            return current_location(db,payload.location_id)

    @app.post("/api/warehouses", status_code=201)
    def add_warehouse(payload: WarehouseCreate, user=Depends(coordinator)):
        ident = uid("depot")
        with transaction(path) as db:
            db.execute("INSERT INTO warehouses VALUES(?,?,?,?,?,?)", (ident,payload.name.strip(),payload.lat,payload.lng,dumps(payload.stock.model_dump()),dumps(ZERO)))
            link_node(db,ident,payload.lat,payload.lng)
            bump(db); log(db,user["name"],"warehouse_created",f"Registered {payload.name} and its opening stock.")
        return {"id":ident,"ok":True}

    @app.post("/api/warehouses/{ident}/restock")
    def restock(ident: str, payload: Quantities, user=Depends(coordinator)):
        values = payload.model_dump()
        if not any(values.values()):
            raise HTTPException(422,"Enter at least one positive stock quantity.")
        with transaction(path) as db:
            wh = require(db,"warehouses",ident); stock = json.loads(wh["stock"])
            for item in ITEMS:
                stock[item] += values[item]
                if stock[item] > 10_000_000:
                    raise HTTPException(422,"Stock exceeds the demonstration limit.")
            db.execute("UPDATE warehouses SET stock=? WHERE id=?", (dumps(stock),ident))
            bump(db); log(db,user["name"],"stock_received",f"Restocked {wh['name']}: {values['food']} packets, {values['water']} L, {values['medical']} medical kits.")
        return {"ok":True}

    @app.post("/api/vehicles", status_code=201)
    def add_vehicle(payload: VehicleCreate, user=Depends(coordinator)):
        ident = uid("vehicle")
        with transaction(path) as db:
            require(db,"warehouses",payload.warehouse_id)
            db.execute("INSERT INTO vehicles VALUES(?,?,?,?,?)", (ident,payload.name.strip(),payload.warehouse_id,payload.capacity_kg,"available"))
            bump(db); log(db,user["name"],"vehicle_created",f"Registered {payload.name} with {payload.capacity_kg:g} kg capacity.")
        return {"id":ident,"ok":True}

    @app.patch("/api/vehicles/{ident}")
    def vehicle_status(ident: str, payload: VehiclePatch, user=Depends(coordinator)):
        with transaction(path) as db:
            v = require(db,"vehicles",ident)
            if v["status"] not in ("available","maintenance"):
                raise HTTPException(409,"This vehicle is reserved or on a delivery tour.")
            db.execute("UPDATE vehicles SET status=? WHERE id=?", (payload.status,ident))
            bump(db); log(db,user["name"],"vehicle_updated",f"Set {v['name']} to {payload.status}.")
        return {"ok":True}

    @app.patch("/api/roads/{ident}")
    def road_status(ident: str, payload: RoadPatch, user=Depends(coordinator)):
        with transaction(path) as db:
            require(db,"roads",ident)
            db.execute("UPDATE roads SET blocked=? WHERE id=?", (int(payload.blocked),ident))
            bump(db); log(db,user["name"],"road_updated",f"Modelled road {ident} {'closed' if payload.blocked else 'reopened'}. Generate a fresh plan before dispatch.")
        return {"ok":True}

    @app.patch("/api/settings")
    def settings(payload: Settings, user=Depends(coordinator)):
        with transaction(path) as db:
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (dumps(payload.model_dump()),))
            log(db,user["name"],"settings_updated","Updated illustrative estimation coefficients. Existing confirmed needs remain unchanged.")
        return payload.model_dump()

    @app.post("/api/plans/generate", status_code=201)
    def generate_plan(user=Depends(coordinator)):
        from .optimizer import solve_plan
        with read_transaction(path) as db:
            snapshot = read_state(db)
        result = solve_plan(snapshot)
        if result.get("status") not in ("optimal","feasible","empty","infeasible"):
            raise HTTPException(500,"The planner did not return a valid result.")
        plan = {"id":uid("plan"),"status":"draft","created_at":now(),"input_version":snapshot["version"],"result":result}
        with transaction(path) as db:
            db.execute("INSERT INTO plans VALUES(?,?,?,?,?)", (plan["id"],plan["status"],plan["created_at"],plan["input_version"],dumps(result)))
            log(db,user["name"],"plan_generated",f"Generated {plan['id']} from input version {snapshot['version']}: {result.get('total_kg',0):g} kg, {result.get('status')} solution.")
        return plan

    @app.post("/api/plans/{ident}/approve")
    def approve_plan(ident: str, user=Depends(coordinator)):
        with transaction(path) as db:
            plan = require(db,"plans",ident)
            if plan["status"] == "approved":
                return {"ok":True,"id":ident,"already_approved":True}
            if plan["status"] != "draft":
                raise HTTPException(409,"Only a draft plan can be approved.")
            if plan["input_version"] != version(db):
                raise HTTPException(409,"This proposal is stale because needs, stock, vehicles or roads changed. Generate a fresh plan.")
            result = json.loads(plan["result"])
            if not result.get("routes"):
                raise HTTPException(409,"There are no feasible deliveries to approve. Check stock, roads and available vehicles.")
            snapshot = read_state(db)
            warehouses = {w["id"]:w for w in snapshot["warehouses"]}
            vehicles = {v["id"]:v for v in snapshot["vehicles"]}
            locations = {l["id"]:l for l in snapshot["locations"]}
            stocks = {wid:dict(ZERO) for wid in warehouses}
            assignments = {lid:dict(ZERO) for lid in locations}
            routes = result["routes"]
            used = set()
            for route in routes:
                vid, wid = route["vehicle_id"],route["warehouse_id"]
                vehicle = vehicles.get(vid)
                if not vehicle or vid in used or vehicle["status"] != "available" or vehicle["warehouse_id"] != wid:
                    raise HTTPException(409,"Vehicle availability or warehouse assignment changed. Replan.")
                used.add(vid); grams = 0
                if not route.get("stops"):
                    raise HTTPException(409,"The plan contains a route without a manifest.")
                for stop in route["stops"]:
                    lid = stop["location_id"]
                    if lid not in locations:
                        raise HTTPException(409,"A delivery location no longer exists.")
                    quantities = stop["manifest"]
                    if any(type(quantities.get(i)) is not int or quantities[i]<0 for i in ITEMS) or not any(quantities.values()):
                        raise HTTPException(409,"The plan contains invalid quantities.")
                    for item in ITEMS:
                        stocks[wid][item] += quantities[item]
                        assignments[lid][item] += quantities[item]
                        grams += quantities[item]*GRAMS[item]
                if grams > round(vehicle["capacity_kg"]*1000):
                    raise HTTPException(409,"The plan exceeds a vehicle's payload. Generate a new proposal.")
            for wid,qty in stocks.items():
                wh = warehouses[wid]
                for item in ITEMS:
                    if qty[item] > wh["stock"][item]-wh["reserved"][item]:
                        raise HTTPException(409,"Available stock changed. Generate a fresh plan.")
                    wh["reserved"][item] += qty[item]
                db.execute("UPDATE warehouses SET reserved=? WHERE id=?", (dumps(wh["reserved"]),wid))
            for lid,qty in assignments.items():
                loc = locations[lid]
                if any(qty[i] > max(0,loc["demands"][i]-loc["received"][i]-loc["committed"][i]) for i in ITEMS):
                    raise HTTPException(409,"This proposal exceeds the remaining need. Generate a fresh plan.")
            field = db.execute("SELECT id FROM users WHERE role='field_worker' ORDER BY id LIMIT 1").fetchone()
            if not field:
                raise HTTPException(409,"A field worker account is required before dispatch planning.")
            for route in routes:
                db.execute("UPDATE vehicles SET status='reserved' WHERE id=?", (route["vehicle_id"],))
                for index,stop in enumerate(route["stops"],1):
                    db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (uid("delivery"),ident,route["vehicle_id"],route["warehouse_id"],stop["location_id"],field["id"],"reserved",dumps(stop["manifest"]),None,"","",index,now(),None))
            db.execute("UPDATE plans SET status='approved' WHERE id=?", (ident,))
            bump(db); log(db,user["name"],"plan_approved",f"Approved {ident}; reserved stock and {len(routes)} vehicle tours atomically.")
        return {"ok":True,"id":ident}

    @app.post("/api/plans/{ident}/cancel")
    def cancel_plan(ident: str, user=Depends(coordinator)):
        with transaction(path) as db:
            plan = require(db,"plans",ident)
            if plan["status"] == "cancelled":
                return {"ok":True,"already_cancelled":True}
            deliveries = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE plan_id=?", (ident,))]
            if plan["status"] not in ("draft","approved"):
                raise HTTPException(409,"This plan cannot be cancelled.")
            reserved_deliveries = [d for d in deliveries if d["status"] == "reserved"]
            if plan["status"] == "approved" and not reserved_deliveries:
                raise HTTPException(409,"There are no undelivered reservations left to cancel; dispatched or verified supplies are retained.")
            for delivery in deliveries:
                if delivery["status"] == "reserved":
                    wh = require(db,"warehouses",delivery["warehouse_id"])
                    reserved = json.loads(wh["reserved"]); manifest = json.loads(delivery["manifest"])
                    for item in ITEMS:
                        reserved[item] -= manifest[item]
                    db.execute("UPDATE warehouses SET reserved=? WHERE id=?", (dumps(reserved),wh["id"]))
            for vid in {d["vehicle_id"] for d in reserved_deliveries}:
                db.execute("UPDATE vehicles SET status='available' WHERE id=?", (vid,))
            db.execute("UPDATE deliveries SET status='cancelled' WHERE plan_id=? AND status='reserved'", (ident,))
            active = any(d["status"] == "dispatched" for d in deliveries)
            has_receipts = any(d["status"] == "verified" for d in deliveries)
            new_status = "approved" if active else "completed" if has_receipts else "cancelled"
            db.execute("UPDATE plans SET status=? WHERE id=?", (new_status,ident))
            if reserved_deliveries:
                bump(db)
            log(db,user["name"],"plan_cancelled",f"Cancelled remaining reservations on {ident}; dispatched and verified records retained.")
        return {"ok":True}

    @app.post("/api/deliveries/{ident}/dispatch")
    def dispatch(ident: str, user=Depends(coordinator)):
        with transaction(path) as db:
            target = require(db,"deliveries",ident)
            if target["status"] in ("dispatched","verified"):
                return {"ok":True,"already_dispatched":True}
            if target["status"] != "reserved":
                raise HTTPException(409,"Only reserved deliveries can be dispatched.")
            # Re-evaluate connectivity after any road edits before loading a tour.
            plan = require(db,"plans",target["plan_id"])
            result = json.loads(plan["result"])
            route = next((r for r in result["routes"] if r["vehicle_id"]==target["vehicle_id"]),None)
            if not route:
                raise HTTPException(409,"The approved route is missing. Cancel and replan.")
            # Rebuild each leg against the current graph, including relocated nodes.
            # An unrelated closure must not strand otherwise feasible reservations.
            from .optimizer import _network
            nodes,paths,costs = _network(read_state(db))
            stops = [route["warehouse_id"],*[s["location_id"] for s in route["stops"]],route["warehouse_id"]]
            geometry=[]; travel=0; distance=0
            for start,end in zip(stops,stops[1:]):
                if (start,end) not in paths:
                    raise HTTPException(409,"A delivery stop is unreachable on the current road network. Cancel remaining reservations and generate a fresh route.")
                travel += costs[start,end][0]/1000
                distance += costs[start,end][1]
                for node_id in paths[start,end]:
                    point=[nodes[node_id]["lat"],nodes[node_id]["lng"]]
                    if not geometry or point != geometry[-1]:
                        geometry.append(point)
            rerouted = route.get("path") != geometry or route.get("travel_minutes") != round(travel,3) or route.get("distance_km") != round(distance,3)
            route.update(path=geometry,travel_minutes=round(travel,3),distance_km=round(distance,3))
            if rerouted:
                result["status"]="feasible"
                result["explanation"]="Approved quantities and stop order were retained. Route legs were revalidated against the updated modelled road network at dispatch; this revised route is feasible, not a fresh proof of optimality."
            result["travel_minutes"]=round(sum(r["travel_minutes"] for r in result["routes"]),3)
            result["distance_km"]=round(sum(r["distance_km"] for r in result["routes"]),3)
            db.execute("UPDATE plans SET result=? WHERE id=?", (dumps(result),target["plan_id"]))
            tour = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE plan_id=? AND vehicle_id=? AND status='reserved'", (target["plan_id"],target["vehicle_id"]))]
            wh = require(db,"warehouses",target["warehouse_id"])
            stock,reserved = json.loads(wh["stock"]),json.loads(wh["reserved"])
            for delivery in tour:
                manifest = json.loads(delivery["manifest"])
                for item in ITEMS:
                    stock[item] -= manifest[item]; reserved[item] -= manifest[item]
                    if stock[item]<0 or reserved[item]<0:
                        raise HTTPException(409,"Stock reservation is inconsistent. Dispatch was not applied.")
            db.execute("UPDATE warehouses SET stock=?,reserved=? WHERE id=?", (dumps(stock),dumps(reserved),wh["id"]))
            db.execute("UPDATE deliveries SET status='dispatched' WHERE plan_id=? AND vehicle_id=? AND status='reserved'", (target["plan_id"],target["vehicle_id"]))
            db.execute("UPDATE vehicles SET status='in_transit' WHERE id=?", (target["vehicle_id"],))
            bump(db); log(db,user["name"],"tour_dispatched",f"Dispatched {target['vehicle_id']} for {len(tour)} stops on plan {target['plan_id']}; stock deducted once.")
        return {"ok":True,"dispatched_stops":len(tour)}

    @app.post("/api/deliveries/{ident}/verify")
    def verify(ident: str, payload: Receipt, user=Depends(authenticated)):
        quantities = {i:getattr(payload,i) for i in ITEMS}
        with transaction(path) as db:
            delivery = require(db,"deliveries",ident)
            if user["role"] != "coordinator" and delivery["assigned_user_id"] != user["id"]:
                raise HTTPException(403,"Only the assigned field worker or a coordinator can verify this delivery.")
            manifest = json.loads(delivery["manifest"])
            if delivery["status"] == "verified":
                if quantities != json.loads(delivery["received"]):
                    raise HTTPException(409,"This receipt is already recorded. Its quantities cannot be changed.")
                return {"ok":True,"already_verified":True}
            if delivery["status"] != "dispatched":
                raise HTTPException(409,"Dispatch this delivery before verifying its receipt.")
            if any(quantities[i]>manifest[i] for i in ITEMS):
                raise HTTPException(422,"Received quantities cannot exceed the dispatched manifest.")
            discrepancy = {i:manifest[i]-quantities[i] for i in ITEMS}
            if any(discrepancy.values()) and not payload.notes.strip():
                raise HTTPException(422,"Add a discrepancy note when received quantities differ from the manifest.")
            loc = require(db,"locations",delivery["location_id"])
            received = json.loads(loc["received"])
            for item in ITEMS:
                received[item] += quantities[item]
            db.execute("UPDATE locations SET received=? WHERE id=?", (dumps(received),loc["id"]))
            db.execute("UPDATE deliveries SET status='verified',received=?,recipient=?,notes=?,verified_at=? WHERE id=?", (dumps(quantities),payload.recipient.strip(),payload.notes,now(),ident))
            remaining = db.execute("SELECT COUNT(*) FROM deliveries WHERE vehicle_id=? AND status IN ('reserved','dispatched')", (delivery["vehicle_id"],)).fetchone()[0]
            if not remaining:
                db.execute("UPDATE vehicles SET status='available' WHERE id=?", (delivery["vehicle_id"],))
            pending = db.execute("SELECT COUNT(*) FROM deliveries WHERE plan_id=? AND status IN ('reserved','dispatched')", (delivery["plan_id"],)).fetchone()[0]
            if not pending:
                db.execute("UPDATE plans SET status='completed' WHERE id=?", (delivery["plan_id"],))
            bump(db); log(db,user["name"],"receipt_verified",f"{loc['name']}: received {quantities['food']} packets, {quantities['water']} L, {quantities['medical']} medical kits; recipient {payload.recipient}. Shortfall: {discrepancy['food']} / {discrepancy['water']} / {discrepancy['medical']}. {payload.notes}".strip())
        return {"ok":True,"discrepancy":discrepancy}

    @app.post("/api/demo/reset")
    def reset(payload: Reset, user=Depends(coordinator)):
        from .seed import seed_demo
        with transaction(path) as db:
            seed_demo(db,reset=True)
            bump(db); log(db,user["name"],"demo_reset","Explicitly reset this demonstration database. User accounts and current sign-in sessions were retained.")
        return {"ok":True}

    @app.get("/api/export/{kind}.csv")
    def export(kind: str, user=Depends(authenticated)):
        with read_transaction(path) as db:
            data = read_state(db)
        if kind == "needs":
            header = ["Location","Population","Severity","Food need (packets)","Water need (L)","Medical need (kits)","Food received","Water received","Medical received"]
            rows = [[l["name"],l["population"],l["severity"],*[l["demands"][i] for i in ITEMS],*[l["received"][i] for i in ITEMS]] for l in data["locations"]]
        elif kind == "deliveries":
            header = ["Delivery","Plan","Vehicle","Warehouse","Location","Status","Food manifest","Water manifest","Medical manifest","Food received","Water received","Medical received","Recipient","Notes"]
            rows = [[d["id"],d["plan_id"],d["vehicle_id"],d["warehouse_id"],d["location_id"],d["status"],*[d["manifest"][i] for i in ITEMS],*[d["received"][i] if d["received"] else "" for i in ITEMS],d["recipient"],d["notes"]] for d in data["deliveries"]]
        elif kind == "activity":
            header = ["Time","User","Action","Detail"]
            rows = [[a["created_at"],a["user_name"],a["action"],a["detail"]] for a in data["activity"]]
        else:
            raise HTTPException(404,"This export does not exist.")
        buffer = io.StringIO(newline=""); writer = csv.writer(buffer)
        # Do not allow user-authored names or notes to become spreadsheet formulas.
        safe = lambda v: "'"+v if isinstance(v,str) and v.lstrip().startswith(("=","+","-","@","\t","\r")) else v
        writer.writerow(header)
        writer.writerows([[safe(v) for v in row] for row in rows])
        return Response(buffer.getvalue(),media_type="text/csv",headers={"Content-Disposition":f'attachment; filename="relieflink-{kind}.csv"'})

    assets = ROOT / "dist" / "assets"
    if assets.is_dir():
        app.mount("/assets",StaticFiles(directory=assets),name="assets")

    @app.get("/{full_path:path}",include_in_schema=False)
    def frontend(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404,"This API endpoint does not exist.")
        dist = ROOT / "dist"
        candidate = (dist / full_path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail":"Frontend is not built. Run npm run build, or use the Vite development server."},status_code=404)

    return app


app = create_app()
