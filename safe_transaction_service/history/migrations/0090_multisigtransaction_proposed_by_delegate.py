# Generated by Django 5.0.9 on 2024-10-24 15:38

from django.db import migrations

import safe_eth.eth.django.models


class Migration(migrations.Migration):

    dependencies = [
        ("history", "0089_safecontractdelegate_expiry_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="multisigtransaction",
            name="proposed_by_delegate",
            field=safe_eth.eth.django.models.EthereumAddressBinaryField(
                blank=True, null=True
            ),
        ),
    ]
