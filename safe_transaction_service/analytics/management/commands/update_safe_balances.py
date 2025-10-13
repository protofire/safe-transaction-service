from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db.models import Sum, Max, Min

from safe_transaction_service.analytics.tasks import update_safe_balances_task
from safe_transaction_service.history.models import ProtofireSafeBalance


class Command(BaseCommand):
    help = 'Update Safe account balances from blockchain'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run synchronously instead of as a Celery task',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force update even if data is recent',
        )
        parser.add_argument(
            '--stats',
            action='store_true',
            help='Show current balance statistics',
        )

    def handle(self, *args, **options):
        if options['stats']:
            self.show_stats()
            return

        if options['sync']:
            self.stdout.write('Running balance update synchronously...')
            result = update_safe_balances_task()
            if result > 0:
                self.stdout.write(
                    self.style.SUCCESS(f'Successfully updated {result} Safe balances')
                )
            else:
                self.stdout.write(
                    'Balance update failed or no balances were updated'
                )
        else:
            # Check if recent data exists and force flag is not set
            if not options['force']:
                try:
                    latest_balance = ProtofireSafeBalance.objects.latest('last_updated')
                    days_old = (timezone.now() - latest_balance.last_updated).days
                    
                    if days_old < 7:  # Less than a week old
                        self.stdout.write(
                            f'Balance data is only {days_old} days old. '
                            f'Use --force to update anyway.'
                        )
                        return
                except ProtofireSafeBalance.DoesNotExist:
                    pass  # No existing data, proceed with update
            
            self.stdout.write('Starting balance update task (async)...')
            try:
                task_result = update_safe_balances_task.delay()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Balance update task started. Task ID: {task_result.id}\n'
                        f'This may take several hours to complete.'
                    )
                )
            except Exception as e:
                raise CommandError(f'Failed to start task: {e}') from e

    def show_stats(self):
        """Display current balance statistics"""
        self.stdout.write('Safe Balance Statistics:')
        self.stdout.write('-' * 40)
        
        total_records = ProtofireSafeBalance.objects.count()
        safes_with_balance = ProtofireSafeBalance.objects.filter(balance_wei__gt=0).count()
        
        self.stdout.write(f'Total Safe balance records: {total_records}')
        self.stdout.write(f'Safes with non-zero balance: {safes_with_balance}')
        
        if total_records > 0:
            stats = ProtofireSafeBalance.objects.aggregate(
                total_balance=Sum('balance_wei'),
                latest_update=Max('last_updated'),
                earliest_update=Min('last_updated')
            )
            
            total_balance = stats['total_balance'] or 0
            latest_update = stats['latest_update']
            earliest_update = stats['earliest_update']
            
            self.stdout.write(f'Total balance across all Safes: {total_balance} wei')
            
            if latest_update:
                days_since_update = (timezone.now() - latest_update).days
                self.stdout.write(f'Latest update: {latest_update} ({days_since_update} days ago)')
            
            if earliest_update:
                self.stdout.write(f'Earliest update: {earliest_update}')
        else:
            self.stdout.write('No balance data found. Run update command first.')