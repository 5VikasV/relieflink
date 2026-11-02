# Shared implementation contract

Local React/TypeScript frontend, FastAPI JSON backend, SQLite. API prefix `/api`.

## Authentication

`POST /api/auth/login {email,password}` → `{user:{id,name,email,role},csrf_token}` and HttpOnly session cookie. `GET /api/auth/me` same JSON. `POST /api/auth/logout`.
Roles `coordinator` and `field_worker`. Demo logins `coordinator@relieflink.local` and `field@relieflink.local`, password `ReliefLinkDemo!`. Mutating requests except login send `X-CSRF-Token` from login/me. Same-origin requests; no role switch impersonation. Logout returns `{ok:true}`.

## Snapshot

`GET /api/state` returns:
```
{
 user, version:number, demo:true,
 locations:[{id,name,lat,lng,population,injured,severity,contact,notes,
   demands:{food,water,medical},received:{food,water,medical},committed:{food,water,medical}}],
 warehouses:[{id,name,lat,lng,stock:{food,water,medical},reserved:{food,water,medical}}],
 vehicles:[{id,name,warehouse_id,capacity_kg,status}],
 roads:[{id,from_id,to_id,minutes,distance_km,blocked}],
 plans:[{id,status,created_at,input_version,result}],
 deliveries:[{id,plan_id,vehicle_id,warehouse_id,location_id,assigned_user_id,status,
   manifest:{food,water,medical},received:{food,water,medical}|null,recipient,notes,
   created_at,verified_at}],
 activity:[{id,created_at,user_name,action,detail}],
 settings:{food_per_person,water_per_person,medical_per_injured},
 summary:{population,locations,available_kg,unmet_units,active_deliveries,completed_deliveries}
}
```
Food = packets (0.8kg); water = litres (1kg); medical = kits (1.5kg). All whole units. Severity enum `low,moderate,high,critical`. Outstanding demand is max(0,demands-received-committed), per item. Roads join location and warehouse node IDs. Every vehicle starts/ends at its own warehouse.

## Writes

All return updated entity or `{ok:true}`. Frontend refetches `/state` on success. Errors `{detail:string}` with 4xx/5xx.

* `POST /api/locations` `{name,lat,lng,population,injured,severity,contact?,notes?,demands:{food,water,medical}}` (coordinator).
* `PATCH /api/locations/{id}` same editable fields, optional; coordinator.
* `POST /api/requests` `{location_id,food,water,medical,notes?}` (both roles). Sets the location's current total need, no duplicate demand summing. Reject targets below received + committed.
* `POST /api/warehouses` `{name,lat,lng,stock:{food,water,medical}}` coordinator.
* `POST /api/warehouses/{id}/restock` `{food,water,medical}` adds stock, coordinator.
* `POST /api/vehicles` `{name,warehouse_id,capacity_kg}` coordinator.
* `PATCH /api/vehicles/{id}` `{status:"available"|"maintenance"}` coordinator, only unreserved vehicles.
* `PATCH /api/roads/{id}` `{blocked:boolean}` coordinator.
* `POST /api/plans/generate` `{}` coordinator → saved plan.
* `POST /api/plans/{id}/approve` `{}` coordinator. Recheck input version; reserve stock/vehicles; create one delivery per stop assigned to field worker. Plan status approved; transactional.
* `POST /api/plans/{id}/cancel` `{}` coordinator. Release remaining un-dispatched tour reservations while preserving dispatched and verified history. Draft cancellation is also supported.
* `POST /api/deliveries/{id}/dispatch` `{}` coordinator. Dispatch all stops on same vehicle+plan together to represent one tour; decrement stock and reservations once; statuses dispatched.
* `POST /api/deliveries/{id}/verify` `{food,water,medical,recipient,notes?}` assigned field worker or coordinator. Actual quantities <= manifest; zero allowed with discrepancy notes; idempotent receipt; update received totals. Vehicle becomes available only when its last stop is verified.
* `PATCH /api/settings` `{food_per_person,water_per_person,medical_per_injured}` coordinator.
* `POST /api/demo/reset` `{confirm:true}` coordinator; explicit reset affects demo database only.
* `GET /api/export/{kind}.csv`, kind `needs`, `deliveries`, `activity`; authenticated.

## Optimizer interface (backend/optimizer.py)

`solve_plan(snapshot:dict, time_limit:float=8.0) -> dict` receives state shape above. No DB access. Return:
```
{
 status:"optimal"|"feasible"|"empty"|"infeasible",
 routes:[{vehicle_id,vehicle_name,warehouse_id,warehouse_name,load_kg,capacity_kg,
   travel_minutes,distance_km,path:[[lat,lng],...],
   stops:[{location_id,location_name,sequence,manifest:{food,water,medical},load_kg}]}],
 shortages:[{location_id,location_name,priority,remaining:{food,water,medical}}],
 total_allocated:{food,water,medical},total_kg,travel_minutes,distance_km,
 weighted_shortage,explanation:string,assumptions:[string]
}
```
CP-SAT joint allocation + optional per-vehicle AddCircuit tours; stock shared across warehouse vehicles; integer grams; reachable unblocked road arcs only; return to home warehouse. Priority weights critical5/high3/moderate2/low1, item weights medical3/water2/food1. Lexicographic unmet-demand then travel minutes; bounded time, accurately label optimal/feasible. Exclude unavailable vehicles. No independent frontend allocation. All outputs finite JSON-safe types. New manually added nodes need deterministic connections to nearest 2 nodes (modelled local links, labelled assumptions).
