"""Joint stock allocation and route planning for the local ReliefLink demo.

This module is deliberately independent of the database. Every proposed item is
assigned to an actual vehicle tour from the warehouse holding that item.
"""

from __future__ import annotations

import heapq
import math
import time
from collections import defaultdict
from typing import Any

from ortools.sat.python import cp_model


ITEMS = ("food", "water", "medical")
WEIGHT_GRAMS = {"food": 800, "water": 1000, "medical": 1500}
ITEM_IMPORTANCE = {"food": 1, "water": 2, "medical": 3}
PRIORITY = {"low": 1, "moderate": 2, "high": 3, "critical": 5}
ASSUMPTIONS = [
    "Sample roads and travel times model a fictional response network; this is not live road navigation.",
    "Each available vehicle makes one tour from its home warehouse and returns there; no intermediate restocking.",
    "Food packets weigh 0.8 kg, water litres 1 kg and medical kits 1.5 kg; quantities are whole units.",
    "Priority weights: low 1, moderate 2, high 3, critical 5. Item weights: food 1, water 2, medical 3. These are demo planning assumptions.",
]


def _units(value: Any) -> int:
    """Input is validated by the API; remain safe for direct/local callers."""
    try:
        number = float(value or 0)
        return max(0, int(number)) if math.isfinite(number) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _network(snapshot: dict) -> tuple[dict, dict, dict]:
    nodes = {node["id"]: node for node in snapshot.get("warehouses", []) + snapshot.get("locations", [])}
    adjacency: dict[Any, dict[Any, tuple[int, float]]] = {node: {} for node in nodes}
    for road in snapshot.get("roads", []):
        if road.get("blocked"):
            continue
        a, b = road.get("from_id"), road.get("to_id")
        if a not in nodes or b not in nodes or a == b:
            continue
        minutes = _number(road.get("minutes"), -1)
        distance = _number(road.get("distance_km"), -1)
        if minutes < 0 or distance < 0:
            continue
        cost = (max(1, round(minutes * 1000)), distance)
        for start, end in ((a, b), (b, a)):
            if end not in adjacency[start] or cost < adjacency[start][end]:
                adjacency[start][end] = cost

    # Lexicographic Dijkstra: earliest arrival, then shorter distance. A serial
    # tie-breaker avoids comparing heterogeneous node IDs in heap entries.
    paths, costs = {}, {}
    serial = 0
    for source in nodes:
        best = {source: (0, 0.0)}
        previous = {}
        queue = [(0, 0.0, serial, source)]
        while queue:
            minutes, distance, _, node = heapq.heappop(queue)
            if best.get(node) != (minutes, distance):
                continue
            for target, (edge_minutes, edge_distance) in adjacency[node].items():
                candidate = (minutes + edge_minutes, round(distance + edge_distance, 9))
                if target not in best or candidate < best[target]:
                    best[target] = candidate
                    previous[target] = node
                    serial += 1
                    heapq.heappush(queue, (*candidate, serial, target))
        for target, cost in best.items():
            chain = [target]
            while chain[-1] != source:
                chain.append(previous[chain[-1]])
            chain.reverse()
            paths[source, target], costs[source, target] = chain, cost
    return nodes, paths, costs


