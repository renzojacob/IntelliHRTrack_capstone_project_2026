import json
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.analytics.production_ml import anomaly
from core.analytics.production_ml.artifacts import save_private_payload, load_registry_artifact
from core.analytics.production_ml.registry import record_lifecycle_event
from core.models import MLModelRegistry


class Command(BaseCommand):
    help = "Fit a private advisory Isolation Forest after the weekly attendance readiness audit."

    def handle(self, *args, **options):
        rows = anomaly.collect()
        audit, _ = anomaly.assess(rows)
        if not audit["ready"]:
            record_lifecycle_event("ANOMALY_NOT_READY", succeeded=False, metadata=audit)
            raise CommandError(json.dumps(audit))
        pipeline, metadata = anomaly.fit(rows)
        now = timezone.now()
        identity = dict(purpose=anomaly.PURPOSE, model_version=f"attendance-if-{uuid4().hex}",
                        model_name="Isolation Forest", model_key="isolation_forest",
                        feature_schema_version=anomaly.SCHEMA)
        saved = save_private_payload(dict(identity, pipeline=pipeline, feature_names=list(anomaly.INPUTS),
                                          training_metadata=metadata))
        with transaction.atomic():
            entry = MLModelRegistry.objects.create(**identity, status=MLModelRegistry.STATUS_VALIDATED,
                artifact_relative_path=saved["relative_path"], artifact_sha256=saved["sha256"],
                dataset_version=anomaly.SCHEMA, dataset_generated_at=now, trained_at=now,
                training_row_count=audit["training_observations"], employee_group_count=audit["training_employees"],
                target_definition="Unlabeled employee-week", random_seed=anomaly.RANDOM_SEED, validation_metadata=metadata)
            load_registry_artifact(entry)
            record_lifecycle_event("ANOMALY_VALIDATED", succeeded=True, model=entry, metadata=audit)
        self.stdout.write(json.dumps(dict(version=entry.model_version, trained_at=now.isoformat(), **metadata), indent=2))
