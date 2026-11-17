from django.db import migrations


def update_status_label_to_bordero(apps, schema_editor):
    ReconciliationStatus = apps.get_model("web", "ReconciliationStatus")
    ReconciliationStatus.objects.filter(code="RESOLVED_ERP_FIXED").update(
        display_name="Corregido en Borderó"
    )


def revert_status_label_to_erp(apps, schema_editor):
    ReconciliationStatus = apps.get_model("web", "ReconciliationStatus")
    ReconciliationStatus.objects.filter(code="RESOLVED_ERP_FIXED").update(
        display_name="Corregido en ERP"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("web", "0011_reconciliationprocess_erp_coverage"),
    ]

    operations = [
        migrations.RunPython(update_status_label_to_bordero, revert_status_label_to_erp),
    ]
