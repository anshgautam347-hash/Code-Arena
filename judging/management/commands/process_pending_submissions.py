from django.core.management.base import BaseCommand

from judging import services
from judging.models import Submission


class Command(BaseCommand):
    help = (
        "Collect results for submissions that are still queued or running (for example when the "
        "participant closed the browser). Safe to run at any time."
    )

    def handle(self, *args, **options):
        examined = services.reconcile(limit=500)
        left = Submission.objects.filter(status__in=Submission.PENDING_STATUSES).count()
        self.stdout.write(self.style.SUCCESS(f"Examined {examined} submission(s); {left} still pending."))
