"""Legacy branch wire identifiers; execution belongs to the shared RSI runner."""
from rsi_lab.branches import *  # noqa: F401,F403
from rsi_lab import branches as _core

LEGACY_WIRE = BranchProtocol(trial_schema='docking_trial_v2',
    packet_schema='docking_branch_packet_v2', input_schema='docking_trial_input_v2',
    output_schema='docking_trial_output_v2')


def make_branch_packet(store, control_run_id, trial_id, pair_id, arm, input_value, adapter_path, *, execution_authorization_sha256):
    return _core.make_branch_packet(store, control_run_id, trial_id, pair_id, arm, input_value, adapter_path,
        execution_authorization_sha256=execution_authorization_sha256, wire=LEGACY_WIRE)


def branch_reservations(store, control_run_id, trial_id):
    return _core.branch_reservations(store, control_run_id, trial_id, wire=LEGACY_WIRE)


class BranchExecutor(_core.BranchExecutor):
    def __init__(self, store, control_run_id):
        super().__init__(store, control_run_id, wire=LEGACY_WIRE)
