"""Legacy trial wire profile over the shared RSI TrialEngine."""
from rsi_lab.trials import *  # noqa: F401,F403
from rsi_lab import trials as _core

SCHEMA_VERSION = 'docking_trial_v2'


def validate_trial_spec(spec):
    return _core.validate_trial_spec(spec, schema_version=SCHEMA_VERSION)


def settle_trial(spec, measurements, *, candidate_applied=False, restoration_confirmed=False,
                 additional_costs=(), recovery_costs=()):
    return _core.settle_trial(spec, measurements, candidate_applied=candidate_applied,
        restoration_confirmed=restoration_confirmed, additional_costs=additional_costs,
        recovery_costs=recovery_costs, schema_version=SCHEMA_VERSION)


class TrialEngine(_core.TrialEngine):
    def __init__(self, store, run_id):
        super().__init__(store, run_id, schema_version=SCHEMA_VERSION)
