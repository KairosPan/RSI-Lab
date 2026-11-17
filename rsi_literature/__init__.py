"""Source-backed literature capabilities for Lab's Literature subagent."""


def make_adapters(document_root, config=None):
    from .adapters import make_adapters as create
    return create(document_root, config)


__all__ = ['make_adapters']
