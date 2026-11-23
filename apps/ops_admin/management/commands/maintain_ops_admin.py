from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ops_admin.lifecycle.services import maintain_lifecycle
from apps.ops_admin.models import MutationReceipt


class Command(BaseCommand):
    help = 'Expire ops tickets, prune rate buckets and clear expired private export snapshots.'

    def handle(self, *args, **options):
        for _ in range(40):
            result = maintain_lifecycle()
            if result['expired_tickets'] < 250:
                break
        now = timezone.now()
        exports = MutationReceipt.objects.filter(created_at__lt=now - timedelta(minutes=5), response__csv__isnull=False)
        # Keep idempotent response metadata after discarding the CSV, without rewriting audit data.
        cleared = 0
        for receipt in exports.only('id', 'request_id').iterator(chunk_size=250):
            cleared += exports.filter(pk=receipt.pk).update(response={'status': 'expired', 'request_id': receipt.request_id})
        old, _ = MutationReceipt.objects.filter(created_at__lt=now - timedelta(days=7)).delete()
        self.stdout.write(f'export_snapshots_cleared={cleared} receipts_pruned={old}')
