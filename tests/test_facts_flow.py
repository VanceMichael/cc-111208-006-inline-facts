"""End-to-end regression tests for the facts ingestion business flow.

These prove the new receive pipeline (docs/acceptance.md); the original
checker behaviour remains covered by tests/test_baseline.py.
"""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from supplierfacts.ixbrl import (
    CONTINUATION_CYCLE, CONTINUATION_DANGLING, CONTINUATION_DUPLICATE,
    DocumentError, UNKNOWN_TRANSFORM, extract, parse_number,
)
from supplierfacts.receive import Receiver

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = (FIXTURES / 'synthetic-disclosure.xhtml').read_bytes()
CYCLE = (FIXTURES / 'cycle.xhtml').read_bytes()


def group(facts, local_name, namespace='urn:synthetic:supplier'):
    matches = [fact for fact in facts
               if fact['concept']['local'] == local_name
               and fact['concept']['namespace'] == namespace]
    assert len(matches) == 1, local_name
    return matches[0]


def source_ids(fact):
    return [source['node_id'] for source in fact['sources']]


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.result = extract(SYNTHETIC)

    def test_scaled_negative_uses_exact_decimal_and_keeps_precision(self):
        amount = group(self.result['facts'], 'amount')
        f1 = next(source for source in amount['sources'] if source['node_id'] == 'f1')
        # "1.2" with scale 4 and sign -, no regex guesswork: exact Decimal.
        self.assertEqual(f1['value'], '-12000')
        self.assertEqual(f1['raw'], '1.2')
        self.assertEqual(f1['decimals'], '0')      # precision declaration kept
        self.assertEqual(f1['scale'], '4')
        self.assertEqual(f1['sign'], '-')
        self.assertEqual(Decimal('-12000'), Decimal(f1['value']))

    def test_bracketed_number_is_negative(self):
        bracketed = group(self.result['facts'], 'bracketed')
        self.assertEqual(bracketed['values'], ['-1234.5'])
        self.assertEqual(bracketed['sources'][0]['raw'], '(1,234.50)')

    def test_comma_decimal_transform_with_scale(self):
        scaled = group(self.result['facts'], 'scaled')
        self.assertEqual(scaled['values'], ['1234500'])  # 1.234,50 * 10^3
        self.assertEqual(scaled['status'], 'merged')

    def test_conflict_group_keeps_every_source_without_overwrite(self):
        amount = group(self.result['facts'], 'amount')
        self.assertEqual(amount['status'], 'conflict')
        self.assertEqual(amount['values'], ['-12000', '9'])
        self.assertEqual(source_ids(amount), ['f1', 'f2', 'f3'])
        # f2/f3 share the canonical value 9 but keep distinct precision claims.
        f2, f3 = (next(s for s in amount['sources'] if s['node_id'] == n)
                  for n in ('f2', 'f3'))
        self.assertEqual(f2['decimals'], '0')
        self.assertEqual(f3['decimals'], '2')

    def test_same_local_name_different_namespace_is_not_merged(self):
        alt = group(self.result['facts'], 'amount',
                    namespace='urn:synthetic:supplier-alt')
        self.assertEqual(alt['values'], ['5'])
        self.assertEqual(source_ids(alt), ['f10'])

    def test_unknown_transform_is_isolated_with_reason_no_guess(self):
        codes = {diagnostic['code'] for diagnostic in self.result['diagnostics']}
        self.assertIn(UNKNOWN_TRANSFORM, codes)
        isolated = next(item for item in self.result['isolated']
                        if item['node_id'] == 'f7')
        self.assertIn('not registered', isolated['reason'])
        self.assertNotIn('f7', [s['node_id']
                                for fact in self.result['facts']
                                for s in fact['sources']])

    def test_nil_fact_is_recorded_without_value(self):
        nilval = group(self.result['facts'], 'nilval')
        self.assertTrue(nilval['nil'])
        self.assertEqual(nilval['values'], [None])

    def test_numeric_transform_grammar(self):
        self.assertEqual(parse_number('1,234.50', 'dot'), Decimal('1234.50'))
        self.assertEqual(parse_number('1.234,50', 'comma'), Decimal('1234.50'))
        self.assertEqual(parse_number('(1,234.50)', 'dot'), Decimal('-1234.50'))
        self.assertIsNone(parse_number('1.2.3', 'dot'))
        self.assertIsNone(parse_number('abc', 'comma'))
        self.assertEqual(parse_number('9', None), Decimal('9'))
        self.assertIsNone(parse_number('1,2', None))

    def test_continued_text_chain_excludes_ix_exclude_footnote(self):
        note = group(self.result['facts'], 'note')
        self.assertEqual(note['values'], ['服务说明续'])
        self.assertNotIn('脚注', note['values'][0])

    def test_duplicate_continuation_reference_is_diagnosed_and_located(self):
        diagnostic = next(d for d in self.result['diagnostics']
                          if d['code'] == CONTINUATION_DUPLICATE)
        self.assertEqual(diagnostic['node_id'], 'tail1')
        self.assertTrue(diagnostic['locator'])
        self.assertIn('n2', [item['node_id'] for item in self.result['isolated']])

    def test_dangling_continuation_is_diagnosed_and_fact_isolated(self):
        dangling = [d for d in self.result['diagnostics']
                    if d['code'] == CONTINUATION_DANGLING]
        self.assertEqual({d['node_id'] for d in dangling}, {'f8', 'n3'})
        isolated_nodes = {item['node_id'] for item in self.result['isolated']}
        self.assertIn('f8', isolated_nodes)
        self.assertIn('n3', isolated_nodes)

    def test_cycle_continuation_is_located(self):
        result = extract(CYCLE)
        cycle = next(d for d in result['diagnostics']
                     if d['code'] == CONTINUATION_CYCLE)
        self.assertEqual(cycle['node_id'], 'tail')
        self.assertIn("'tail'", cycle['reason'])
        # No non-numeric fact escapes a cyclic chain.
        self.assertFalse(any(fact['kind'] == 'nonNumeric'
                             for fact in result['facts']))

    def test_dimension_reorder_and_prefix_swap_preserve_identity(self):
        renamed = SYNTHETIC
        for old, new in [(b'xmlns:ex=', b'xmlns:vendor='), (b'ex:', b'vendor:'),
                         (b'xmlns:xbrli=', b'xmlns:inst='), (b'xbrli:', b'inst:'),
                         (b'xmlns:iso=', b'xmlns:cur='), (b'iso:', b'cur:'),
                         (b'xmlns:ixt=', b'xmlns:t='), (b'ixt:', b't:')]:
            renamed = renamed.replace(old, new)
        identity = lambda result: sorted(
            (f['kind'], f['concept']['namespace'], f['concept']['local'],
             f['context_key'], f['unit_key'], f['status'], tuple(f['values']))
            for f in result['facts'])
        self.assertEqual(identity(self.result), identity(extract(renamed)))
        contexts = {c['id']: c['key'] for c in self.result['contexts']}
        self.assertEqual(contexts['c2'], contexts['c3'])

    def test_unit_numerator_denominator_and_measures_are_normalized(self):
        units = {unit['id']: unit['canonical'] for unit in self.result['units']}
        divide = units['u2']['divide']
        self.assertEqual(divide['numerator'][0]['namespace'],
                         'http://www.xbrl.org/2003/iso4217')
        self.assertEqual([m['local'] for m in divide['numerator']], ['CNY'])
        self.assertEqual([m['local'] for m in divide['denominator']], ['USD'])
        # u3 lists measures out of order in the source; canonical sorts them.
        self.assertEqual([m['local'] for m in units['u3']['measures']],
                         ['CNY', 'USD'])

    def test_duplicate_node_id_is_a_hard_located_failure(self):
        duplicate = SYNTHETIC.replace(b'id="f2"', b'id="f1"')
        with self.assertRaises(DocumentError) as raised:
            extract(duplicate)
        self.assertIn("duplicate node id 'f1'", str(raised.exception))

    def test_malformed_document_is_a_hard_failure(self):
        with self.assertRaises(DocumentError):
            extract(b'<html><ix:nonFraction name="ex:a"')

    def test_entities_are_refused(self):
        doctype = (b'<!DOCTYPE html [<!ENTITY x "y">]>'
                   b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"></html>')
        with self.assertRaises(DocumentError):
            extract(doctype)

    def test_missing_context_and_unit_references_are_hard_failures(self):
        shell = (
            b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"'
            b' xmlns:ex="urn:synthetic:supplier">'
            b'<ix:nonFraction id="x" name="ex:a" contextRef="missing"'
            b' unitRef="u1" decimals="0">1</ix:nonFraction></html>')
        with self.assertRaises(DocumentError) as raised:
            extract(shell)
        self.assertIn('contextRef', str(raised.exception))

        with_unit = (
            b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"'
            b' xmlns:xbrli="http://www.xbrl.org/2003/instance"'
            b' xmlns:ex="urn:synthetic:supplier">'
            b'<ix:resources><xbrli:context id="c1"><xbrli:entity>'
            b'<xbrli:identifier scheme="urn:s">S</xbrli:identifier></xbrli:entity>'
            b'<xbrli:period><xbrli:instant>2026-11-12</xbrli:instant></xbrli:period>'
            b'</xbrli:context></ix:resources>'
            b'<ix:nonFraction id="x" name="ex:a" contextRef="c1"'
            b' unitRef="gone" decimals="0">1</ix:nonFraction></html>')
        with self.assertRaises(DocumentError) as raised:
            extract(with_unit)
        self.assertIn('unitRef', str(raised.exception))

    def test_unparseable_number_is_isolated_not_guessed(self):
        broken = SYNTHETIC.replace(b'id="f11"', b'id="f11b"', 1).replace(
            b'>7<', b'>not-a-number<', 1)
        result = extract(broken)
        isolated = next(item for item in result['isolated']
                        if item['node_id'] == 'f11b')
        self.assertIn('plain decimal', isolated['reason'])
        self.assertTrue(any(d['code'] == 'invalid-number'
                            for d in result['diagnostics']))


class ReceiverTests(unittest.TestCase):
    def test_receive_persists_full_set_and_raw_blob(self):
        with tempfile.TemporaryDirectory() as state:
            receiver = Receiver(state)
            receipt = receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            self.assertFalse(receipt['already_received'])
            blob = Path(receipt['blob_path'])
            self.assertTrue(blob.exists())
            self.assertEqual(blob.read_bytes(), SYNTHETIC)
            database = sqlite3.connect(Path(state) / 'facts.sqlite3')
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM facts').fetchone()[0], 7)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM isolated_facts').fetchone()[0], 4)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM diagnostics').fetchone()[0], 4)
            receiver.close()

    def test_repeat_receive_adds_no_duplicate_facts(self):
        with tempfile.TemporaryDirectory() as state:
            receiver = Receiver(state)
            first = receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            second = receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            self.assertTrue(second['already_received'])
            self.assertEqual(first['digest'], second['digest'])
            database = sqlite3.connect(Path(state) / 'facts.sqlite3')
            for table in ('documents', 'facts', 'isolated_facts',
                          'diagnostics', 'contexts', 'units'):
                self.assertEqual(database.execute(
                    'SELECT COUNT(*) FROM ' + table).fetchone()[0],
                    1 if table == 'documents' else
                    {'facts': 7, 'isolated_facts': 4, 'diagnostics': 4,
                     'contexts': 3, 'units': 3}[table])
            receiver.close()

    def test_failed_parse_leaves_no_visible_disclosure(self):
        with tempfile.TemporaryDirectory() as state:
            receiver = Receiver(state)
            with self.assertRaises(DocumentError):
                receiver.receive(b'<html><ix:nonFraction name="ex:a"', 'src', 'r')
            database = sqlite3.connect(Path(state) / 'facts.sqlite3')
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(list(Path(state, 'blobs').iterdir()), [])
            # The state directory still accepts a valid revision afterwards.
            receipt = receiver.receive(SYNTHETIC, 'src', 'r')
            self.assertFalse(receipt['already_received'])
            receiver.close()

    def test_conflicting_revision_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as state:
            receiver = Receiver(state)
            receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            with self.assertRaises(DocumentError):
                receiver.receive(SYNTHETIC, 'supplier-A', 'relabelled')
            receiver.close()

    def test_reopen_state_directory_is_stable(self):
        with tempfile.TemporaryDirectory() as state:
            first = Receiver(state)
            first.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            first.close()
            receiver = Receiver(state)  # schema must survive re-opening
            receipt = receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            self.assertTrue(receipt['already_received'])
            receiver.close()

    def test_mid_transaction_crash_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as state:
            calls = {'n': 0}

            def crash_after_first_fact():
                calls['n'] += 1
                if calls['n'] == 2:
                    raise RuntimeError('simulated mid-write crash')

            receiver = Receiver(state, crash_hook=crash_after_first_fact)
            with self.assertRaises(RuntimeError):
                receiver.receive(SYNTHETIC, 'supplier-A', 'rev-1')
            database = sqlite3.connect(Path(state) / 'facts.sqlite3')
            for table in ('documents', 'contexts', 'units', 'facts',
                          'isolated_facts', 'diagnostics'):
                self.assertEqual(database.execute(
                    'SELECT COUNT(*) FROM ' + table).fetchone()[0], 0, table)
            self.assertEqual(list(Path(state, 'blobs').iterdir()), [])
            receiver.close()


