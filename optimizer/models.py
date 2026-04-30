from django.db import models


class Station(models.Model):
    opis_id = models.IntegerField(primary_key=True, db_column="OPIS Truckstop ID")
    name = models.TextField(db_column="Truckstop Name")
    address = models.TextField(db_column="Address")
    city = models.TextField(db_column="City")
    state = models.TextField(db_column="State")
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        db_table = "stations"
        managed = False
        verbose_name = "Station"
        verbose_name_plural = "Stations"

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"


class FuelPrice(models.Model):
    id = models.AutoField(primary_key=True)
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        db_column="OPIS Truckstop ID",
        related_name="fuel_prices",
    )
    rack_id = models.IntegerField(db_column="Rack ID")
    retail_price = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        db_column="Retail Price",
    )

    class Meta:
        db_table = "fuel_prices"
        managed = False
        verbose_name = "Fuel Price"
        verbose_name_plural = "Fuel Prices"

    def __str__(self):
        return f"${self.retail_price}"
