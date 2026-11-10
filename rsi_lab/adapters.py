"""Explicit host-registered capabilities; the scheduler knows no domain names."""
from dataclasses import dataclass
import inspect
import json
import marshal
from pathlib import Path
from typing import Callable

from .store import digest, encode


@dataclass(frozen=True)
class Adapter:
    """A host-registered capability.

    ``prepare`` validates inputs and builds the execution packet. It must be
    free of side effects: the runtime also calls it as a dry run when it
    admits a tool request, and raises ValueError/KeyError/TypeError from it
    reject the request before any Work is queued.
    """
    adapter_id: str
    version: str
    effect_kind: str
    manifest: dict
    prepare: Callable
    reservations: Callable
    execute: Callable
    publication: Callable
    source_paths: tuple = ()
    legacy_effect_kinds: tuple = ()
    requires_acceptance: bool = False
    # Optional domain interpretation of a settled publication. The controller
    # stores its JSON result separately from controller-owned execution facts.
    measure: Callable | None = None

    def __post_init__(self):
        for value in (self.adapter_id, self.version, self.effect_kind):
            if not isinstance(value, str) or not value or len(value) > 160:
                raise ValueError('Adapter identities must be explicit bounded strings')
        if self.effect_kind in {'lab', 'meta', 'capability_acceptance'}:
            raise ValueError('Adapter may not impersonate a controller-owned effect kind')
        if any(k in {'lab','meta','capability_acceptance'} for k in self.legacy_effect_kinds):
            raise ValueError('Legacy aliases cannot take over controller-owned recovery')
        if not isinstance(self.manifest, dict) or not self.manifest:
            raise ValueError('Adapter must describe its applicability and contracts')
        encode(self.manifest)
        if type(self.requires_acceptance) is not bool:
            raise ValueError('Explicit adapter acceptance policy is required')
        for fn in (self.prepare, self.reservations, self.execute, self.publication):
            if not callable(fn):
                raise ValueError('A registered adapter requires executable callbacks')
        if self.measure is not None and not callable(self.measure):
            raise ValueError('An adapter measurement provider must be callable')

    def descriptor(self):
        sources, callbacks = {}, {}
        names = ('prepare','reservations','execute','publication')
        if self.measure is not None:
            names += ('measure',)
        for name in names:
            fn = getattr(self,name)
            if not inspect.isfunction(fn) or fn.__closure__:
                raise ValueError('Adapter callbacks must be source-backed functions; pass configuration explicitly, not captured state')
            path = inspect.getsourcefile(fn)
            if path is None:
                raise ValueError('Adapter implementation must have inspectable source')
            sources[str(Path(path).resolve())] = digest(Path(path).read_bytes())
            callbacks[name] = {'module':fn.__module__,'qualname':fn.__qualname__,
                'code_sha256':digest(marshal.dumps(fn.__code__)),
                'defaults':json.loads(encode(fn.__defaults__)), 'keyword_defaults':json.loads(encode(fn.__kwdefaults__))}
        for path in self.source_paths:
            path = Path(path).resolve()
            sources[str(path)] = digest(path.read_bytes())
        return {'schema_version': 'rsi_adapter_descriptor_v1', 'adapter_id': self.adapter_id,
                'version': self.version, 'effect_kind': self.effect_kind,
                'manifest': json.loads(encode(self.manifest)), 'implementation_sources': sources,
                'callbacks':callbacks,'requires_acceptance':self.requires_acceptance}


class AdapterRegistry:
    def __init__(self, adapters=()):
        self.by_id, self.by_kind, self.legacy = {}, {}, {}
        for adapter in adapters:
            if not isinstance(adapter, Adapter):
                raise ValueError('Only explicit host-registered Adapter objects are admitted')
            if adapter.adapter_id in self.by_id or adapter.effect_kind in self.by_kind:
                raise ValueError('Adapter and effect identities must be unique in a runtime')
            adapter.descriptor()
            self.by_id[adapter.adapter_id] = adapter
            self.by_kind[adapter.effect_kind] = adapter
            for kind in adapter.legacy_effect_kinds:
                if kind in self.legacy:
                    raise ValueError('Conflicting legacy recovery ownership')
                self.legacy[kind] = adapter

    def get(self, adapter_id):
        try:
            return self.by_id[adapter_id]
        except KeyError as exc:
            raise ValueError('Adapter is not loaded: ' + str(adapter_id)) from exc

    def resolve(self, effect):
        metadata = effect.get('metadata', {})
        aid = metadata.get('adapter_id')
        if aid is None:
            if effect['kind'] not in self.legacy:
                raise ValueError('No explicit recovery adapter for this legacy effect')
            return self.legacy[effect['kind']]
        adapter = self.get(aid)
        if (effect['kind'] != adapter.effect_kind or metadata.get('adapter_version') != adapter.version
                or metadata.get('adapter_descriptor_sha256') != digest(adapter.descriptor())):
            raise ValueError('Recovery requires the exact original adapter version and implementation')
        return adapter
