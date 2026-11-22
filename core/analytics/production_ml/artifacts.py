"""Private trusted artifact persistence with checksum and schema validation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import joblib

from .constants import (
    EXPECTED_FEATURES,
    FEATURE_SCHEMA_VERSION,
    MODEL_PURPOSE,
    RISK_BANDS,
    TARGET_DEFINITION,
    private_ml_root,
)


class MLArtifactError(RuntimeError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_private_root(path):
    root = private_ml_root().resolve()
    resolved = Path(path).resolve()
    if resolved != root and root not in resolved.parents:
        raise MLArtifactError("ML artifact path is outside the private artifact root.")
    return root, resolved


def save_model_artifact(*, candidate, model_version, training_metadata):
    root = private_ml_root().resolve()
    target = (root / "models" / model_version / "pipeline.joblib").resolve()
    _inside_private_root(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "purpose": MODEL_PURPOSE,
        "model_key": candidate["model_key"],
        "model_name": candidate["model_name"],
        "model_version": model_version,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(EXPECTED_FEATURES),
        "target_definition": TARGET_DEFINITION,
        "threshold": float(candidate["threshold"]),
        "risk_bands": dict(RISK_BANDS),
        "pipeline": candidate["pipeline"],
        "explanation": candidate["explanation"],
        "training_metadata": training_metadata,
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix="ml-artifact-", suffix=".joblib", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        joblib.dump(payload, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "relative_path": target.relative_to(root).as_posix(),
        "sha256": sha256_file(target),
        "payload": payload,
    }


def validate_artifact_payload(payload, registry=None):
    if not isinstance(payload, dict):
        raise MLArtifactError("ML artifact payload is invalid.")
    if payload.get("purpose") != MODEL_PURPOSE:
        raise MLArtifactError("ML artifact purpose does not match production absenteeism.")
    if payload.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise MLArtifactError("ML artifact feature schema is incompatible.")
    if tuple(payload.get("feature_names") or ()) != tuple(EXPECTED_FEATURES):
        raise MLArtifactError("ML artifact feature names are incompatible.")
    if payload.get("target_definition") != TARGET_DEFINITION:
        raise MLArtifactError("ML artifact target definition is incompatible.")
    if not hasattr(payload.get("pipeline"), "predict_proba"):
        raise MLArtifactError("ML artifact does not contain a probability pipeline.")
    threshold = payload.get("threshold")
    if not isinstance(threshold, (int, float)) or not 0 <= float(threshold) <= 1:
        raise MLArtifactError("ML artifact threshold is invalid.")
    if registry is not None:
        if payload.get("model_version") != registry.model_version:
            raise MLArtifactError("ML artifact version does not match the registry.")
        if payload.get("model_key") != registry.model_key:
            raise MLArtifactError("ML artifact model key does not match the registry.")
        if float(threshold) != float(registry.selected_threshold):
            raise MLArtifactError("ML artifact threshold does not match the registry.")
    return payload


def load_registry_artifact(registry):
    if registry.purpose != MODEL_PURPOSE:
        raise MLArtifactError("Registry purpose is not production absenteeism.")
    if not registry.artifact_relative_path or not registry.artifact_sha256:
        raise MLArtifactError("Registry artifact metadata is incomplete.")
    root = private_ml_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _, path = _inside_private_root(root / registry.artifact_relative_path)
    if not path.is_file():
        raise MLArtifactError("Registered ML artifact is missing.")
    actual_hash = sha256_file(path)
    if actual_hash.lower() != registry.artifact_sha256.lower():
        raise MLArtifactError("Registered ML artifact checksum does not match.")
    try:
        payload = joblib.load(path)
    except Exception as exc:
        raise MLArtifactError("Registered ML artifact could not be loaded.") from exc
    return validate_artifact_payload(payload, registry=registry)
