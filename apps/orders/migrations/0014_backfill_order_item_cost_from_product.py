from decimal import Decimal

from django.db import migrations


ZERO = Decimal("0.00")


def backfill_cost_price_snapshots(apps, schema_editor):
    OrderItem = apps.get_model("orders", "OrderItem")

    items = (
        OrderItem.objects.filter(cost_price_snapshot=ZERO)
        .select_related("variant", "product")
        .iterator()
    )
    for item in items:
        unit_cost = getattr(item.variant, "unit_cost", ZERO) or ZERO
        if unit_cost <= ZERO:
            unit_cost = getattr(item.product, "cost_price", ZERO) or ZERO
        if unit_cost > ZERO:
            item.cost_price_snapshot = unit_cost
            item.save(update_fields=["cost_price_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0013_orderitem_cost_price_snapshot"),
        ("products", "0008_product_cost_and_selling_price"),
    ]

    operations = [
        migrations.RunPython(backfill_cost_price_snapshots, migrations.RunPython.noop),
    ]
