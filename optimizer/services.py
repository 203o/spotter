import hashlib
import json
import math

import requests
from django.core.cache import cache
from django.db.models import Min, OuterRef, Subquery

from .models import FuelPrice, Station

MPG = 10.0
MAX_RANGE = 500.0
TANK_CAPACITY = 50.0
MIN_REFUEL_GALLONS = 1.0
ROUTE_CACHE_TIMEOUT = 3600
ROUTE_RESULT_CACHE_TIMEOUT = 1800
GEOCODE_CACHE_TIMEOUT = 86400
ROUTE_REQUEST_TIMEOUT = 20
ROUTE_HASH_POINT_LIMIT = 50
MAX_ALTERNATE_ROUTES = 2
STATION_CORRIDOR_THRESHOLD_MILES = 10.0
STATION_MATCH_TOLERANCE_MILES = STATION_CORRIDOR_THRESHOLD_MILES
ROUTE_MATCH_INDEX_ATTR = "_route_match_index"
ROUTE_MATCH_DISTANCE_ATTR = "_route_match_distance"
TRIP_ASSUMPTIONS = {
    "starts_with_full_tank": True,
    "initial_fuel_cost_included": False,
    "cost_definition": "additional fuel purchased during the trip only",
    "vehicle_mpg": MPG,
    "max_range_miles": MAX_RANGE,
}
ORS_DIRECTIONS_PROFILE = "driving-car"
ORS_GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
ORS_DIRECTIONS_URL_TEMPLATE = (
    "https://api.openrouteservice.org/v2/directions/{profile}/geojson"
)
UNITED_STATES_BOUNDS = (
    {
        "min_lat": 24.396308,
        "max_lat": 49.384358,
        "min_lng": -124.848974,
        "max_lng": -66.885444,
    },
    {
        "min_lat": 51.214183,
        "max_lat": 71.365162,
        "min_lng": -179.148909,
        "max_lng": -129.979500,
    },
    {
        "min_lat": 18.910361,
        "max_lat": 22.235600,
        "min_lng": -160.247100,
        "max_lng": -154.806773,
    },
    {
        "min_lat": 17.800000,
        "max_lat": 18.600000,
        "min_lng": -67.400000,
        "max_lng": -65.100000,
    },
)


