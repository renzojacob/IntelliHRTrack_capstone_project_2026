# Generated for the production advisory ML registry.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_branchconnector_biometricsyncrequest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MLModelRegistry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purpose", models.CharField(db_index=True, default="NEXT_PERIOD_QUALIFYING_ABSENCE", max_length=64)),
                ("model_key", models.CharField(db_index=True, max_length=64)),
                ("model_name", models.CharField(max_length=120)),
                ("model_version", models.CharField(max_length=100, unique=True)),
                ("status", models.CharField(choices=[("CANDIDATE", "Candidate"), ("VALIDATED", "Validated"), ("ACTIVE", "Active"), ("RETIRED", "Retired"), ("FAILED", "Failed")], db_index=True, default="CANDIDATE", max_length=20)),
                ("artifact_relative_path", models.CharField(blank=True, default="", max_length=500)),
                ("artifact_sha256", models.CharField(blank=True, default="", max_length=64)),
                ("dataset_version", models.CharField(max_length=64)),
                ("dataset_generated_at", models.DateTimeField()),
                ("trained_at", models.DateTimeField(blank=True, null=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("training_row_count", models.PositiveIntegerField(default=0)),
                ("employee_group_count", models.PositiveIntegerField(default=0)),
                ("positive_count", models.PositiveIntegerField(default=0)),
                ("negative_count", models.PositiveIntegerField(default=0)),
                ("target_definition", models.TextField()),
                ("feature_schema_version", models.CharField(max_length=64)),
                ("random_seed", models.PositiveIntegerField()),
                ("selected_threshold", models.FloatField(blank=True, null=True)),
                ("metrics", models.JSONField(blank=True, default=dict)),
                ("validation_metadata", models.JSONField(blank=True, default=dict)),
                ("training_code_version", models.CharField(blank=True, default="", max_length=120)),
                ("activation_provenance", models.CharField(blank=True, default="", max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activated_ml_models", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_ml_models", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "core_mlmodelregistry",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["purpose", "status"], name="core_mlmode_purpose_1f036c_idx"), models.Index(fields=["model_key", "status"], name="core_mlmode_model_k_897e69_idx")],
            },
        ),
        migrations.CreateModel(
            name="MLLifecycleEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("succeeded", models.BooleanField(db_index=True, default=False)),
                ("actor_label", models.CharField(blank=True, default="management-command", max_length=120)),
                ("message", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ml_lifecycle_events", to=settings.AUTH_USER_MODEL)),
                ("model", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="lifecycle_events", to="core.mlmodelregistry")),
            ],
            options={"db_table": "core_mllifecycleevent", "ordering": ["-created_at"]},
        ),
    ]
