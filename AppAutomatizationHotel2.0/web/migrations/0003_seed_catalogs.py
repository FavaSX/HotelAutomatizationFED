# Seeds the base catalogs (CardType, ReconciliationStatus, SystemConfiguration)
# required before any USD or CLP reconciliation can be committed to the database.

from django.db import migrations
from decimal import Decimal


CARD_TYPES = [
    {"code": "VI", "name": "Visa", "bin_prefixes": "4"},
    {"code": "MC", "name": "MasterCard", "bin_prefixes": "51-55,22-27"},
    {"code": "AX", "name": "American Express", "bin_prefixes": "34,37"},
    {"code": "DI", "name": "Diners Club", "bin_prefixes": "30,36,38"},
    {"code": "RCO", "name": "Redcompra", "bin_prefixes": ""},
]

RECONCILIATION_STATUSES = [
    {"code": "PENDING", "display_name": "Pendiente de Revisión"},
    {"code": "ACCEPTED_ROUNDING", "display_name": "Aceptado (Cuadratura Automática)"},
    {"code": "RESOLVED", "display_name": "Resuelto por Contador"},
]


def seed_catalogs(apps, schema_editor):
    CardType = apps.get_model("web", "CardType")
    ReconciliationStatus = apps.get_model("web", "ReconciliationStatus")
    SystemConfiguration = apps.get_model("web", "SystemConfiguration")

    for card in CARD_TYPES:
        CardType.objects.get_or_create(code=card["code"], defaults=card)

    for status in RECONCILIATION_STATUSES:
        ReconciliationStatus.objects.get_or_create(code=status["code"], defaults=status)

    if not SystemConfiguration.objects.exists():
        SystemConfiguration.objects.create(
            usd_rounding_tolerance=Decimal("0.50"),
            clp_rounding_tolerance=Decimal("1000.00"),
        )


def unseed_catalogs(apps, schema_editor):
    CardType = apps.get_model("web", "CardType")
    ReconciliationStatus = apps.get_model("web", "ReconciliationStatus")
    CardType.objects.filter(code__in=[c["code"] for c in CARD_TYPES]).delete()
    ReconciliationStatus.objects.filter(code__in=[s["code"] for s in RECONCILIATION_STATUSES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('web', '0002_cardoperatortransaction_sequence_number'),
    ]

    operations = [
        migrations.RunPython(seed_catalogs, unseed_catalogs),
    ]
