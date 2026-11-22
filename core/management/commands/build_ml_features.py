import json

from django.core.management.base import BaseCommand

from core.analytics.production_ml import anomaly


class Command(BaseCommand):
    help = "Audit completed attendance anomaly employee-weeks, independently of supervised employee-periods."

    def add_arguments(self, parser):
        parser.add_argument("--exclusions", action="store_true", help="Include excluded employee-window keys for local audit.")

    def handle(self, *args, **options):
        rows = anomaly.collect()
        audit, _ = anomaly.assess(rows)
        if options["exclusions"]:
            audit["excluded_windows"] = [row for row in rows if "features" not in row]
        self.stdout.write(json.dumps(dict(feature_type=anomaly.PURPOSE, **audit), indent=2, default=str))
