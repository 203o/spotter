from django.contrib import admin

from .models import FuelPrice, Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state")
    search_fields = ("opis_id", "name", "city", "state")


@admin.register(FuelPrice)
class FuelPriceAdmin(admin.ModelAdmin):
    list_display = ("id", "station", "rack_id", "retail_price")
    search_fields = ("station__name", "station__opis_id", "rack_id")
