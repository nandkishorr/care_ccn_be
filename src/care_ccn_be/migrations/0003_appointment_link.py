import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("care_ccn_be", "0002_referral_link"),
        ("emr", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppointmentLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("beckn_ref", models.CharField(blank=True, max_length=255)),
                ("active", models.BooleanField(default=True)),
                ("created_date", models.DateTimeField(auto_now_add=True)),
                (
                    "token_booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="+", to="emr.tokenbooking"
                    ),
                ),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(class)s_set",
                        to="care_ccn_be.beckntransaction",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("active", True)),
                        fields=("transaction",),
                        name="appointment_link_one_active",
                    )
                ],
            },
        ),
    ]
