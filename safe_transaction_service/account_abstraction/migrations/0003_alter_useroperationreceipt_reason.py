# Generated by Django 5.0.4 on 2024-05-09 11:34

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account_abstraction", "0002_alter_useroperation_unique_together"),
    ]

    operations = [
        migrations.AlterField(
            model_name="useroperationreceipt",
            name="reason",
            field=models.CharField(blank=True, max_length=256),
        ),
    ]
