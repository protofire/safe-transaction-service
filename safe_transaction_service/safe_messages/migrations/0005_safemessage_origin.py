# Generated by Django 5.0.9 on 2024-10-24 10:47

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "safe_messages",
            "0004_alter_safemessage_proposed_by_alter_safemessage_safe_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="safemessage",
            name="origin",
            field=models.JSONField(default=dict),
        ),
    ]
