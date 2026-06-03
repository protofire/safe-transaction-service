from django.db import migrations

from safe_transaction_service.tokens.constants import SRC20_KEYLESS_PLACEHOLDER_LOGS


def backfill_src20_placeholder_logs(apps, schema_editor):
    """
    Seed `src20_keyless_placeholder_logs_per_transfer` for SRC20 tokens already in the DB.

    New registrations get the value via `create_src20_from_blockchain`, but tokens indexed
    before this field existed keep the migration default (1 = keep-all). Without this
    backfill, already-known standard-base tokens (e.g. TST) would keep showing duplicate
    "bulk" transfers until re-registered.
    """
    Token = apps.get_model("tokens", "Token")
    for address, placeholder_logs in SRC20_KEYLESS_PLACEHOLDER_LOGS.items():
        Token.objects.filter(address=address).update(
            src20_keyless_placeholder_logs_per_transfer=placeholder_logs
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0017_token_src20_keyless_placeholder_logs_per_transfer"),
    ]

    operations = [
        migrations.RunPython(
            backfill_src20_placeholder_logs,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
