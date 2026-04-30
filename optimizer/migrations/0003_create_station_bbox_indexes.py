from django.db import migrations

CREATE_STATIONS_LATITUDE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_stations_latitude
ON stations (latitude);
"""

CREATE_STATIONS_LONGITUDE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_stations_longitude
ON stations (longitude);
"""


def create_station_bbox_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_STATIONS_LATITUDE_INDEX_SQL)
        cursor.execute(CREATE_STATIONS_LONGITUDE_INDEX_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("optimizer", "0002_create_optimizer_tables"),
    ]

    operations = [
        migrations.RunPython(create_station_bbox_indexes, migrations.RunPython.noop),
    ]
