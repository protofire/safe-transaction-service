# Generated by Django 5.0.9 on 2024-10-18 13:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("history", "0088_erc20transfer_history_erc__from_d88198_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="safecontractdelegate",
            name="expiry_date",
            field=models.DateTimeField(db_index=True, null=True),
        ),
    ]
