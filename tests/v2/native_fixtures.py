"""Normal-form native settings shared by the native-capability tests."""

SEARCH = {'web_search': True, 'terminal': None, 'subagents': None}
TERMINAL = {'web_search': False, 'terminal': {'readable_roots': []}, 'subagents': None}
SUBAGENTS = {'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 3}}
TERMINAL_AND_SEARCH = {'web_search': True, 'terminal': {'readable_roots': []}, 'subagents': None}
TERMINAL_AND_SUBAGENTS = {'web_search': False, 'terminal': {'readable_roots': []}, 'subagents': {'max_concurrent': 3}}
