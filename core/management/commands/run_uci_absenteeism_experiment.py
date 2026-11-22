"""Run the isolated Phase 2C public research experiment."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.analytics.research.experiment import run_phase2c_experiment
from core.analytics.research.reporting import write_research_artifacts
from core.analytics.research.uci_absenteeism import PHASE2B_SOURCE_SHA256


class Command(BaseCommand):
    help = "Run the external UCI Phase 2C experiment; never reads or writes production models."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default="local_data/Absenteeism_at_work.csv",
            help="Path to the semicolon-delimited public UCI CSV.",
        )
        parser.add_argument(
            "--output-dir",
            default="research_results/phase2c_uci_absenteeism",
            help="Directory for JSON and Markdown results (must not be under local_data).",
        )

    def handle(self, *args, **options):
        source = Path(options["file"]).expanduser().resolve()
        output = Path(options["output_dir"]).expanduser().resolve()
        local_data = (Path.cwd() / "local_data").resolve()
        if output == local_data or local_data in output.parents:
            raise CommandError("Research results must not be written under local_data/.")
        if not source.is_file():
            raise CommandError(f"Public research CSV not found: {source}")

        self.stdout.write("Running nested employee-group-aware Phase 2C research experiment...")
        try:
            results = run_phase2c_experiment(
                source,
                expected_source_sha256=PHASE2B_SOURCE_SHA256,
            )
            artifacts = write_research_artifacts(results, output)
        except (AssertionError, RuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        selected = results["selected_public_research_model"]
        self.stdout.write(self.style.SUCCESS("Phase 2C public research experiment completed."))
        self.stdout.write(f"SELECTED_PUBLIC_RESEARCH_MODEL={selected['display_name']}")
        self.stdout.write(f"Results JSON: {artifacts['json']}")
        self.stdout.write(f"Markdown report: {artifacts['markdown']}")
        self.stdout.write(results["external_validity_disclaimer"])