def solve_plan(snapshot: dict, time_limit: float = 8.0) -> dict:
    """Plan dispatchable quantities and complete vehicle tours in one CP model.

    Objective is lexicographic: minimise weighted unmet units, then route time.
    A feasible result is never presented as a proof of global optimality.
    """
    started = time.monotonic()
    budget = max(0.1, min(_number(time_limit, 8.0), 60.0))
    locations = list(snapshot.get("locations", []))
    warehouses = {w["id"]: w for w in snapshot.get("warehouses", [])}
    need = {
        (camp["id"], item): max(
            0,
            _units(camp.get("demands", {}).get(item))
            - _units((camp.get("received") or {}).get(item))
            - _units((camp.get("committed") or {}).get(item)),
        )
        for camp in locations for item in ITEMS
    }
    priority = {camp["id"]: PRIORITY.get(str(camp.get("severity", "moderate")).lower(), 2) for camp in locations}
    available = {
        (warehouse_id, item): max(
            0, _units(w.get("stock", {}).get(item)) - _units((w.get("reserved") or {}).get(item))
        )
        for warehouse_id, w in warehouses.items() for item in ITEMS
    }
    road_blocked_locations = []
    network = None
    if any(need.values()) and warehouses and any(road.get("blocked") for road in snapshot.get("roads", [])):
        network = _network(snapshot)
        _, open_paths, _ = network
        isolated = [camp for camp in locations
                    if any(need[camp["id"], item] > 0 for item in ITEMS)
                    and not any((home, camp["id"]) in open_paths for home in warehouses)]
        if isolated:
            # Only attribute isolation to closures when reopening roads restores
            # a warehouse connection. Stock and vehicle availability are separate.
            _, reopened_paths, _ = _network({
                **snapshot,
                "roads": [{**road, "blocked": False} for road in snapshot.get("roads", [])],
            })
            road_blocked_locations = [
                {"location_id": camp["id"], "location_name": camp["name"]}
                for camp in isolated
                if any((home, camp["id"]) in reopened_paths for home in warehouses)
            ]

    def result(routes: list, status: str, explanation: str) -> dict:
        delivered: dict[tuple, int] = defaultdict(int)
        total = dict.fromkeys(ITEMS, 0)
        for route in routes:
            for stop in route["stops"]:
                for item in ITEMS:
                    amount = stop["manifest"][item]
                    delivered[stop["location_id"], item] += amount
                    total[item] += amount
        shortages = [{
            "location_id": camp["id"], "location_name": camp["name"],
            "priority": priority[camp["id"]],
            "remaining": {item: max(0, need[camp["id"], item] - delivered[camp["id"], item]) for item in ITEMS},
        } for camp in sorted(locations, key=lambda c: (-priority[c["id"]], str(c["id"])))]
        weighted_shortage = sum(row["priority"] * ITEM_IMPORTANCE[item] * row["remaining"][item]
                                for row in shortages for item in ITEMS)
        return {
            "status": status, "routes": routes, "shortages": shortages,
            "road_blocked_locations": road_blocked_locations,
            "total_allocated": total,
            "total_kg": round(sum(total[item] * WEIGHT_GRAMS[item] for item in ITEMS) / 1000, 3),
            "travel_minutes": round(sum(route["travel_minutes"] for route in routes), 3),
            "distance_km": round(sum(route["distance_km"] for route in routes), 3),
            "weighted_shortage": weighted_shortage,
            "explanation": explanation, "assumptions": ASSUMPTIONS.copy(),
        }

    if not any(need.values()):
        return result([], "empty", "No outstanding demand remains after verified receipts and existing commitments.")
    vehicles = [v for v in snapshot.get("vehicles", [])
                if v.get("status", "available") == "available"
                and v.get("warehouse_id") in warehouses and _number(v.get("capacity_kg")) > 0]
    if not vehicles or not any(available.values()):
        return result([], "empty", "No dispatchable allocation: available vehicles and unreserved warehouse stock are both required.")

    nodes, paths, costs = network if network is not None else _network(snapshot)
    model = cp_model.CpModel()
    quantities: dict[tuple, Any] = {}
    tours = []
    all_variables = []
    travel_terms = []
    for vi, vehicle in enumerate(vehicles):
        home = vehicle["warehouse_id"]
        capacity = int(_number(vehicle["capacity_kg"]) * 1000)
        candidates = [camp for camp in locations if any(need[camp["id"], item] > 0 for item in ITEMS)
                      and (home, camp["id"]) in paths and (camp["id"], home) in paths]
        if not candidates:
            continue
        route_nodes = [home] + [camp["id"] for camp in candidates]
        active = model.NewBoolVar(f"active_{vi}")
        visits = {}
        circuit = [(0, 0, active.Not())]
        arcs = {}
        all_variables.append(active)
        for ci, camp in enumerate(candidates, 1):
            camp_id = camp["id"]
            visit = model.NewBoolVar(f"visit_{vi}_{ci}")
            visits[ci] = visit
            all_variables.append(visit)
            circuit.append((ci, ci, visit.Not()))
            model.Add(visit <= active)
            cargo = []
            for item in ITEMS:
                upper = min(need[camp_id, item], available[home, item], capacity // WEIGHT_GRAMS[item])
                qty = model.NewIntVar(0, upper, f"qty_{vi}_{ci}_{item}")
                quantities[vi, camp_id, item] = qty
                all_variables.append(qty)
                model.Add(qty <= upper * visit)
                cargo.append(qty)
            model.Add(sum(cargo) >= visit)
        model.Add(sum(visits.values()) >= active)
        for a, node_a in enumerate(route_nodes):
            for b, node_b in enumerate(route_nodes):
                if a == b or (node_a, node_b) not in costs:
                    continue
                arc = model.NewBoolVar(f"arc_{vi}_{a}_{b}")
                all_variables.append(arc)
                arcs[a, b] = arc
                circuit.append((a, b, arc))
                travel_terms.append(costs[node_a, node_b][0] * arc)
        model.AddCircuit(circuit)
        model.Add(sum(qty * WEIGHT_GRAMS[item] for (index, _, item), qty in quantities.items() if index == vi) <= capacity)
        tours.append((vi, vehicle, route_nodes, arcs))

    if not tours:
        return result([], "empty", "No outstanding location is reachable from an available vehicle's home warehouse on the unblocked road graph.")

    for warehouse_id in warehouses:
        for item in ITEMS:
            stock_vars = [qty for (vi, _, kind), qty in quantities.items()
                          if vehicles[vi]["warehouse_id"] == warehouse_id and kind == item]
            if stock_vars:
                model.Add(sum(stock_vars) <= available[warehouse_id, item])
    for camp in locations:
        for item in ITEMS:
            allocation_vars = [qty for (_, camp_id, kind), qty in quantities.items() if camp_id == camp["id"] and kind == item]
            if allocation_vars:
                model.Add(sum(allocation_vars) <= need[camp["id"], item])

    initial_shortage = sum(priority[camp_id] * ITEM_IMPORTANCE[item] * amount for (camp_id, item), amount in need.items())
    primary = initial_shortage - sum(priority[camp_id] * ITEM_IMPORTANCE[item] * qty
                                    for (_, camp_id, item), qty in quantities.items())
    model.Minimize(primary)
    first = cp_model.CpSolver()
    first.parameters.max_time_in_seconds = max(.01, (budget - (time.monotonic() - started)) * .65)
    first.parameters.num_search_workers = 1
    first.parameters.random_seed = 0
    first_status = first.Solve(model)
    if first_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return result([], "feasible", "The solver did not establish a dispatch plan within its time budget. The safe empty plan leaves all demand outstanding; optimality is not established.")

    chosen = first
    primary_optimal = first_status == cp_model.OPTIMAL
    secondary_optimal = False
    remaining_time = budget - (time.monotonic() - started)
    if remaining_time > .02:
        model.Add(primary == first.Value(primary))
        model.Minimize(sum(travel_terms))
        for variable in all_variables:
            model.AddHint(variable, first.Value(variable))
        second = cp_model.CpSolver()
        second.parameters.max_time_in_seconds = remaining_time
        second.parameters.num_search_workers = 1
        second.parameters.random_seed = 0
        second_status = second.Solve(model)
        if second_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            chosen = second
            secondary_optimal = second_status == cp_model.OPTIMAL

    routes = []
    for vi, vehicle, route_nodes, arcs in tours:
        successor = {a: b for (a, b), variable in arcs.items() if chosen.Value(variable)}
        if 0 not in successor:
            continue
        order = [0]
        cursor = successor[0]
        while cursor != 0:
            order.append(cursor)
            cursor = successor[cursor]
        order.append(0)
        stops, geometry = [], []
        route_minutes, route_distance, load_grams = 0, 0.0, 0
        for a, b in zip(order, order[1:]):
            node_a, node_b = route_nodes[a], route_nodes[b]
            minutes, distance = costs[node_a, node_b]
            route_minutes += minutes
            route_distance += distance
            for node_id in paths[node_a, node_b]:
                coordinate = [_number(nodes[node_id].get("lat")), _number(nodes[node_id].get("lng"))]
                if not geometry or geometry[-1] != coordinate:
                    geometry.append(coordinate)
            if b == 0:
                continue
            manifest = {item: int(chosen.Value(quantities[vi, node_b, item])) for item in ITEMS}
            grams = sum(manifest[item] * WEIGHT_GRAMS[item] for item in ITEMS)
            load_grams += grams
            stops.append({"location_id": node_b, "location_name": nodes[node_b]["name"],
                          "sequence": len(stops) + 1, "manifest": manifest, "load_kg": grams / 1000})
        home = warehouses[vehicle["warehouse_id"]]
        routes.append({
            "vehicle_id": vehicle["id"], "vehicle_name": vehicle["name"],
            "warehouse_id": home["id"], "warehouse_name": home["name"],
            "load_kg": load_grams / 1000, "capacity_kg": _number(vehicle["capacity_kg"]),
            "travel_minutes": round(route_minutes / 1000, 3), "distance_km": round(route_distance, 3),
            "path": geometry, "stops": stops,
        })
    status = "optimal" if primary_optimal and secondary_optimal else "feasible"
    explanation = (
        "Joint allocation and routing minimise weighted unmet demand first, then travel time. Both objectives are proven optimal for this snapshot."
        if status == "optimal" else
        "A feasible joint allocation and route plan was found within the time budget; global optimality for both objectives is not established."
    )
    return result(routes, status, explanation)
