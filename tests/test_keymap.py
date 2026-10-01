import ctypes.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from herdr_shell import keymap as k


class KeymapTests(unittest.TestCase):
    def source(self, content):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'bindings.lua'
        path.write_text(content)
        return path

    @staticmethod
    def binding(key='', *, mask=72, code=0, label='Desktop action', submap='', universal=False, **kwargs):
        return dict(key=key, modmask=mask, keycode=code, description=label,
                    submap=submap, submap_universal=universal, **kwargs)

    def check(self, keys, binds, **kwargs):
        return k.collisions([(keys, 'test-action', 'Action')], binds, **kwargs)

    def test_named_modifier_and_page_aliases(self):
        for keys in ('meta+mod1+Prior', 'SUPER + ALT + page_up', 'alt+super+Page Up',
                     'SUPER+ALT+page-up', 'SUPER+ALT+PgUp'):
            with self.subTest(keys=keys):
                self.assertTrue(self.check(keys, [self.binding('PAGEUP')]))
        self.assertEqual(k.chord('control+CTRL+super+Return'), (68, 'RETURN'))
        self.assertEqual(k.chord('SUPER+ALT+['), (72, 'BRACKETLEFT'))
        with self.assertRaises(ValueError):
            k.chord('SUPER+UNKNOWN+M')

    def test_named_collision_checks_exact_modifiers(self):
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding('M')]))
        self.assertFalse(self.check('SUPER+ALT+M', [self.binding('M', mask=64)]))
        self.assertFalse(self.check('SUPER+ALT+M', [self.binding('Q')]))

    def test_other_submaps_do_not_block_except_universal(self):
        for universal in (False, 'false', None):
            with self.subTest(universal=universal):
                self.assertFalse(self.check('SUPER+ALT+M', [self.binding('M', submap='resize', universal=universal)]))
        for universal in (True, 'true', 'TRUE'):
            with self.subTest(universal=universal):
                self.assertTrue(self.check('SUPER+ALT+M', [self.binding('M', submap='resize', universal=universal)]))
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding('M', submap='reset')]))

    def test_catchall_blocks_every_key_with_same_modifiers(self):
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding(catch_all=True)]))
        self.assertFalse(self.check('SUPER+ALT+M', [self.binding(mask=64, catch_all=True)]))

    def test_literal_source_recovery_ignores_comments_and_never_executes(self):
        source = self.source('''
-- o.bind("SUPER + ALT + code:24", "Comment", "bad")
--[=[ o.bind("SUPER + ALT + code:25", "Block comment", "bad") ]=]
os.execute("touch SHOULD_NOT_EXIST")
local note = 'o.bind("SUPER + ALT + code:26", "Inside string", "bad")'
o.bind('SUPER + ALT + code:27', 'Description -- is not a comment', "anything")
hl.bind("SUPER + ALT + code:28", hl.dsp.focus({direction="l"}), {description="Native declaration"})
''')
        sentinel = source.parent / 'SHOULD_NOT_EXIST'
        source.write_text(source.read_text().replace('touch SHOULD_NOT_EXIST', 'touch ' + str(sentinel)))
        recovered = k.source_positions([source])
        self.assertEqual(recovered, {(72, 'Description -- is not a comment'): {27},
                                     (72, 'Native declaration'): {28}})
        self.assertFalse(sentinel.exists())

    def test_numeric_loops_resolve_local_physical_alias_and_descriptions(self):
        source = self.source('''
for workspace = 1, 10 do
  local key = "code:" .. tostring(workspace + 9)
  o.bind("SUPER + " .. key, "Workspace " .. workspace, {})
  o.bind("SUPER + SHIFT + " .. key, "Move " .. tostring(workspace), {})
end
for panel = 1, 9 do
  o.bind("SUPER + CTRL + code:" .. tostring(panel + 9), "Panel " .. panel, {})
end
''')
        recovered = k.source_positions([source])
        self.assertEqual(len(recovered), 29)
        self.assertEqual(recovered[(64, 'Workspace 10')], {19})
        self.assertEqual(recovered[(65, 'Move 1')], {10})
        self.assertEqual(recovered[(68, 'Panel 9')], {18})

    def test_loop_scopes_and_nested_bounded_loops(self):
        source = self.source('''
for n = 1, 2 do
 local key = "code:" .. tostring(n + 9)
 for p = 1, 2 do
  o.bind("SUPER + " .. key, "Nested " .. n .. p, {})
 end
end
for n = 3, 4 do
 o.bind("SUPER + code:" .. tostring(n + 9), "Later " .. n, {})
end
o.bind("SUPER + " .. key, "Out of scope", {})
''')
        recovered = k.source_positions([source])
        self.assertEqual(len(recovered), 6)
        self.assertEqual(recovered[(64, 'Nested 12')], {10})
        self.assertEqual(recovered[(64, 'Later 4')], {13})
        self.assertNotIn((64, 'Out of scope'), recovered)

    def test_dynamic_alias_reassignment_is_not_guessed(self):
        source = self.source('''
local key = "code:24"
key = arbitrary_function()
o.bind("SUPER + ALT + " .. key, "Dynamic", {})
local transformed = "code:25":gsub("25", "26")
o.bind("SUPER + ALT + " .. transformed, "Transformed", {})
''')
        self.assertEqual(k.source_positions([source]), {})
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding(label='Dynamic')],
                                   positions=k.source_positions([source])))

    def test_oversized_loops_are_not_expanded(self):
        source = self.source('''
for n = 1, 1000000 do
 o.bind("SUPER + code:" .. tostring(n), "Huge " .. n, {})
end
o.bind("SUPER + code:24", "After loop", {})
''')
        self.assertEqual(k.source_positions([source]), {(64, 'After loop'): {24}})

    def test_unknown_physical_entry_fails_closed_only_in_relevant_modifiers(self):
        self.assertIn('Cannot identify', self.check('SUPER+ALT+M', [self.binding()])['test-action'])
        self.assertFalse(self.check('SUPER+ALT+M', [self.binding(mask=64)]))
        with patch.object(k, 'physical_names', return_value={}):
            self.assertIn('Cannot resolve', self.check('SUPER+ALT+M', [self.binding(code=24)])['test-action'])
            self.assertTrue(self.check('SUPER+ALT+code:24', []))

    def test_physical_named_and_literal_code_collisions(self):
        with patch.object(k, 'physical_names', return_value={24: {'Q'}}):
            for binding in (self.binding(code=24), self.binding('code:24'),
                            self.binding('irrelevant-symbol', code=24)):
                with self.subTest(binding=binding):
                    self.assertTrue(self.check('SUPER+ALT+Q', [binding]))
            self.assertTrue(self.check('SUPER+ALT+code:24', [self.binding('Q')]))
            self.assertTrue(self.check('SUPER+ALT+code:24', [self.binding(code=24)]))
            self.assertFalse(self.check('SUPER+ALT+M', [self.binding(code=24)]))

    def test_recovered_blank_live_binding_collides_with_named_alias(self):
        positions = {(72, 'Physical action'): {24}}
        with patch.object(k, 'physical_names', return_value={24: {'Q'}}):
            self.assertTrue(self.check('SUPER+ALT+Q', [self.binding(label='Physical action')], positions=positions))
            self.assertFalse(self.check('SUPER+ALT+M', [self.binding(label='Physical action')], positions=positions))

    def test_ambiguous_partial_physical_recovery_fails_closed(self):
        with patch.object(k, 'physical_names', return_value={24: {'Q'}, 999: set()}):
            result = self.check('SUPER+ALT+M', [self.binding()], positions={(72, 'Desktop action'): {24, 999}})
            self.assertIn('Cannot resolve', result['test-action'])

    def test_managed_description_exemption_requires_actual_chord_and_lua_dispatcher(self):
        self.assertFalse(self.check('SUPER+ALT+M', [self.binding('M', label='Herdr Shell: test-action', dispatcher='__lua')]))
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding('M', label='Herdr Shell: test-action', dispatcher='exec')]))
        self.assertTrue(self.check('SUPER+ALT+M', [self.binding(label='Herdr Shell: test-action', dispatcher='__lua')]))

    def test_internal_profile_alias_collision(self):
        result = k.collisions([('SUPER+ALT+PageUp', 'first', ''), ('ALT+SUPER+Prior', 'second', '')], [])
        self.assertEqual(set(result), {'first', 'second'})
        with patch.object(k, 'physical_names', return_value={24: {'Q'}}):
            result = k.collisions([('SUPER+ALT+code:24', 'first', ''), ('SUPER+ALT+Q', 'second', '')], [])
        self.assertEqual(set(result), {'first', 'second'})

    @unittest.skipUnless(ctypes.util.find_library('xkbcommon'), 'xkbcommon is unavailable')
    def test_xkb_uses_layout_variants_and_every_group(self):
        self.assertIn('Q', k.physical_names({24}, 'us')[24])
        self.assertIn('A', k.physical_names({24}, 'fr')[24])
        self.assertIn('APOSTROPHE', k.physical_names({24}, 'us', 'dvorak')[24])
        names = k.physical_names({24}, 'us,fr')[24]
        self.assertTrue({'Q', 'A'} <= names)
        self.assertEqual(k.physical_names({65535}, 'us')[65535], set())
        self.assertTrue(self.check('SUPER+ALT+A', [self.binding(code=24)], layout='fr'))
        self.assertFalse(self.check('SUPER+ALT+Q', [self.binding(code=24)], layout='fr'))

    @unittest.skipUnless(Path('/tmp/herdr-super-alt-audit-20261001.json').exists(), 'Local audit snapshot unavailable')
    def test_current_omarchy_blank_physical_records_and_agreed_profile(self):
        registered = json.loads(Path('/tmp/herdr-super-alt-audit-20261001.json').read_text())
        paths = list(Path('/usr/share/omarchy/default/hypr/bindings').glob('*.lua'))
        paths += list(Path.home().joinpath('.config/hypr').glob('*.lua'))
        positions = k.source_positions(paths)
        for number in range(1, 11):
            self.assertEqual(positions[(64, f'Switch to workspace {number}')], {number + 9})
            self.assertEqual(positions[(65, f'Move window to workspace {number}')], {number + 9})
            self.assertEqual(positions[(73, f'Move window silently to workspace {number}')], {number + 9})
        self.assertTrue(self.check('SUPER+ALT+1', registered, positions=positions))
        self.assertTrue(self.check('SUPER+ALT+minus', registered, positions=positions))
        self.assertTrue(self.check('SUPER+ALT+[', registered, positions=positions))
        basic = ('M', 'U', 'D', 'L', 'R', 'J', 'Z', 'X', 'T', 'W', 'PageUp', 'PageDown', 'A', 'Q', 'V', 'P')
        shifted = ('U', 'D', 'L', 'R', 'T', 'W', 'PageUp', 'PageDown', 'Q', 'P')
        mappings = [('SUPER+ALT+' + key, 'basic-' + key, '') for key in basic]
        mappings += [('SUPER+ALT+SHIFT+' + key, 'shift-' + key, '') for key in shifted]
        self.assertEqual(len(mappings), 26)
        self.assertEqual(k.collisions(mappings, registered, positions=positions), {})


if __name__ == '__main__':
    unittest.main()
