from django.core.management.base import BaseCommand
from django.utils import timezone

from exams.models import ExamSession
from exams.services import sync_session


class Command(BaseCommand):
    help = (
        "Close exam sessions whose time has run out. The server also does this whenever a "
        "participant or the exam page contacts it, so this is only a tidy-up for abandoned sessions."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        closed = 0
        sessions = ExamSession.objects.filter(status__in=ExamSession.ACTIVE_STATUSES).select_related("competition")
        for session in sessions:
            sync_session(session, now)
            if session.is_finished:
                closed += 1
        self.stdout.write(self.style.SUCCESS(f"Closed {closed} expired session(s)."))
