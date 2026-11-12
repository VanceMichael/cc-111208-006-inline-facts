"""SQLite receiver for one complete document revision of supplier facts.

Invariants:
* the raw page is retained, content-addressed by its SHA-256 digest;
* parsing happens entirely in memory before any visible state is written;
* the full fact set becomes visible in one transaction, so a parse failure or
  a crash never leaves half a disclosure externally visible;
* receiving the same file twice never adds a duplicate fact.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .ixbrl import DocumentError, extract

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS documents (
        digest TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        revision TEXT NOT NULL,
        size INTEGER NOT NULL,
        blob_path TEXT NOT NULL,
        fact_count INTEGER NOT NULL,
        isolated_count INTEGER NOT NULL,
        diagnostic_count INTEGER NOT NULL,
        received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS documents_source_revision "
    "ON documents(source, revision)",
    """CREATE TABLE IF NOT EXISTS contexts (
        document_digest TEXT NOT NULL,
        context_id TEXT NOT NULL,
        canonical TEXT NOT NULL,
        PRIMARY KEY (document_digest, context_id)
    )""",
    """CREATE TABLE IF NOT EXISTS units (
        document_digest TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        canonical TEXT NOT NULL,
        PRIMARY KEY (document_digest, unit_id)
    )""",
    """CREATE TABLE IF NOT EXISTS facts (
        fact_row INTEGER PRIMARY KEY AUTOINCREMENT,
        document_digest TEXT NOT NULL,
        kind TEXT NOT NULL,
        namespace TEXT NOT NULL,
        local_name TEXT NOT NULL,
        context_id TEXT NOT NULL,
        unit_id TEXT,
        status TEXT NOT NULL,
        values_json TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        is_nil INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS facts_document ON facts(document_digest)",
    """CREATE TABLE IF NOT EXISTS isolated_facts (
        document_digest TEXT NOT NULL,
        node_id TEXT,
        locator TEXT NOT NULL,
        kind TEXT NOT NULL,
        namespace TEXT NOT NULL,
        local_name TEXT NOT NULL,
        reason TEXT NOT NULL,
        raw TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS isolated_document ON isolated_facts(document_digest)",
    """CREATE TABLE IF NOT EXISTS diagnostics (
        document_digest TEXT NOT NULL,
        code TEXT NOT NULL,
        locator TEXT NOT NULL,
        node_id TEXT,
        reason TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS diagnostics_document ON diagnostics(document_digest)",
]


class Receiver:
    def __init__(self, state_root, crash_hook=None):
        self.root = Path(state_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(exist_ok=True)
        self.database = sqlite3.connect(self.root / 'facts.sqlite3')
        # Optional test-only callback raised inside the write transaction.
        self.crash_hook = crash_hook
        self.database.execute('PRAGMA foreign_keys = ON')
        for statement in SCHEMA:
            self.database.execute(statement)

    def close(self):
        self.database.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def receive(self, payload, source, revision=None):
        """Receive one document revision atomically. Returns a receipt dict."""
        if not isinstance(payload, (bytes, bytearray)):
            raise DocumentError('document payload must be bytes')
        payload = bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        revision = revision or digest

        existing = self.database.execute(
            'SELECT revision FROM documents WHERE digest = ?',
            (digest,)).fetchone()
        if existing is not None:
            if existing[0] != revision:
                raise DocumentError(
                    'digest ' + digest + ' already received under revision '
                    + existing[0] + ', refusing conflicting revision label '
                    + revision)
            return self._receipt(digest, already_received=True)

        conflicting = self.database.execute(
            'SELECT digest FROM documents WHERE source = ? AND revision = ?',
            (source, revision)).fetchone()
        if conflicting is not None:
            raise DocumentError(
                'source ' + source + ' revision ' + revision
                + ' is already bound to a different document digest')

        # All parsing happens in memory, before any visible state change.
        parsed = extract(payload)

        blob_path = self.blobs / digest
        temporary_name = None
        if not blob_path.exists():
            descriptor, temporary_name = tempfile.mkstemp(dir=self.blobs)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                if temporary_name and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
                raise

        try:
            with self.database:
                self.database.execute(
                    'INSERT INTO documents (digest, source, revision, size, '
                    'blob_path, fact_count, isolated_count, diagnostic_count) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (digest, source, revision, len(payload), str(blob_path),
                     len(parsed['facts']), len(parsed['isolated']),
                     len(parsed['diagnostics'])))
                for context in parsed['contexts']:
                    self.database.execute(
                        'INSERT INTO contexts VALUES (?, ?, ?)',
                        (digest, context['id'],
                         json.dumps(context['canonical'], sort_keys=True,
                                    ensure_ascii=False)))
                for unit in parsed['units']:
                    self.database.execute(
                        'INSERT INTO units VALUES (?, ?, ?)',
                        (digest, unit['id'],
                         json.dumps(unit['canonical'], sort_keys=True,
                                    ensure_ascii=False)))
                for fact in parsed['facts']:
                    if self.crash_hook is not None:
                        self.crash_hook()
                    self.database.execute(
                        'INSERT INTO facts (document_digest, kind, namespace, '
                        'local_name, context_id, unit_id, status, values_json, '
                        'sources_json, is_nil) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (digest, fact['kind'], fact['concept']['namespace'],
                         fact['concept']['local'], fact['context_id'],
                         fact['unit_id'], fact['status'],
                         json.dumps(fact['values'], ensure_ascii=False),
                         json.dumps(fact['sources'], ensure_ascii=False),
                         1 if fact.get('nil') else 0))
                for item in parsed['isolated']:
                    self.database.execute(
                        'INSERT INTO isolated_facts (document_digest, node_id, '
                        'locator, kind, namespace, local_name, reason, raw) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (digest, item['node_id'], item['locator'], item['kind'],
                         item['concept']['namespace'], item['concept']['local'],
                         item.get('reason', 'isolated by continuation diagnostics'),
                         item.get('raw')))
                for diagnostic in parsed['diagnostics']:
                    detail = {key: value for key, value in diagnostic.items()
                              if key not in ('code', 'locator', 'node_id', 'reason')}
                    self.database.execute(
                        'INSERT INTO diagnostics (document_digest, code, locator, '
                        'node_id, reason, detail_json) VALUES (?, ?, ?, ?, ?, ?)',
                        (digest, diagnostic['code'], diagnostic['locator'],
                         diagnostic.get('node_id'), diagnostic['reason'],
                         json.dumps(detail, sort_keys=True, ensure_ascii=False)))
            if temporary_name is not None:
                os.replace(temporary_name, blob_path)
                temporary_name = None
        except BaseException:
            # Roll back visibility and remove an unpublished blob.
            self.database.rollback()
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise

        return self._receipt(digest, already_received=False, parsed=parsed,
                             source=source, revision=revision,
                             size=len(payload), blob_path=str(blob_path))

    def _receipt(self, digest, already_received, parsed=None, source=None,
                 revision=None, size=None, blob_path=None):
        if parsed is None:
            row = self.database.execute(
                'SELECT source, revision, size, blob_path, fact_count, '
                'isolated_count, diagnostic_count FROM documents WHERE digest = ?',
                (digest,)).fetchone()
            source, revision, size, blob_path = row[0], row[1], row[2], row[3]
            counts = {'facts': row[4], 'isolated': row[5], 'diagnostics': row[6]}
            receipt = {'digest': digest, 'source': source, 'revision': revision,
                       'size': size, 'blob_path': blob_path,
                       'already_received': already_received, 'counts': counts}
            return receipt
        counts = {'facts': len(parsed['facts']),
                  'isolated': len(parsed['isolated']),
                  'diagnostics': len(parsed['diagnostics'])}
        return {'digest': digest, 'source': source, 'revision': revision,
                'size': size, 'blob_path': blob_path,
                'already_received': already_received, 'counts': counts,
                'extraction': parsed}
