import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("care_ccn_be", "0001_initial"),
        ("emr", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferralLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("beckn_ref", models.CharField(blank=True, max_length=255)),
                ("active", models.BooleanField(default=True)),
                ("created_date", models.DateTimeField(auto_now_add=True)),
                ("role", models.CharField(choices=[("origin", "Origin"), ("assigned", "Assigned")], max_length=10)),
                (
                    "resource_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="+", to="emr.resourcerequest"
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
                "indexes": [models.Index(fields=["role", "active"], name="care_ccn_be_role_008362_idx")],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("active", True)),
                        fields=("transaction", "role"),
                        name="beckn_referral_link_unique",
                    )
                ],
            },
        ),
    ]
