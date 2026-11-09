"""Legacy model-envelope compatibility; reasoning and permission rules are shared."""
from rsi_lab.roles import *
from rsi_lab.roles import CodexExecutor as _CodexExecutor


class CodexExecutor(_CodexExecutor):
    def __init__(self,calls_root,*,timeout_seconds=300):
        super().__init__(calls_root,timeout_seconds=timeout_seconds,request_schema='docking_rsi_model_request_v2')
