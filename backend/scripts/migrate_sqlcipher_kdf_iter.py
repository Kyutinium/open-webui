#!/usr/bin/env python3
"""Re-encrypt an SQLCipher Open WebUI database with a new kdf_iter value.

Why
---
SQLCipher 4.x derives the AES key from ``DATABASE_PASSWORD`` via PBKDF2 with
a high iteration count (default 256 000) — ~50–100 ms per ``PRAGMA key``
call.  Because the async engine uses ``NullPool`` for SQLCipher (sqlcipher3
is not thread-safe enough for connection pooling), every async query opens a
new connection and re-derives the key.  A single page load issuing 20
queries can pay 1–2 seconds of pure KDF overhead.

When ``DATABASE_PASSWORD`` is a high-entropy random key (e.g.
``openssl rand -hex 32``), 256 000 iterations buys no extra brute-force
resistance — the entropy of the password already exceeds anything PBKDF2
can stretch.  Lowering ``kdf_iter`` to e.g. 4000 cuts connection-open cost
~64x with no practical security impact for that scenario.

Lowering ``kdf_iter`` requires re-encrypting the database file with the new
value, which is what this script does.

Usage
-----
1. Stop the service (or at minimum quiesce all writers).
2. Back up the existing DB file out of band — this script also makes its
   own backup but a separate copy is wise.
3. Run::

       DATABASE_URL=sqlite+sqlcipher:///path/to/webui.db \
       DATABASE_PASSWORD=<your password> \
       python3 backend/scripts/migrate_sqlcipher_kdf_iter.py --new-kdf-iter 4000

   The script will:
       - open the DB at the current default iteration count
       - run a sanity ``SELECT count(*) FROM user`` to confirm the key works
       - export every page into ``<dbfile>.new`` keyed at the new iter count
       - swap ``<dbfile>`` <-> ``<dbfile>.new`` (old kept as ``<dbfile>.bak``)

4. Add ``DATABASE_SQLCIPHER_KDF_ITER=4000`` (or whichever value you used) to
   the service environment.
5. Start the service.  ``create_sqlcipher_connection`` will set
   ``PRAGMA kdf_iter`` before ``PRAGMA key`` so derivations match.

Re-running the script with the wrong --current-kdf-iter raises before
anything is written, so accidentally double-running is safe.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split('Usage')[0].strip())
    p.add_argument(
        '--db-path',
        default=None,
        help='Path to the SQLCipher database file. Defaults to the path '
        'parsed from DATABASE_URL (sqlite+sqlcipher:///...).',
    )
    p.add_argument(
        '--password',
        default=None,
        help='SQLCipher passphrase. Defaults to $DATABASE_PASSWORD.',
    )
    p.add_argument(
        '--current-kdf-iter',
        type=int,
        default=None,
        help='kdf_iter currently in effect (i.e. the value used to encrypt '
        'the existing DB). Default lets SQLCipher use its built-in default.',
    )
    p.add_argument(
        '--new-kdf-iter',
        type=int,
        required=True,
        help='Target kdf_iter after migration. Recommended: 4000 for '
        'random 32-byte passwords; 1000 only for ≥ 256-bit keys.',
    )
    p.add_argument(
        '--dry-run',
        action='store_true',
        help='Run the entire migration into a side-by-side .new file but '
        'skip the swap. Useful to time the operation safely first.',
    )
    return p.parse_args()


def _resolve_db_path(arg_path: str | None) -> Path:
    if arg_path:
        return Path(arg_path).expanduser().resolve()

    url = os.environ.get('DATABASE_URL', '')
    prefix = 'sqlite+sqlcipher://'
    if not url.startswith(prefix):
        sys.exit(
            'ERROR: --db-path not given and DATABASE_URL is not a '
            f'sqlite+sqlcipher:// URL (got: {url!r})'
        )
    raw = url[len(prefix):]
    # Both ``sqlite+sqlcipher:///abs`` (3 slashes) and
    # ``sqlite+sqlcipher://relative`` are accepted; strip up to one leading '/'.
    if raw.startswith('/'):
        raw = raw[1:]
    return Path('/' + raw if not raw.startswith('/') else raw).resolve()


def _resolve_password(arg_password: str | None) -> str:
    pw = arg_password or os.environ.get('DATABASE_PASSWORD', '')
    if not pw:
        sys.exit('ERROR: DATABASE_PASSWORD is empty (or --password not given)')
    return pw


def _ensure_sqlcipher3() -> None:
    try:
        import sqlcipher3  # noqa: F401
    except ImportError:
        sys.exit(
            'ERROR: sqlcipher3 is not installed in this Python environment. '
            'Install it (or run this script inside the open-webui container).'
        )


def _open_existing(
    db_path: Path,
    password: str,
    current_kdf_iter: int | None,
):
    import sqlcipher3

    conn = sqlcipher3.connect(str(db_path), check_same_thread=False)
    if current_kdf_iter is not None:
        conn.execute(f'PRAGMA kdf_iter = {int(current_kdf_iter)}')
    conn.execute(f"PRAGMA key = '{password}'")
    # Run a no-op query that forces SQLCipher to derive and validate the key.
    conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
    return conn


def _verify_payload(conn) -> None:
    """Sanity: confirm the file actually contains an Open WebUI schema."""
    expected = {'user', 'chat'}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    missing = expected - names
    if missing:
        sys.exit(
            f'ERROR: Database does not look like an Open WebUI DB '
            f'(missing tables: {sorted(missing)}; tables present: {sorted(names)})'
        )


def _attach_new_and_export(
    conn,
    new_path: Path,
    password: str,
    new_kdf_iter: int,
) -> None:
    # ``ATTACH ... KEY`` derives a key for the attached DB using the *current*
    # default kdf_iter.  We then bump kdf_iter on the attached schema before
    # ``sqlcipher_export`` runs so every page in the new file is encrypted at
    # the target iteration count.
    conn.execute(
        f"ATTACH DATABASE '{new_path}' AS new KEY '{password}'"
    )
    conn.execute(f'PRAGMA new.kdf_iter = {int(new_kdf_iter)}')
    conn.execute("SELECT sqlcipher_export('new')")
    conn.execute('DETACH DATABASE new')


def _swap_files(db_path: Path, new_path: Path) -> Path:
    """Atomically replace db_path with new_path, keeping a backup."""
    backup = db_path.with_suffix(db_path.suffix + '.bak')
    if backup.exists():
        # Don't silently overwrite a previous backup — refuse so the user
        # makes an explicit decision.
        sys.exit(
            f'ERROR: backup file {backup} already exists. Move or delete '
            f'it before re-running the migration.'
        )
    db_path.rename(backup)
    new_path.rename(db_path)
    return backup


def main() -> None:
    args = _parse_args()
    _ensure_sqlcipher3()

    db_path = _resolve_db_path(args.db_path)
    password = _resolve_password(args.password)

    if not db_path.exists():
        sys.exit(f'ERROR: db file not found: {db_path}')

    new_path = db_path.with_suffix(db_path.suffix + '.new')
    if new_path.exists():
        sys.exit(
            f'ERROR: stale {new_path} already exists. Delete it before '
            f're-running.'
        )

    print(f'[migrate] db_path = {db_path}')
    print(f'[migrate] new_path = {new_path}')
    print(f'[migrate] current_kdf_iter = {args.current_kdf_iter or "(SQLCipher default)"}')
    print(f'[migrate] new_kdf_iter = {args.new_kdf_iter}')
    print(f'[migrate] dry_run = {args.dry_run}')
    print()

    print('[migrate] opening existing DB ...')
    started = time.monotonic()
    conn = _open_existing(db_path, password, args.current_kdf_iter)
    open_secs = time.monotonic() - started
    print(f'[migrate] open + key derive took {open_secs:.3f}s')

    _verify_payload(conn)

    print('[migrate] exporting to new file ...')
    started = time.monotonic()
    try:
        _attach_new_and_export(conn, new_path, password, args.new_kdf_iter)
    finally:
        conn.close()
    export_secs = time.monotonic() - started
    print(f'[migrate] export complete in {export_secs:.3f}s')

    print('[migrate] verifying new file opens at new kdf_iter ...')
    verify_started = time.monotonic()
    new_conn = _open_existing(new_path, password, args.new_kdf_iter)
    _verify_payload(new_conn)
    new_conn.close()
    verify_secs = time.monotonic() - verify_started
    print(f'[migrate] new file opens cleanly in {verify_secs:.3f}s')
    print(
        f'[migrate] connection-open speed-up: '
        f'{open_secs / max(verify_secs, 1e-6):.1f}x'
    )

    if args.dry_run:
        print(
            '[migrate] dry-run requested: leaving new file in place '
            f'({new_path}). Original DB untouched.'
        )
        return

    print('[migrate] swapping files ...')
    backup = _swap_files(db_path, new_path)
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(
        f'[migrate] done. {db_path} now uses kdf_iter={args.new_kdf_iter} '
        f'({size_mb:.1f} MB). Backup: {backup}'
    )
    print()
    print(
        '[migrate] NEXT: set DATABASE_SQLCIPHER_KDF_ITER='
        f'{args.new_kdf_iter} in the service environment and restart.'
    )


if __name__ == '__main__':
    main()
