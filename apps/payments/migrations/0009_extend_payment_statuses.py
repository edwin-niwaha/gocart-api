from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0008_alter_payment_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="status",
            field=models.CharField(
                choices=[
                    ("UNPAID", "Unpaid"),
                    ("PENDING", "Pending"),
                    ("PROCESSING", "Processing"),
                    ("PAID", "Paid"),
                    ("FAILED", "Failed"),
                    ("REFUNDED", "Refunded"),
                    ("PARTIALLY_REFUNDED", "Partially refunded"),
                    ("CANCELLED", "Cancelled"),
                ],
                db_index=True,
                default="PENDING",
                max_length=20,
            ),
        ),
    ]
