"""Operational invariants for the actual CP-SAT planning engine."""

from copy import deepcopy
import json

import pytest

from backend.optimizer import solve_plan


def snapshot():
    return {
        "locations": [{"id": "camp", "name": "Camp", "lat": 13.0, "lng": 77.6,
                       "severity": "high", "demands": {"food": 10, "water": 20, "medical": 2},
                       "received": {}, "committed": {}}],
        "warehouses": [{"id": "depot", "name": "Depot", "lat": 13.01, "lng": 77.61,
                        "stock": {"food": 100, "water": 100, "medical": 100}, "reserved": {}}],
        "vehicles": [{"id": "truck", "name": "Truck", "warehouse_id": "depot",
                      "capacity_kg": 100, "status": "available"}],
        "roads": [{"id": "r1", "from_id": "depot", "to_id": "camp", "minutes": 10,
                   "distance_km": 5, "blocked": False}],
    }


def test_ample_resources_fulfil_every_unit_on_a_real_tour():
    data = snapshot()
    original = deepcopy(data)
    result = solve_plan(data, 2)
    assert result["status"] == "optimal"
    assert result["total_allocated"] == {"food": 10, "water": 20, "medical": 2}
    assert result["total_kg"] == 31
    assert result["weighted_shortage"] == 0
    assert result["road_blocked_locations"] == []
    assert result["travel_minutes"] == 20
    assert result["distance_km"] == 10
    assert result["routes"][0]["path"][0] == result["routes"][0]["path"][-1]
    assert data == original
    json.dumps(result, allow_nan=False)


def test_zero_fleet_means_zero_allocations():
    data = snapshot()
    data["vehicles"] = []
    result = solve_plan(data, 1)
    assert result["routes"] == []
    assert sum(result["total_allocated"].values()) == 0
    assert result["shortages"][0]["remaining"] == data["locations"][0]["demands"]


def test_unreachable_camp_has_no_allocation():
    data = snapshot()
    data["roads"][0]["blocked"] = True
    result = solve_plan(data, 1)
    assert result["routes"] == []
    assert result["total_kg"] == 0
    assert result["road_blocked_locations"] == [{"location_id": "camp", "location_name": "Camp"}]


def test_isolated_critical_camp_warns_without_preventing_other_deliveries():
    data = snapshot()
    data["locations"][0]["severity"] = "critical"
    data["roads"][0]["blocked"] = True
    data["locations"].append({**deepcopy(data["locations"][0]), "id": "reachable", "name": "Reachable Camp",
                              "severity": "low", "lat": 13.02, "lng": 77.62})
    data["roads"].append({"id": "r2", "from_id": "depot", "to_id": "reachable", "minutes": 7, "distance_km": 3})
    original = deepcopy(data)
    result = solve_plan(data, 2)
    assert result["road_blocked_locations"] == [{"location_id": "camp", "location_name": "Camp"}]
    assert {s["location_id"] for r in result["routes"] for s in r["stops"]} == {"reachable"}
    assert next(s for s in result["shortages"] if s["location_id"] == "camp")["remaining"] == data["locations"][0]["demands"]
    assert data == original

    data["roads"][0]["blocked"] = False
    reopened = solve_plan(data, 2)
    assert reopened["road_blocked_locations"] == []
    assert {s["location_id"] for r in reopened["routes"] for s in r["stops"]} == {"camp", "reachable"}


def test_open_connection_from_another_warehouse_prevents_closure_warning():
    data = snapshot()
    data["roads"][0]["blocked"] = True
    data["warehouses"].append({"id": "other", "name": "Other Depot", "lat": 13.02, "lng": 77.62,
                               "stock": {}, "reserved": {}})
    data["roads"].append({"id": "r2", "from_id": "other", "to_id": "camp", "minutes": 5, "distance_km": 2})
    result = solve_plan(data, 1)
    # No truck or stock at this depot is a resource issue, not complete road isolation.
    assert result["routes"] == []
    assert result["road_blocked_locations"] == []


def test_missing_network_connection_is_not_misreported_as_a_closure():
    data = snapshot()
    data["warehouses"].append({"id": "other", "name": "Other Depot", "lat": 13.02, "lng": 77.62,
                               "stock": {}, "reserved": {}})
    data["roads"] = [{"id": "r2", "from_id": "depot", "to_id": "other", "minutes": 5,
                      "distance_km": 2, "blocked": True}]
    assert solve_plan(data, 1)["road_blocked_locations"] == []


@pytest.mark.parametrize("fulfilled_by", ["received", "committed"])
def test_isolated_camp_without_outstanding_need_has_no_warning(fulfilled_by):
    data = snapshot()
    data["roads"][0]["blocked"] = True
    data["locations"][0][fulfilled_by] = dict(data["locations"][0]["demands"])
    assert solve_plan(data, 1)["road_blocked_locations"] == []


@pytest.mark.parametrize("missing_resource", ["vehicles", "stock"])
def test_resource_shortage_does_not_trigger_a_road_closure_warning(missing_resource):
    data = snapshot()
    if missing_resource == "vehicles":
        data["vehicles"] = []
    else:
        data["warehouses"][0]["stock"] = {}
    result = solve_plan(data, 1)
    assert result["routes"] == []
    assert result["road_blocked_locations"] == []
    # A real closure must still be explained when resource shortages also exist.
    data["roads"][0]["blocked"] = True
    assert solve_plan(data, 1)["road_blocked_locations"] == [{"location_id": "camp", "location_name": "Camp"}]


