from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0011_order_delivery_snapshots"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("AWAITING_PAYMENT", "Awaiting payment"),
                    ("CONFIRMED", "Confirmed"),
                    ("PROCESSING", "Processing"),
                    ("PAID", "Paid"),
                    ("SHIPPED", "Shipped"),
                    ("DELIVERED", "Delivered"),
                    ("CANCELLED", "Cancelled"),
                    ("REFUNDED", "Refunded"),
                ],
                db_index=True,
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="orderstatusevent",
            name="from_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING", "Pending"),
                    ("AWAITING_PAYMENT", "Awaiting payment"),
                    ("CONFIRMED", "Confirmed"),
                    ("PROCESSING", "Processing"),
                    ("PAID", "Paid"),
                    ("SHIPPED", "Shipped"),
                    ("DELIVERED", "Delivered"),
                    ("CANCELLED", "Cancelled"),
                    ("REFUNDED", "Refunded"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="orderstatusevent",
            name="to_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("AWAITING_PAYMENT", "Awaiting payment"),
                    ("CONFIRMED", "Confirmed"),
                    ("PROCESSING", "Processing"),
                    ("PAID", "Paid"),
                    ("SHIPPED", "Shipped"),
                    ("DELIVERED", "Delivered"),
                    ("CANCELLED", "Cancelled"),
                    ("REFUNDED", "Refunded"),
                ],
                max_length=20,
            ),
        ),
    ]
