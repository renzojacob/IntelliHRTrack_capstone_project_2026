import json

from django.core.management.base import BaseCommand, CommandError

from core.analytics.production_ml.artifacts import MLArtifactError, load_registry_artifact
from core.analytics.production_ml.registry import record_lifecycle_event
from core.models import MLModelRegistry
from core.analytics.production_ml.constants import MODEL_PURPOSE


class Command(BaseCommand):
    purposes = (MODEL_PURPOSE,)
    help = "Verify stored validated model artifacts and display their measured production metrics."

    def add_arguments(self, parser):
        parser.add_argument("identifier", nargs="?", help="Registry ID or model version; defaults to all VALIDATED candidates.")

    def handle(self, *args, **options):
        identifier = options.get("identifier")
        queryset = MLModelRegistry.objects.filter(
            purpose__in=self.purposes,
            status__in=[
                MLModelRegistry.STATUS_CANDIDATE,
                MLModelRegistry.STATUS_VALIDATED,
                MLModelRegistry.STATUS_ACTIVE,
            ]
        )
        if identifier:
            queryset = queryset.filter(pk=int(identifier)) if str(identifier).isdigit() else queryset.filter(model_version=identifier)
        models = list(queryset.order_by("model_version"))
        if not models:
            raise CommandError("No matching evaluated production models were found.")
        output = []
        for registry in models:
            try:
                load_registry_artifact(registry)
            except MLArtifactError as exc:
                record_lifecycle_event("EVALUATION_FAILED", succeeded=False, model=registry, message=str(exc))
                raise CommandError(f"{registry.model_version}: {exc}") from exc
            output.append(
                {
                    "id": registry.pk,
                    "version": registry.model_version,
                    "purpose": registry.purpose,
                    "status": registry.status,
                    "metrics": registry.metrics,
                    "validation_metadata": registry.validation_metadata,
                }
            )
            record_lifecycle_event(
                "EVALUATION_VERIFIED",
                succeeded=True,
                model=registry,
                message="Stored metrics and private artifact verified.",
            )
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True, default=str))