class FactsCliTests(unittest.TestCase):
    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'supplierfacts', 'facts', *arguments],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    def test_cli_receive_then_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as state:
            source = str(FIXTURES / 'synthetic-disclosure.xhtml')
            first = self._run(source, '--state', state, '--origin', 'A',
                              '--revision', 'r1')
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = json.loads(first.stdout)
            self.assertEqual(receipt['counts']['facts'], 7)
            second = self._run(source, '--state', state, '--origin', 'A',
                               '--revision', 'r1')
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(json.loads(second.stdout)['already_received'])

    def test_cli_failure_exit_code_and_no_visible_state(self):
        with tempfile.TemporaryDirectory() as state:
            db_dir = Path(state) / 'db'
            bad = Path(state) / 'bad.xhtml'
            bad.write_bytes(b'<html><ix:nonFraction name="ex:a"')
            result = self._run(str(bad), '--state', db_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn('malformed', result.stderr)
            # A failed parse must not expose a document or any facts.
            database = sqlite3.connect(db_dir / 'facts.sqlite3')
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM facts').fetchone()[0], 0)
            self.assertEqual(list(Path(db_dir, 'blobs').iterdir()), [])


class CrashAtomicityTests(unittest.TestCase):
    """A real child process is launched and actually killed (docs/acceptance)."""

    def test_killed_receiver_leaves_no_half_disclosure(self):
        with tempfile.TemporaryDirectory() as state:
            helper = Path(state) / 'crash_receiver.py'
            helper.write_text(
                'import sys\n'
                'sys.path.insert(0, ' + repr(str(PROJECT_ROOT)) + ')\n'
                'from supplierfacts.receive import Receiver\n'
                'payload = open(sys.argv[1], "rb").read()\n'
                'def crash():\n'
                '    raise RuntimeError("simulated mid-write crash")\n'
                'receiver = Receiver(sys.argv[2], crash_hook=crash)\n'
                'receiver.receive(payload, "supplier-A", "rev-1")\n')
            process = subprocess.Popen(
                [sys.executable, helper,
                 str(FIXTURES / 'synthetic-disclosure.xhtml'), state],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT))
            try:
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('simulated mid-write crash', stderr.decode())
            database = sqlite3.connect(Path(state) / 'facts.sqlite3')
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM facts').fetchone()[0], 0)
            # No unpublished temp blob survives.
            self.assertEqual(
                [p.name for p in Path(state, 'blobs').iterdir()
                 if p.name.startswith('tmp')], [])


if __name__ == '__main__':
    unittest.main()
