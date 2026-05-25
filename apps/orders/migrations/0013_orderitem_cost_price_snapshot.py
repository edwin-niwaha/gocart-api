from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def backfill_cost_price_snapshots(apps, schema_editor):
    OrderItem = apps.get_model("orders", "OrderItem")
    for item in OrderItem.objects.select_related("variant").iterator():
        if item.cost_price_snapshot:
            continue
        item.cost_price_snapshot = getattr(item.variant, "unit_cost", Decimal("0.00")) or Decimal("0.00")
        item.save(update_fields=["cost_price_snapshot"])


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0012_add_confirmed_order_status"),
        ("products", "0007_productvariant_unit_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="cost_price_snapshot",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.RunPython(backfill_cost_price_snapshots, migrations.RunPython.noop),
    ]
