from requests import RequestException
from django.conf import settings
from django.core.cache import cache
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import (
    OptimizerService,
    ROUTE_RESULT_CACHE_TIMEOUT,
    RouteService,
    StationService,
    build_route_map,
    build_cache_key,
)


class OptimizerInterfaceView(TemplateView):
    template_name = "optimizer/interface.html"


class HealthCheckView(APIView):
    def get(self, request):
        return Response(
            {
                "status": "ok",
                "service": "fuel-optimizer-api",
            },
            status=status.HTTP_200_OK,
        )


class OptimizeFuelView(APIView):
    def evaluate_route(self, route_data):
        route_points = route_data["points"]
        total_distance = route_data["total_distance_miles"]
        stations = StationService.get_stations_in_corridor(route_points)
        projected = OptimizerService.process_stations(
            stations,
            route_points,
            total_distance,
        )
        result = OptimizerService.run_optimization(projected, total_distance)
        result["route_index"] = route_data["route_index"]
        result["route_label"] = route_data["route_label"]

        if "error" in result:
            return {
                "valid": False,
                "route_data": route_data,
                "failure": {
                    "route_index": route_data["route_index"],
                    "route_label": route_data["route_label"],
                    "reason": result["error"],
                    "failure_code": result.get("failure_code"),
                    "fuel_gap": result.get("fuel_gap"),
                    "route_distance": result.get("route_distance"),
                },
            }

        result["start"] = route_data["start"]["resolved_address"]
        result["end"] = route_data["end"]["resolved_address"]
        result["route_map"] = build_route_map(route_data, result["stops"])
        return {
            "valid": True,
            "route_data": route_data,
            "result": result,
        }

    def post(self, request):
        start = request.data.get("start")
        end = request.data.get("end")

        if not start or not end:
            return Response(
                {"error": "Missing start or end location."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result_cache_key = build_cache_key(
            "result:v6",
            {"start": start, "end": end},
        )
        cached_result = cache.get(result_cache_key)
        if cached_result:
            return Response(cached_result, status=status.HTTP_200_OK)

        try:
            route_data = RouteService.get_route(
                start,
                end,
                settings.OPENROUTESERVICE_API_KEY,
            )
            primary_evaluation = self.evaluate_route(route_data)
            failures = []

            if primary_evaluation["valid"]:
                result = primary_evaluation["result"]
                result["route_strategy"] = "primary_selected"
                result["routes_checked"] = 1
                result["failures"] = []
                cache.set(result_cache_key, result, timeout=ROUTE_RESULT_CACHE_TIMEOUT)
                return Response(result, status=status.HTTP_200_OK)

            failures.append(primary_evaluation["failure"])
            alternate_error = None
            alternate_evaluations = []
            try:
                alternate_routes = RouteService.get_alternate_routes(
                    start,
                    end,
                    settings.OPENROUTESERVICE_API_KEY,
                )
            except RequestException as exc:
                alternate_routes = []
                alternate_error = str(exc)

            for alternate_route in alternate_routes:
                evaluation = self.evaluate_route(alternate_route)
                if evaluation["valid"]:
                    alternate_evaluations.append(evaluation)
                else:
                    failures.append(evaluation["failure"])

            if alternate_evaluations:
                selected = min(
                    alternate_evaluations,
                    key=lambda evaluation: evaluation["result"]["total_cost"],
                )
                result = selected["result"]
                result["route_strategy"] = "alternate_selected"
                result["routes_checked"] = 1 + len(alternate_routes)
                result["primary_route_error"] = primary_evaluation["failure"]
                result["failures"] = failures
                cache.set(result_cache_key, result, timeout=ROUTE_RESULT_CACHE_TIMEOUT)
                return Response(result, status=status.HTTP_200_OK)

            error_result = {
                "error": "No feasible fuel plan found.",
                "routes_checked": 1 + len(alternate_routes),
                "failures": failures,
            }
            if alternate_error:
                error_result["alternate_route_error"] = alternate_error
            return Response(error_result, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RequestException as exc:
            return Response(
                {"error": f"Unable to reach openrouteservice API: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
