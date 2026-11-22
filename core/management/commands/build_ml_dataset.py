import json
from datetime import date, datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.analytics.production_ml.dataset import build_production_ml_dataset, export_private_dataset
from core.analytics.production_ml.registry import record_lifecycle_event


def _public_readiness(readiness):
    result = dict(readiness)
    result.pop("split_plan", None)
    return result


class Command(BaseCommand):
    help = "Build and assess a private IntelliHRTrack employee-period ML dataset without training."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Explicit analytical cutoff date (YYYY-MM-DD).")
        parser.add_argument("--output", help="Relative path inside the private ML artifact root.")
        parser.add_argument("--no-record", action="store_true", help="Do not write an aggregate lifecycle audit event.")

    def handle(self, *args, **options):
        if options.get("as_of"):
            try:
                cutoff = date.fromisoformat(options["as_of"])
            except ValueError as exc:
                raise CommandError("--as-of must use YYYY-MM-DD.") from exc
            as_of = timezone.make_aware(datetime.combine(cutoff, time.max))
        else:
            as_of = timezone.now()
        bundle = build_production_ml_dataset(as_of_timestamp=as_of)
        output = export_private_dataset(bundle, output_path=options.get("output"))
        readiness = _public_readiness(bundle["readiness"])
        if not options.get("no_record"):
            record_lifecycle_event(
                "DATASET_BUILT" if readiness["ready"] else "READINESS_FAILED",
                succeeded=readiness["ready"],
                message="Production ML dataset assessment completed.",
                metadata={"dataset": bundle["metadata"], "readiness": readiness},
            )
        report = {
            "artifact": str(output),
            "metadata": bundle["metadata"],
            "readiness": readiness,
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
        if readiness["ready"]:
            self.stdout.write(self.style.SUCCESS("READY: production training may be attempted explicitly."))
        else:
            self.stdout.write(self.style.WARNING("NOT READY: no training or activation was performed."))
