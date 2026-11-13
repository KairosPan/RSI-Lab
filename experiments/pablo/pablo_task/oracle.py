"""Private, frozen original benchmark implementation; never expose to agents."""
from functools import lru_cache
import hashlib
import importlib.metadata
import json
from pathlib import Path


def canonicalize(value):
    from rdkit import Chem
    if not isinstance(value, str) or not value.strip():
        return None
    molecule = Chem.MolFromSmiles(value.strip())
    return None if molecule is None else Chem.MolToSmiles(molecule, canonical=True)


@lru_cache(maxsize=1)
def _objective():
    import numpy as np
    import scipy
    if not hasattr(scipy, 'histogram'):
        scipy.histogram = np.histogram
    from guacamol import standard_benchmarks
    return standard_benchmarks.amlodipine_rings().objective


def score(candidate):
    return float(_objective().score(candidate))


def fingerprint():
    """Opaque implementation/runtime binding, safe to include in receipts."""
    value = {'source': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'dependencies': {name: importlib.metadata.version(name) for name in
                              ('guacamol', 'rdkit', 'numpy', 'scipy')}}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
