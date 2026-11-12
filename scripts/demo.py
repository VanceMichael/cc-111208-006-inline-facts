import json
from pathlib import Path
import subprocess
import sys
import tempfile
from .materialize import main as materialize
from .source_server import running


def main():
    materialize()
    subprocess.run([sys.executable, '-m', 'supplierfacts', 'inspect', 'fixtures/disclosure.xhtml'], check=True)
    with running() as server, tempfile.TemporaryDirectory() as state:
        subprocess.run([sys.executable, '-m', 'supplierfacts', 'capture', server.origin + '/disclosure', '--method', 'GET', '--state', state, *[]], check=True)
        print(json.dumps({'requests': server.requests}, ensure_ascii=True))


if __name__ == '__main__':
    main()
