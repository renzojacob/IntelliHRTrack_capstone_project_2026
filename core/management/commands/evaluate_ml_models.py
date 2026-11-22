from core.analytics.production_ml.anomaly import PURPOSE
from .evaluate_absenteeism_models import Command as EvaluateSupervised


class Command(EvaluateSupervised):
    help = "Verify both production model families and report their stored evaluation evidence."
    purposes = (*EvaluateSupervised.purposes, PURPOSE)
