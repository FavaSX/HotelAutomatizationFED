from django.db import migrations


# Reasons the accountant can pick when closing an exception from the "Pendientes" inbox.
# Every code is RESOLVED_*, so anything that isn't PENDING drops out of the dashboard counters.
RESOLUTION_STATUSES = [
    ("RESOLVED_ERP_FIXED", "Corregido en ERP"),
    ("RESOLVED_ADJUSTMENT", "Ajuste contable registrado"),
    ("RESOLVED_REVERSAL", "Anulación / contracargo"),
    ("RESOLVED_CASHIER", "Informado a recepción (error de cajero)"),
    ("RESOLVED_ACCEPTED", "Diferencia aceptada"),
]


def seed_resolution_statuses(apps, schema_editor):
    ReconciliationStatus = apps.get_model("web", "ReconciliationStatus")
    for code, display_name in RESOLUTION_STATUSES:
        ReconciliationStatus.objects.get_or_create(code=code, defaults={"display_name": display_name})


class Migration(migrations.Migration):

    dependencies = [
        ('web', '0004_tolerance_settings'),
    ]

    operations = [
        migrations.RunPython(seed_resolution_statuses, migrations.RunPython.noop),
    ]
