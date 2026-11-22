from django.core.management.base import BaseCommand, CommandError

from core.analytics.production_ml.registry import MLActivationError, activate_validated_model


class Command(BaseCommand):
    help = "Activate one VALIDATED production advisory model by registry ID or version."

    def add_arguments(self, parser):
        parser.add_argument("identifier", help="MLModelRegistry ID or exact model version")
        parser.add_argument("--actor-label", default="management-command", help="Non-user activation provenance label")

    def handle(self, *args, **options):
        try:
            model = activate_validated_model(
                options["identifier"],
                actor_label=options["actor_label"],
            )
        except MLActivationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Activated {model.model_name} {model.model_version}."))
