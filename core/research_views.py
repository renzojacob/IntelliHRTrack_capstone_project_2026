"""Superuser-only, read-only views for public-dataset research evidence."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from core.analytics.research.presentation import ResearchArtifactError, load_research_evaluation


@login_required
@never_cache
def admin_ml_research_evaluation(request):
    if not request.user.is_superuser:
        raise PermissionDenied("This capstone research page is restricted to superusers.")

    try:
        research = load_research_evaluation()
        artifact_error = None
    except ResearchArtifactError:
        research = None
        artifact_error = (
            "The committed research results are temporarily unavailable. "
            "No production attendance or payroll function is affected."
        )

    return render(
        request,
        "admin/ml_research_evaluation.html",
        {
            "current": "ml_research",
            "research": research,
            "artifact_error": artifact_error,
        },
    )
