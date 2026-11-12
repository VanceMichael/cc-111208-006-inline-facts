import json
from pathlib import Path
import subprocess
import sys
import tempfile
from .materialize import main as materialize
from .source_server import running


def run(*arguments):
    completed = subprocess.run([sys.executable, '-m', 'supplierfacts', *arguments],
                               check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def main():
    materialize()
    # Baseline inspection is retained as a structural summary tool.
    subprocess.run([sys.executable, '-m', 'supplierfacts', 'inspect',
                    'fixtures/disclosure.xhtml'], check=True)

    with running() as server, tempfile.TemporaryDirectory() as state:
        # 1) Capture the raw disclosed page over loopback HTTP and retain it.
        captured = run('capture', server.origin + '/disclosure',
                       '--method', 'GET', '--state', state)
        raw_path = Path(captured['path'])

        # 2) Receive one document revision as a complete, traceable fact set.
        receipt = run('facts', str(raw_path), '--state',
                      str(Path(state) / 'facts'),
                      '--origin', 'loopback/disclosure', '--revision', 'rev-1')
        summary = {
            'digest': receipt['digest'],
            'already_received': receipt['already_received'],
            'counts': receipt['counts'],
            'conflict_groups': [
                {'concept': fact['concept']['qualified'],
                 'values': fact['values'],
                 'source_nodes': [source['node_id'] for source in fact['sources']]}
                for fact in receipt['extraction']['facts']
                if fact['status'] == 'conflict'],
            'isolated_nodes': [item['node_id']
                               for item in receipt['extraction']['isolated']],
            'diagnostic_codes': [diagnostic['code']
                                 for diagnostic in
                                 receipt['extraction']['diagnostics']],
        }

        # 3) Re-receiving the identical revision must add no duplicate facts.
        repeated = run('facts', str(raw_path), '--state',
                       str(Path(state) / 'facts'),
                       '--origin', 'loopback/disclosure', '--revision', 'rev-1')

        # 4) A richer synthetic revision exercises conflicts and isolated items.
        rich = run('facts', 'fixtures/synthetic-disclosure.xhtml', '--state',
                   str(Path(state) / 'facts'),
                   '--origin', 'loopback/synthetic', '--revision', 'rev-1')
        rich_summary = {
            'counts': rich['counts'],
            'conflict_groups': [
                {'concept': fact['concept']['qualified'],
                 'values': fact['values'],
                 'source_nodes': [source['node_id']
                                  for source in fact['sources']]}
                for fact in rich['extraction']['facts']
                if fact['status'] == 'conflict'],
            'isolated_nodes': [item['node_id']
                               for item in rich['extraction']['isolated']],
            'diagnostic_codes': [diagnostic['code']
                                 for diagnostic in
                                 rich['extraction']['diagnostics']],
        }

        print(json.dumps({'first_receipt': summary,
                          'repeat_already_received': repeated['already_received'],
                          'repeat_counts': repeated['counts'],
                          'synthetic_revision': rich_summary,
                          'requests': server.requests},
                         ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
