"""Transactional production-model registry operations."""

from django.db import transaction
from django.utils import timezone

from core.models import MLLifecycleEvent, MLModelRegistry

from .artifacts import load_registry_artifact
from .constants import MODEL_PURPOSE


class MLActivationError(RuntimeError):
    pass


def record_lifecycle_event(event_type, *, succeeded, model=None, message="", metadata=None, actor=None, actor_label="management-command"):
    return MLLifecycleEvent.objects.create(
        event_type=event_type,
        succeeded=bool(succeeded),
        model=model,
        actor=actor,
        actor_label=actor_label,
        message=message,
        metadata=metadata or {},
    )


def activate_validated_model(identifier, *, actor=None, actor_label="management-command"):
    models = MLModelRegistry.objects.filter(purpose=MODEL_PURPOSE)
    try:
        if str(identifier).isdigit():
            selected = models.get(pk=int(identifier))
        else:
            selected = models.get(model_version=str(identifier))
    except MLModelRegistry.DoesNotExist as exc:
        record_lifecycle_event(
            "ACTIVATION_FAILED",
            succeeded=False,
            message="Requested model registry entry does not exist.",
            actor=actor,
            actor_label=actor_label,
        )
        raise MLActivationError("Model registry entry was not found.") from exc

    if selected.status != MLModelRegistry.STATUS_VALIDATED:
        record_lifecycle_event(
            "ACTIVATION_FAILED",
            succeeded=False,
            model=selected,
            message="Only VALIDATED models can be activated.",
            actor=actor,
            actor_label=actor_label,
        )
        raise MLActivationError("Only a VALIDATED model can be activated.")
    readiness = selected.validation_metadata.get("readiness", {})
    selection = selected.validation_metadata.get("selection", {})
    if readiness.get("ready") is not True or selection.get("selected_model_key") != selected.model_key:
        record_lifecycle_event(
            "ACTIVATION_FAILED",
            succeeded=False,
            model=selected,
            message="Registry entry lacks a passing readiness gate and robust selection.",
            actor=actor,
            actor_label=actor_label,
        )
        raise MLActivationError("Activation requires passing readiness evidence and a robust selected winner.")
    try:
        load_registry_artifact(selected)
    except Exception as exc:
        record_lifecycle_event(
            "ACTIVATION_FAILED",
            succeeded=False,
            model=selected,
            message=str(exc),
            actor=actor,
            actor_label=actor_label,
        )
        raise MLActivationError(str(exc)) from exc

    with transaction.atomic():
        locked = MLModelRegistry.objects.select_for_update().filter(purpose=MODEL_PURPOSE)
        selected = locked.get(pk=selected.pk)
        if selected.status != MLModelRegistry.STATUS_VALIDATED:
            raise MLActivationError("Model status changed before activation.")
        load_registry_artifact(selected)
        now = timezone.now()
        previous = list(locked.filter(status=MLModelRegistry.STATUS_ACTIVE).exclude(pk=selected.pk))
        for model in previous:
            model.status = MLModelRegistry.STATUS_RETIRED
            model.save(update_fields=["status", "updated_at"])
            record_lifecycle_event(
                "MODEL_RETIRED",
                succeeded=True,
                model=model,
                message=f"Retired during activation of {selected.model_version}.",
                actor=actor,
                actor_label=actor_label,
            )
        selected.status = MLModelRegistry.STATUS_ACTIVE
        selected.activated_at = now
        selected.activated_by = actor
        selected.activation_provenance = actor_label
        selected.save(
            update_fields=[
                "status",
                "activated_at",
                "activated_by",
                "activation_provenance",
                "updated_at",
            ]
        )
        record_lifecycle_event(
            "MODEL_ACTIVATED",
            succeeded=True,
            model=selected,
            message="Validated production advisory model activated.",
            actor=actor,
            actor_label=actor_label,
        )
    return selected
