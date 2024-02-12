# Generated by Django 3.2.5 on 2021-07-30 12:55

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("history", "0041_auto_20210729_0916"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="safestatus",
            index=models.Index(
                fields=["address", "-nonce", "-internal_tx"],
                name="history_saf_address_1c362b_idx",
            ),
        ),
    ]
