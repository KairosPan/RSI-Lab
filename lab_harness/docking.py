"""Frozen evaluation of independent AutoDock Vina CPU runs."""
from scripts.vina_contract import (
    BATCH_SCHEMA, ENGINE, ENGINE_VERSION, POSE_SELECTION, SEEDS, WORKER_FILES,
    parse_vina_poses, validate_vina_protocol,
)

EVALUATOR_ID = "autodock_vina_affinity_v1"
SCORE_FIELD = "qualified_median_vina_affinity"
SCORE_DIRECTION = "lower"
SCORE_UNITS = "kcal/mol"
TOOL_VERSION = f"{ENGINE} {ENGINE_VERSION}"


def evaluator_descriptor():
    return {
        "evaluator_id": EVALUATOR_ID,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "score_field": SCORE_FIELD,
        "score_direction": SCORE_DIRECTION,
        "score_units": SCORE_UNITS,
        "seeds": list(SEEDS),
        "aggregation": "Median model-1 affinity from the two completed, identity-preserving, valid poses",
        "pose_selection": POSE_SELECTION,
    }
