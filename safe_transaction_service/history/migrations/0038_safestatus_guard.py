# Generated by Django 3.1.8 on 2021-04-28 12:44

from django.db import migrations

import safe_eth.eth.django.models


class Migration(migrations.Migration):
    dependencies = [
        ("history", "0037_fix_failed_module_transactions"),
    ]

    operations = [
        migrations.AddField(
            model_name="safestatus",
            name="guard",
            field=safe_eth.eth.django.models.EthereumAddressField(
                default=None, null=True
            ),
        ),
    ]
