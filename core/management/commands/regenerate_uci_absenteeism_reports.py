"""Regenerate Phase 2C interpretation artifacts without retraining."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.analytics.research.reporting import regenerate_research_artifacts


class Command(BaseCommand):
    help = "Regenerate Phase 2C JSON/Markdown interpretation from measured results; never trains models."

    def add_arguments(self, parser):
        parser.add_argument(
            "--results-json",
            default=(
                "research_results/phase2c_uci_absenteeism/"
                "uci_absenteeism_phase2c_results.json"
            ),
            help="Existing measured Phase 2C results JSON.",
        )
        parser.add_argument(
            "--output-dir",
            default="",
            help="Optional output directory; default is the JSON's current directory.",
        )

    def handle(self, *args, **options):
        source = Path(options["results_json"]).expanduser().resolve()
        if not source.is_file():
            raise CommandError(f"Measured results JSON not found: {source}")
        output = options["output_dir"].strip() or None
        try:
            results, artifacts = regenerate_research_artifacts(source, output)
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Phase 2C interpretation artifacts regenerated."))
        self.stdout.write(
            "ORIGINAL_RULE_SELECTED_MODEL="
            + results["original_rule_selected_model"]["display_name"]
        )
        self.stdout.write(
            "PRIMARY_RECALL_LEADER=" + results["primary_recall_leader"]["display_name"]
        )
        self.stdout.write(
            "STRONGEST_OVERALL_DISCRIMINATION="
            + results["strongest_overall_discrimination"]["display_name"]
        )
        self.stdout.write(
            "DUPLICATE_SENSITIVITY_WINNER="
            + results["duplicate_sensitivity_winner"]["display_name"]
        )
        robust = results["robust_single_winner"]
        self.stdout.write(
            "ROBUST_SINGLE_WINNER=" + (robust["display_name"] if robust else "NONE")
        )
        self.stdout.write(f"Results JSON: {artifacts['json']}")
        self.stdout.write(f"Markdown report: {artifacts['markdown']}")
