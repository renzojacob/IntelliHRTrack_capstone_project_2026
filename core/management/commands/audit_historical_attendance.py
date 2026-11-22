"""Aggregate-only, read-only audit for historical attendance files."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.analytics.historical_intake import audit_historical_file
from core.models import Branch


class Command(BaseCommand):
    help = "Audit a historical CSV/XLS/XLSX attendance file. Never writes to the database."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Local historical attendance file")
        parser.add_argument(
            "--branch",
            required=True,
            help="Existing target branch ID or exact branch name",
        )

    def handle(self, *args, **options):
        file_path = Path(options["file"]).expanduser().resolve()
        if not file_path.is_file():
            raise CommandError("Historical attendance file was not found.")

        branch_input = str(options["branch"]).strip()
        if branch_input.isdigit():
            branch = Branch.objects.filter(pk=int(branch_input)).first()
        else:
            branch = Branch.objects.filter(name=branch_input).first()
        if branch is None:
            raise CommandError("Target branch must already exist and match exactly.")

        try:
            with file_path.open("rb") as source:
                report = audit_historical_file(
                    source,
                    file_path.name,
                    branch=branch,
                )
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        self.stdout.write(self.style.WARNING("DRY-RUN ONLY: zero database writes performed."))
