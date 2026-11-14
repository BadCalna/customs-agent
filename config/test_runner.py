from django.core.management.base import CommandError
from django.test.runner import DiscoverRunner


class TaskRunner(DiscoverRunner):
    def build_suite(self, *args, **kwargs):
        suite = super().build_suite(*args, **kwargs)
        if not suite.countTestCases():
            raise CommandError("No tests were discovered for this task.")
        return suite
