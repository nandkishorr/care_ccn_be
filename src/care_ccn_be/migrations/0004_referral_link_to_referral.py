import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Referral links point at Care's ``Referral`` instead of the legacy ``ResourceRequest``.

    The old links cannot be converted in place (a Referral needs an availability and a patient), so the table is
    rebuilt. ``beckn_backfill`` recreates the links of legacy rows.
    """

    dependencies = [
        ("care_ccn_be", "0003_appointment_link"),
        ("emr", "0100_merge_20261025_0238"),
    ]

    operations = [
        migrations.DeleteModel(name="ReferralLink"),
        migrations.CreateModel(
            name="ReferralLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("beckn_ref", models.CharField(blank=True, max_length=255)),
                ("active", models.BooleanField(default=True)),
                ("created_date", models.DateTimeField(auto_now_add=True)),
                ("role", models.CharField(choices=[("origin", "Origin"), ("assigned", "Assigned")], max_length=10)),
                ("details", models.JSONField(blank=True, default=dict)),
                (
                    "legacy_resource_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="emr.resourcerequest",
                    ),
                ),
                (
                    "referral",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="emr.referral"),
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
