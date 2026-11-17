"""Adapter code identity depends only on what a callback does, not on process state."""
import unittest
import warnings

from rsi_lab.adapters import code_identity
from rsi_lab.store import digest
from tests.v2.liveness_support import lenient_adapter, open_run, validating_adapter


SOURCE = '''
def prepare(inputs, limit=3):
    def check(value):
        return value in {'a', 'b', 'c'} and (1.5, b'x', None, ...) is not None
    if not check(inputs.get('k')):
        raise ValueError('rejected input')
    return inputs
'''


def load(filename, source=SOURCE):
    namespace = {}
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)  # the tuple identity check in SOURCE is deliberate
        code = compile(source, filename, 'exec')
    exec(code, namespace)
    return namespace['prepare']


class CodeIdentityTests(unittest.TestCase):
    def test_same_source_under_two_file_names_has_one_identity(self):
        self.assertEqual(code_identity(load('/first/a.py').__code__), code_identity(load('/second/b.py').__code__))

    def test_identity_ignores_line_position(self):
        self.assertEqual(code_identity(load('/first/a.py', '\n\n\n' + SOURCE).__code__),
                         code_identity(load('/first/a.py').__code__))

    def test_identity_changes_with_a_constant(self):
        self.assertNotEqual(code_identity(load('/first/a.py', SOURCE.replace('rejected input', 'refused input')).__code__),
                            code_identity(load('/first/a.py').__code__))

    def test_identity_changes_with_nested_code(self):
        self.assertNotEqual(code_identity(load('/first/a.py', SOURCE.replace('value in', 'value not in')).__code__),
                            code_identity(load('/first/a.py').__code__))

    def test_identity_is_stable_while_constants_are_held(self):
        function = load('/first/a.py'); before = code_identity(function.__code__)
        held = list(function.__code__.co_consts)
        self.assertEqual(code_identity(function.__code__), before)
        self.assertTrue(held)


class DescriptorTests(unittest.TestCase):
    def test_descriptor_is_stable_while_an_error_message_is_held(self):
        adapter = validating_adapter(); before = digest(adapter.descriptor()); held = None
        try:
            adapter.prepare({'wrong': 1})
        except ValueError as error:
            held = str(error)
        self.assertEqual(digest(adapter.descriptor()), before)
        self.assertEqual(held, 'Probe adapter requires exactly record_id')

    def test_descriptor_distinguishes_different_callbacks(self):
        self.assertNotEqual(validating_adapter().descriptor()['callbacks']['prepare']['code_sha256'],
                            lenient_adapter().descriptor()['callbacks']['prepare']['code_sha256'])

    def test_descriptor_schema_is_v2(self):
        self.assertEqual(validating_adapter().descriptor()['schema_version'], 'rsi_adapter_descriptor_v2')

    def test_effect_still_resolves_while_its_adapter_error_text_is_held(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        effect = rt.execute_adapter('probe', {'record_id': 'a'}, trigger_id='t1')['effect']
        held = None
        try:
            rt.adapters.get('probe').prepare({'wrong': 1})
        except ValueError as error:
            held = str(error)
        self.assertIs(rt.adapters.resolve(store.get('r', 'effect', effect['id'])), rt.adapters.get('probe'))
        self.assertEqual(rt.recover()['adapter_results'], [{'effect_id': effect['id'], 'status': 'published'}])
        self.assertTrue(held)


if __name__ == '__main__':
    unittest.main()