def build_cache_key(prefix, payload):
    digest = hashlib.md5(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"


def haversine(lat1, lon1, lat2, lon2):
    radius_miles = 3958.8
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    start_lat = math.radians(lat1)
    end_lat = math.radians(lat2)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(start_lat) * math.cos(end_lat) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_miles * c


def is_within_united_states(latitude, longitude):
    for bounds in UNITED_STATES_BOUNDS:
        if (
            bounds["min_lat"] <= latitude <= bounds["max_lat"]
            and bounds["min_lng"] <= longitude <= bounds["max_lng"]
        ):
            return True
    return False


def serialize_point(latitude, longitude):
    return {
        "lat": round(latitude, 6),
        "lng": round(longitude, 6),
    }


def serialize_station_location(station):
    serialized = {
        "opis_id": getattr(station, "opis_id", None),
        "name": station.name,
        "address": getattr(station, "address", None),
        "city": getattr(station, "city", None),
        "state": getattr(station, "state", None),
        "location": serialize_point(station.latitude, station.longitude),
    }
    rack_id = getattr(station, "selected_rack_id", None)
    if rack_id is not None:
        serialized["rack_id"] = rack_id
    return serialized


def build_route_map(route_data, stops):
    points = []
    for point in route_data["points"]:
        if isinstance(point, dict):
            points.append(point)
        else:
            points.append(serialize_point(point[0], point[1]))

    stop_markers = []
    for stop in stops:
        marker = {
            "type": "fuel_stop",
            "name": stop["station"]["name"],
            "location": stop["station"]["location"],
            "price": stop["price"],
        }
        if stop.get("rack_id") is not None:
            marker["rack_id"] = stop["rack_id"]
        stop_markers.append(marker)

    return {
        "provider": "openrouteservice",
        "attribution": "Routing by openrouteservice; map data by OpenStreetMap contributors",
        "polyline": route_data["polyline"],
        "points": points,
        "bounds": route_data["bounds"],
        "start": route_data["start"],
        "end": route_data["end"],
        "markers": [
            {
                "type": "start",
                "name": route_data["start"]["resolved_address"],
                "location": route_data["start"]["location"],
            },
            *stop_markers,
            {
                "type": "end",
                "name": route_data["end"]["resolved_address"],
                "location": route_data["end"]["location"],
            },
        ],
    }


def build_trip_assumptions():
    return dict(TRIP_ASSUMPTIONS)


def build_route_failure(code, message, gap_start_miles, gap_end_miles, total_distance):
    return {
        "error": message,
        "failure_code": code,
        "fuel_gap": {
            "start_miles": round(gap_start_miles, 2),
            "end_miles": round(gap_end_miles, 2),
            "length_miles": round(max(gap_end_miles - gap_start_miles, 0.0), 2),
            "max_range_miles": MAX_RANGE,
        },
        "route_distance": round(total_distance, 2),
        "assumptions": build_trip_assumptions(),
    }


def get_stop_type(gallons, current_position, total_distance):
    if gallons < MIN_REFUEL_GALLONS:
        if total_distance - current_position <= MAX_RANGE:
            return "final_top_off"
        return "micro_refuel"
    return "regular_refuel"


def add_refuel_counts(result):
    stops = result.get("stops", [])
    result["total_refuels"] = len(stops)
    result["regular_refuels"] = sum(
        1 for stop in stops if stop.get("stop_type") == "regular_refuel"
    )
    result["micro_refuels"] = sum(
        1 for stop in stops if stop.get("stop_type") == "micro_refuel"
    )
    result["final_top_offs"] = sum(
        1 for stop in stops if stop.get("stop_type") == "final_top_off"
    )
    return result


def get_nearest_route_point(latitude, longitude, route_points):
    closest_index = None
    closest_distance = float("inf")

    for index, point in enumerate(route_points):
        distance_from_route = haversine(latitude, longitude, point[0], point[1])
        if distance_from_route < closest_distance:
            closest_distance = distance_from_route
            closest_index = index

    return closest_index, closest_distance


def is_station_near_route(
    station,
    route_points,
    threshold_miles=STATION_CORRIDOR_THRESHOLD_MILES,
):
    closest_index, closest_distance = get_nearest_route_point(
        station.latitude,
        station.longitude,
        route_points,
    )
    if closest_index is None or closest_distance > threshold_miles:
        return False

    setattr(station, ROUTE_MATCH_INDEX_ATTR, closest_index)
    setattr(station, ROUTE_MATCH_DISTANCE_ATTR, closest_distance)
    return True


def get_station_route_match(station, route_points):
    cached_index = getattr(station, ROUTE_MATCH_INDEX_ATTR, None)
    cached_distance = getattr(station, ROUTE_MATCH_DISTANCE_ATTR, None)
    if cached_index is not None and cached_distance is not None:
        return cached_index, cached_distance

    closest_index, closest_distance = get_nearest_route_point(
        station.latitude,
        station.longitude,
        route_points,
    )
    setattr(station, ROUTE_MATCH_INDEX_ATTR, closest_index)
    setattr(station, ROUTE_MATCH_DISTANCE_ATTR, closest_distance)
    return closest_index, closest_distance


class RouteService:
    @staticmethod
    def normalize_location(location_input, api_key):
        if isinstance(location_input, dict):
            location = location_input.get("location") or location_input
            latitude = location.get("lat")
            longitude = location.get("lng")
            if latitude is None:
                latitude = location.get("latitude")
            if longitude is None:
                longitude = location.get("longitude")

            if latitude is None or longitude is None:
                raise ValueError("Coordinate locations must include lat and lng.")

            latitude = float(latitude)
            longitude = float(longitude)
            if not is_within_united_states(latitude, longitude):
                raise ValueError("Start and finish must both be within the USA.")

            label = (
                location_input.get("label")
                or location_input.get("resolved_address")
                or location_input.get("input")
                or f"{latitude}, {longitude}"
            )
            return {
                "input": label,
                "resolved_address": label,
                "location": serialize_point(latitude, longitude),
                "country_code": "USA",
                "source": "coordinates",
            }

        return {
            **RouteService.geocode_location(location_input, api_key),
            "source": "geocode",
        }

    @staticmethod
    def geocode_location(location_text, api_key):
        if not api_key or api_key == "YOUR_OPENROUTESERVICE_API_KEY_HERE":
            raise ValueError("OPENROUTESERVICE_API_KEY is not configured.")

        cache_key = build_cache_key(
            "geocode",
            {"location": location_text},
        )
        cached = cache.get(cache_key)
        if cached:
            return cached

        response = requests.get(
            ORS_GEOCODE_URL,
            params={
                "api_key": api_key,
                "text": location_text,
                "size": 1,
            },
            timeout=ROUTE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        features = data.get("features") or []
        if not features:
            raise ValueError(f"Unable to geocode location: {location_text}")

        feature = features[0]
        coordinates = feature["geometry"]["coordinates"]
        properties = feature["properties"]
        result = {
            "input": location_text,
            "resolved_address": properties.get("label", location_text),
            "location": serialize_point(coordinates[1], coordinates[0]),
            "country_code": properties.get("country_a"),
        }
        cache.set(cache_key, result, timeout=GEOCODE_CACHE_TIMEOUT)
        return result

    @staticmethod
    def _build_route_result(route, data, start_geocode, end_geocode, route_index=0):
        geometry = route["geometry"]
        coordinates = geometry["coordinates"]
        route_points = [(point[1], point[0]) for point in coordinates]
        summary = route["properties"]["summary"]
        start_location = route_points[0]
        end_location = route_points[-1]

        if not is_within_united_states(
            start_location[0],
            start_location[1],
        ) or not is_within_united_states(
            end_location[0],
            end_location[1],
        ):
            raise ValueError("Start and finish must both resolve to locations within the USA.")

        bbox = route.get("bbox") or data.get("bbox")
        bounds = {}
        if bbox and len(bbox) >= 4:
            bounds = {
                "southwest": serialize_point(bbox[1], bbox[0]),
                "northeast": serialize_point(bbox[3], bbox[2]),
            }

        return {
            "route_index": route_index,
            "route_label": "primary" if route_index == 0 else f"alternate_{route_index}",
            "points": route_points,
            "polyline": geometry,
            "total_distance_miles": summary["distance"] * 0.000621371,
            "bounds": bounds,
            "start": {
                **start_geocode,
                "location": serialize_point(start_location[0], start_location[1]),
            },
            "end": {
                **end_geocode,
                "location": serialize_point(end_location[0], end_location[1]),
            },
        }

    @staticmethod
    def _request_routes(start_loc, end_loc, api_key, include_alternates=False):
        if not api_key or api_key == "YOUR_OPENROUTESERVICE_API_KEY_HERE":
            raise ValueError("OPENROUTESERVICE_API_KEY is not configured.")

        start_geocode = RouteService.normalize_location(start_loc, api_key)
        end_geocode = RouteService.normalize_location(end_loc, api_key)

        cache_key = build_cache_key(
            "routes:v1",
            {
                "start": start_geocode["location"],
                "end": end_geocode["location"],
                "include_alternates": include_alternates,
            },
        )
        cached = cache.get(cache_key)
        if cached:
            return cached

        payload = {
            "coordinates": [
                [
                    start_geocode["location"]["lng"],
                    start_geocode["location"]["lat"],
                ],
                [
                    end_geocode["location"]["lng"],
                    end_geocode["location"]["lat"],
                ],
            ]
        }
        if include_alternates:
            payload["alternative_routes"] = {
                "target_count": MAX_ALTERNATE_ROUTES + 1,
                "weight_factor": 2,
                "share_factor": 0.8,
            }

        response = requests.post(
            ORS_DIRECTIONS_URL_TEMPLATE.format(profile=ORS_DIRECTIONS_PROFILE),
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=ROUTE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        features = data.get("features") or []
        if not features:
            raise ValueError("openrouteservice returned no route.")

        routes = [
            RouteService._build_route_result(
                route,
                data,
                start_geocode,
                end_geocode,
                route_index=index,
            )
            for index, route in enumerate(features)
        ]
        cache.set(cache_key, routes, timeout=ROUTE_CACHE_TIMEOUT)
        return routes

    @staticmethod
    def get_route(start_loc, end_loc, api_key):
        return RouteService._request_routes(start_loc, end_loc, api_key)[0]

    @staticmethod
    def get_alternate_routes(start_loc, end_loc, api_key):
        routes = RouteService._request_routes(
            start_loc,
            end_loc,
            api_key,
            include_alternates=True,
        )
        return routes[1 : MAX_ALTERNATE_ROUTES + 1]


class StationService:
    @staticmethod
    def get_stations_in_corridor(
        route_points,
        buffer_deg=0.2,
        threshold_miles=STATION_CORRIDOR_THRESHOLD_MILES,
    ):
        if not route_points:
            return []

        latitudes = [point[0] for point in route_points]
        longitudes = [point[1] for point in route_points]

        min_lat = min(latitudes) - buffer_deg
        max_lat = max(latitudes) + buffer_deg
        min_lon = min(longitudes) - buffer_deg
        max_lon = max(longitudes) + buffer_deg

        cheapest_price_rack = (
            FuelPrice.objects.filter(station=OuterRef("pk"))
            .order_by("retail_price", "id")
            .values("rack_id")[:1]
        )
        stations = list(
            Station.objects.filter(
                latitude__gte=min_lat,
                latitude__lte=max_lat,
                longitude__gte=min_lon,
                longitude__lte=max_lon,
            )
            .annotate(price=Min("fuel_prices__retail_price"))
            .annotate(selected_rack_id=Subquery(cheapest_price_rack))
            .exclude(price__isnull=True)
        )
        corridor_stations = [
            station
            for station in stations
            if is_station_near_route(
                station,
                route_points,
                threshold_miles=threshold_miles,
            )
        ]
        return corridor_stations


class OptimizerService:
    @staticmethod
    def process_stations(stations, route_points, total_distance):
        if not route_points:
            return []

        cumulative_distances = [0.0]
        for index in range(1, len(route_points)):
            previous = route_points[index - 1]
            current = route_points[index]
            cumulative_distances.append(
                cumulative_distances[-1]
                + haversine(previous[0], previous[1], current[0], current[1])
            )

        projected_by_station = {}

        for station in stations:
            closest_index, closest_distance = get_station_route_match(
                station,
                route_points,
            )

            if closest_index is None or closest_distance > STATION_MATCH_TOLERANCE_MILES:
                continue

            projected = {
                "station": station,
                "price": float(station.price),
                "dist": min(cumulative_distances[closest_index], total_distance),
                "fuel_from_start_gallons": round(
                    min(cumulative_distances[closest_index], total_distance) / MPG,
                    2,
                ),
                "offset_miles": round(closest_distance, 3),
            }
            existing = projected_by_station.get(station.pk)
            if existing is None or projected["dist"] < existing["dist"]:
                projected_by_station[station.pk] = projected

        return sorted(projected_by_station.values(), key=lambda item: item["dist"])

    @staticmethod
    def run_optimization(projected_stations, total_distance):
        if total_distance <= MAX_RANGE:
            return add_refuel_counts({
                "route_distance": round(total_distance, 2),
                "estimated_gallons_used": round(total_distance / MPG, 2),
                "total_cost": 0.0,
                "total_additional_fuel_cost": 0.0,
                "stops": [],
                "assumptions": build_trip_assumptions(),
            })

        stations = sorted(projected_stations, key=lambda item: item["dist"])
        current_position = 0.0
        current_fuel = TANK_CAPACITY
        current_price = None
        current_station = None
        last_stop_position = 0.0
        total_cost = 0.0
        stops = []

        if current_position + (current_fuel * MPG) < total_distance:
            initial_reachable = [
                station
                for station in stations
                if current_position < station["dist"] <= current_position + (current_fuel * MPG)
            ]
            if not initial_reachable:
                first_station = next(
                    (station for station in stations if station["dist"] > current_position),
                    None,
                )
                gap_end = (
                    min(first_station["dist"], total_distance)
                    if first_station is not None
                    else total_distance
                )
                return build_route_failure(
                    "start_gap",
                    "Route not possible: no station reachable from the start.",
                    current_position,
                    gap_end,
                    total_distance,
                )

            next_stop = min(initial_reachable, key=lambda item: (item["price"], -item["dist"]))
            fuel_needed = (next_stop["dist"] - current_position) / MPG
            current_fuel -= fuel_needed
            current_position = next_stop["dist"]
            current_price = next_stop["price"]
            current_station = next_stop["station"]

        while current_position + (current_fuel * MPG) < total_distance:
            reachable = [
                station
                for station in stations
                if current_position < station["dist"] <= current_position + MAX_RANGE
            ]
            if not reachable:
                next_station = next(
                    (station for station in stations if station["dist"] > current_position),
                    None,
                )
                gap_end = (
                    min(next_station["dist"], total_distance)
                    if next_station is not None
                    else total_distance
                )
                failure_code = "end_gap" if next_station is None else "middle_gap"
                message = (
                    "Route not possible: destination is more than 500 miles from the last reachable station."
                    if failure_code == "end_gap"
                    else "Route not possible: gap between fuel stops exceeds 500 miles."
                )
                return build_route_failure(
                    failure_code,
                    message,
                    current_position,
                    gap_end,
                    total_distance,
                )

            cheaper_stations = [
                station for station in reachable if station["price"] < current_price
            ]

            if cheaper_stations:
                next_stop = min(cheaper_stations, key=lambda item: item["dist"])
                desired_fuel = (next_stop["dist"] - current_position) / MPG
            else:
                next_stop = min(reachable, key=lambda item: (item["price"], -item["dist"]))
                desired_fuel = TANK_CAPACITY

            fuel_to_buy = max(desired_fuel - current_fuel, 0.0)
            if fuel_to_buy > 0:
                stop_cost = round(fuel_to_buy * current_price, 2)
                total_cost += stop_cost
                current_fuel += fuel_to_buy
                distance_since_last_stop = current_position - last_stop_position
                station_location = serialize_station_location(current_station)
                stops.append(
                    {
                        "station": station_location,
                        "rack_id": station_location.get("rack_id"),
                        "price": round(current_price, 3),
                        "stop_type": get_stop_type(
                            fuel_to_buy,
                            current_position,
                            total_distance,
                        ),
                        "distance_from_start_miles": round(current_position, 2),
                        "distance_since_last_stop_miles": round(distance_since_last_stop, 2),
                        "fuel_used_since_last_stop_gallons": round(
                            distance_since_last_stop / MPG,
                            2,
                        ),
                        "gallons": round(fuel_to_buy, 2),
                        "cost": stop_cost,
                    }
                )
                last_stop_position = current_position

            fuel_needed = (next_stop["dist"] - current_position) / MPG
            current_fuel -= fuel_needed
            current_position = next_stop["dist"]
            current_price = next_stop["price"]
            current_station = next_stop["station"]

        return add_refuel_counts({
            "route_distance": round(total_distance, 2),
            "estimated_gallons_used": round(total_distance / MPG, 2),
            "total_cost": round(total_cost, 2),
            "total_additional_fuel_cost": round(total_cost, 2),
            "stops": stops,
            "assumptions": build_trip_assumptions(),
        })