def test_dijkstra_uses_detour_when_direct_road_is_blocked():
    data = snapshot()
    data["warehouses"].append({"id": "junction", "name": "Junction", "lat": 13.1, "lng": 77.7, "stock": {}, "reserved": {}})
    data["roads"] += [
        {"id": "r2", "from_id": "depot", "to_id": "junction", "minutes": 7, "distance_km": 4},
        {"id": "r3", "from_id": "junction", "to_id": "camp", "minutes": 8, "distance_km": 4},
    ]
    direct = solve_plan(data, 2)
    data["roads"][0]["blocked"] = True
    detour = solve_plan(data, 2)
    assert direct["travel_minutes"] == 20
    assert detour["travel_minutes"] == 30
    assert [13.1, 77.7] in detour["routes"][0]["path"]
    assert detour["total_allocated"] == direct["total_allocated"]
    assert detour["road_blocked_locations"] == []


def test_vehicle_payload_and_warehouse_shared_stock_are_conserved():
    data = snapshot()
    data["locations"][0]["demands"] = {"food": 100, "water": 0, "medical": 0}
    data["warehouses"][0]["stock"]["food"] = 12
    data["warehouses"][0]["reserved"] = {"food": 2}
    data["vehicles"][0]["capacity_kg"] = 4
    data["vehicles"].append({**data["vehicles"][0], "id": "truck2", "name": "Truck 2"})
    result = solve_plan(data, 2)
    assert result["total_allocated"]["food"] == 10
    assert len(result["routes"]) == 2
    assert all(route["load_kg"] <= route["capacity_kg"] for route in result["routes"])
    assert sum(route["stops"][0]["manifest"]["food"] for route in result["routes"]) == 10


def test_committed_and_received_supplies_are_not_allocated_twice():
    data = snapshot()
    data["locations"][0]["received"] = {"food": 3, "water": 10, "medical": 1}
    data["locations"][0]["committed"] = {"food": 2, "water": 5, "medical": 0}
    result = solve_plan(data, 2)
    assert result["total_allocated"] == {"food": 5, "water": 5, "medical": 1}


def test_unavailable_vehicles_cannot_supply_cargo():
    data = snapshot()
    data["vehicles"][0]["status"] = "maintenance"
    assert solve_plan(data, 1)["routes"] == []


def test_each_vehicle_only_collects_from_its_home_warehouse():
    data = snapshot()
    data["warehouses"][0]["stock"] = {"food": 0, "water": 0, "medical": 0}
    data["warehouses"].append({"id": "other", "name": "Other", "lat": 13.02, "lng": 77.62,
                               "stock": {"food": 100, "water": 100, "medical": 100}, "reserved": {}})
    data["roads"].append({"id": "r2", "from_id": "other", "to_id": "camp", "minutes": 5, "distance_km": 2})
    result = solve_plan(data, 1)
    assert result["total_kg"] == 0
    assert result["routes"] == []


def test_priority_allocates_scarce_stock_to_critical_camp_first():
    data = snapshot()
    data["locations"][0]["severity"] = "low"
    data["locations"][0]["demands"] = {"food": 10, "water": 0, "medical": 0}
    critical = {**deepcopy(data["locations"][0]), "id": "critical", "name": "Critical", "severity": "critical"}
    data["locations"].append(critical)
    data["warehouses"][0]["stock"]["food"] = 10
    data["roads"].append({"id": "r2", "from_id": "depot", "to_id": "critical", "minutes": 20, "distance_km": 10})
    result = solve_plan(data, 2)
    manifests = {stop["location_id"]: stop["manifest"] for route in result["routes"] for stop in route["stops"]}
    assert manifests == {"critical": {"food": 10, "water": 0, "medical": 0}}


def test_no_roads_never_becomes_a_straight_line_route():
    data = snapshot()
    data["roads"] = []
    result = solve_plan(data, 1)
    assert not result["routes"]
    assert result["total_kg"] == 0
    assert result["road_blocked_locations"] == []


def test_fractional_capacity_smaller_than_one_unit_produces_no_cargo():
    data = snapshot()
    data["vehicles"][0]["capacity_kg"] = .0001
    result = solve_plan(data, 1)
    assert not result["routes"]
    assert result["total_kg"] == 0


def test_direct_call_nonfinite_values_cannot_escape_in_json():
    data = snapshot()
    data["locations"][0]["demands"]["food"] = float("nan")
    data["roads"][0]["minutes"] = float("inf")
    data["vehicles"][0]["capacity_kg"] = float("nan")
    result = solve_plan(data, .1)
    assert not result["routes"]
    assert result["total_kg"] == 0
    json.dumps(result, allow_nan=False)


def test_schema_maximum_quantities_are_integer_safe():
    data = snapshot()
    data["locations"][0]["demands"] = {item: 10_000_000 for item in ("food", "water", "medical")}
    data["warehouses"][0]["stock"] = {item: 10_000_000 for item in ("food", "water", "medical")}
    data["vehicles"][0]["capacity_kg"] = 1_000_000
    result = solve_plan(data, .3)
    assert result["total_kg"] <= 1_000_000
    for route in result["routes"]:
        assert route["load_kg"] <= route["capacity_kg"]
    assert result["weighted_shortage"] >= 0
    json.dumps(result, allow_nan=False)
