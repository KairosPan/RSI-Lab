"""Thin legacy facade; the generic Runtime contains no docking dispatch logic."""
from rsi_lab.runtime import Runtime as _Runtime
from rsi_lab.capabilities import execute_acceptance
from rsi_domains.docking.plugin import make_adapter, ADAPTER_ID, EFFECT_KIND
from .controller import Controller
from .trials import TrialEngine
from .packets import PacketBuilder
from .roles import MetaAgent


class Runtime(_Runtime):
    controller_factory = Controller
    trial_engine_factory = TrialEngine
    packet_builder_factory = PacketBuilder
    meta_agent_factory = MetaAgent

    def __init__(self,store,run_id,*,adapters=()):
        super().__init__(store,run_id,adapters=(make_adapter(),*adapters))

    def inspect_docking(self,data_dir,protocol_path,*,sources=None,scope='exploratory',trigger_id):
        result = self.execute_adapter(ADAPTER_ID,{'data_dir':str(data_dir),'protocol_path':str(protocol_path),
            'sources':sources or {},'scope':scope},trigger_id=trigger_id)
        return {'effect':result['effect'],'inspection':result['result']}

    def publish_adapter_effect(self,effect):
        if effect['kind'] == EFFECT_KIND:
            return self._publish_inspection(effect)
        return super().publish_adapter_effect(effect)

    def _publish_inspection(self,effect):
        return super().publish_adapter_effect(effect)
