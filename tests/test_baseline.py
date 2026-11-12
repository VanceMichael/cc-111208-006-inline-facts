import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from supplierfacts.parser import inspect
from supplierfacts.journal import Journal
from supplierfacts.transport import capture
from scripts.materialize import main as materialize
from scripts.source_server import running


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        materialize()

    def test_capture_journal_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                first = journal.save(b'synthetic', 'source-1')
                second = journal.save(b'synthetic', 'source-1')
                self.assertEqual(first, second)
                self.assertEqual(Path(first['path']).read_bytes(), b'synthetic')
                self.assertEqual(journal.database.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            finally:
                journal.close()

    def test_remote_transport_is_rejected_before_io(self):
        with self.assertRaises(ValueError):
            capture('https://example.invalid/private')

    def test_cli_help(self):
        result = subprocess.run([sys.executable, '-m', 'supplierfacts', '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'inspect', result.stdout)

    def test_local_source_and_request_log(self):
        with running() as server:
            status, headers, payload = capture(server.origin + '/disclosure', 'GET', headers={})
            self.assertEqual(status, 200)
            self.assertGreater(len(payload), 0)
            self.assertEqual(server.requests[0]['method'], 'GET')

    def test_unknown_source_route_is_not_success(self):
        with running() as server:
            self.assertEqual(capture(server.origin + '/not-present')[0], 404)

    def test_context_and_unit_declarations(self):
        result = inspect(Path('fixtures/disclosure.xhtml').read_bytes())
        self.assertEqual(result['contexts'][0]['entity'], 'S1')
        self.assertEqual(result['units'][0]['id'], 'u1')

    def test_prefix_change_is_not_namespace_change(self):
        value = Path('fixtures/disclosure.xhtml').read_bytes()
        self.assertEqual(inspect(value), inspect(value.replace(b'xbrli:', b'instance:').replace(b'xmlns:xbrli=', b'xmlns:instance=')))


if __name__ == '__main__':
    unittest.main()
