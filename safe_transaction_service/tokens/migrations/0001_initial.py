# Generated by Django 3.0.7 on 2020-06-15 15:22

from django.db import migrations, models

import safe_eth.eth.django.models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Token",
            fields=[
                (
                    "address",
                    safe_eth.eth.django.models.EthereumAddressField(
                        primary_key=True, serialize=False
                    ),
                ),
                ("name", models.CharField(max_length=60)),
                ("symbol", models.CharField(max_length=60)),
                ("decimals", models.PositiveSmallIntegerField(db_index=True)),
                ("logo_uri", models.CharField(blank=True, default="", max_length=300)),
                ("trusted", models.BooleanField(default=False)),
            ],
        ),
    ]
