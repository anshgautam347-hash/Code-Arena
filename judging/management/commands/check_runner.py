from django.conf import settings
from django.core.management.base import BaseCommand

from judging import runner
from judging.models import Language


class Command(BaseCommand):
    help = "Check the configured Judge0 code runner and compare its languages with the ones configured here."

    def handle(self, *args, **options):
        client = runner.get_client()
        if client is None:
            self.stdout.write(self.style.ERROR(
                "No runner configured. Set JUDGE0_URL (see the README). Until then submissions are "
                "recorded as 'runner unavailable' and nothing is evaluated or scored."))
            return
        self.stdout.write(f"Runner URL: {client.base_url}")
        self.stdout.write("Credentials: " + ("sent with every request" if client.token else "none configured"))
        try:
            about = client.about() or {}
            available = client.languages()
        except runner.RunnerError as exc:
            self.stdout.write(self.style.ERROR(f"NOT REACHABLE: {exc}"))
            return
        self.stdout.write(self.style.SUCCESS(f"Reachable. Judge0 version: {about.get('version', 'unknown')}"))
        names = {item.get("id"): item.get("name") for item in available if isinstance(item, dict)}
        self.stdout.write(f"The runner offers {len(names)} languages.")
        problems = 0
        for language in Language.objects.order_by("name"):
            runner_name = names.get(language.judge0_language_id)
            if runner_name:
                self.stdout.write(f"  OK    {language.name}: id {language.judge0_language_id} = {runner_name}")
            else:
                problems += 1
                self.stdout.write(self.style.WARNING(
                    f"  MISSING {language.name}: id {language.judge0_language_id} is not offered by this runner"))
        if problems:
            self.stdout.write(self.style.WARNING("Fix the IDs in the admin (Languages). Available IDs:"))
            for lang_id, name in sorted(names.items()):
                self.stdout.write(f"    {lang_id}: {name}")
        self.stdout.write("Note: this checks connectivity and language IDs. It does not run any code.")
