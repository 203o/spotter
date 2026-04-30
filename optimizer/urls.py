from django.urls import path

from .views import HealthCheckView, OptimizerInterfaceView, OptimizeFuelView

urlpatterns = [
    path("", OptimizerInterfaceView.as_view(), name="optimizer_interface"),
    path("api/health/", HealthCheckView.as_view(), name="health_check"),
    path("api/route/optimize-fuel/", OptimizeFuelView.as_view(), name="optimize_fuel"),
]
