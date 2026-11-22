from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.analytics.production_ml.artifacts import save_model_artifact
from core.analytics.production_ml.constants import FEATURE_SCHEMA_VERSION, RANDOM_SEED, TARGET_DEFINITION
from core.analytics.production_ml.dataset import build_production_ml_dataset
from core.analytics.production_ml.registry import record_lifecycle_event
from core.analytics.production_ml.training import ProductionTrainingBlocked, train_and_evaluate_candidates
from core.models import MLModelRegistry


def _public_readiness(readiness):
    result = dict(readiness)
    result.pop("split_plan", None)
    return result


class Command(BaseCommand):
    help = "Train and temporally evaluate four production candidates only when readiness passes."

    def handle(self, *args, **options):
        record_lifecycle_event("TRAINING_ATTEMPT", succeeded=True, message="Production training command started.")
        dataset = build_production_ml_dataset(as_of_timestamp=timezone.now())
        if not dataset["readiness"]["ready"]:
            readiness = _public_readiness(dataset["readiness"])
            record_lifecycle_event(
                "READINESS_FAILED",
                succeeded=False,
                message="Training blocked by production readiness gate.",
                metadata={"dataset": dataset["metadata"], "readiness": readiness},
            )
            codes = ", ".join(readiness["reason_codes"])
            raise CommandError(f"Training blocked. Readiness reasons: {codes}")
        try:
            result = train_and_evaluate_candidates(dataset)
        except ProductionTrainingBlocked as exc:
            record_lifecycle_event("TRAINING_FAILED", succeeded=False, message=str(exc))
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            record_lifecycle_event(
                "TRAINING_FAILED",
                succeeded=False,
                message=f"Training failed: {type(exc).__name__}",
            )
            raise CommandError("Production model training failed; no model was activated.") from exc

        timestamp = timezone.now()
        created = []
        for model_key, candidate in result["candidates"].items():
            version = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{model_key}"
            training_metadata = {
                "dataset": dataset["metadata"],
                "readiness": _public_readiness(dataset["readiness"]),
                "validation": result["validation_metadata"],
                "selection": result["selection"],
                "best_parameters": candidate["best_parameters"],
            }
            saved = save_model_artifact(
                candidate=candidate,
                model_version=version,
                training_metadata=training_metadata,
            )
            is_robust_selection = (
                result["selection"]["status"] == "SELECTED"
                and result["selection"]["selected_model_key"] == model_key
            )
            registry = MLModelRegistry.objects.create(
                model_key=model_key,
                model_name=candidate["model_name"],
                model_version=version,
                status=(
                    MLModelRegistry.STATUS_VALIDATED
                    if is_robust_selection
                    else MLModelRegistry.STATUS_CANDIDATE
                ),
                artifact_relative_path=saved["relative_path"],
                artifact_sha256=saved["sha256"],
                dataset_version=dataset["metadata"]["dataset_version"],
                dataset_generated_at=datetime.fromisoformat(dataset["metadata"]["generated_at"]),
                trained_at=timestamp,
                training_row_count=dataset["metadata"]["eligible_observation_count"],
                employee_group_count=dataset["metadata"]["employee_count"],
                positive_count=dataset["metadata"]["positive_count"],
                negative_count=dataset["metadata"]["negative_count"],
                target_definition=TARGET_DEFINITION,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                random_seed=RANDOM_SEED,
                selected_threshold=candidate["threshold"],
                metrics={
                    "validation": candidate["validation"],
                    "final_evaluation": candidate["final_evaluation"],
                },
                validation_metadata=training_metadata,
                training_code_version=dataset["metadata"]["source_code_version"],
            )
            record_lifecycle_event(
                "TRAINING_SUCCEEDED",
                succeeded=True,
                model=registry,
                message="Candidate trained and validated using temporal evaluation.",
                metadata={"selection": result["selection"]},
            )
            created.append(registry)

        self.stdout.write(self.style.SUCCESS(f"Created {len(created)} evaluated candidate registry entries."))
        self.stdout.write(f"Selection status: {result['selection']['status']}")
        if result["selection"]["status"] == "NO_ROBUST_WINNER":
            self.stdout.write(self.style.WARNING("No model was activated; no robust winner was established."))
        else:
            self.stdout.write("Activation remains a separate explicit superuser-controlled command.")
