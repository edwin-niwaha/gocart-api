from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


def backfill_product_prices(apps, schema_editor):
    Product = apps.get_model("products", "Product")

    for product in Product.objects.all().iterator():
        first_variant = (
            product.variants.filter(is_active=True)
            .order_by("sort_order", "price", "id")
            .first()
        )
        if first_variant is None:
            continue
        product.selling_price = first_variant.price or Decimal("0.00")
        product.cost_price = getattr(first_variant, "unit_cost", Decimal("0.00")) or Decimal("0.00")
        product.save(update_fields=["selling_price", "cost_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0007_productvariant_unit_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="cost_price",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                validators=[MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="selling_price",
            field=models.DecimalField(
                db_index=True,
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                validators=[MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.RunPython(backfill_product_prices, migrations.RunPython.noop),
    ]
