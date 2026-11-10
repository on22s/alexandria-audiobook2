"""Operator recovery for an interrupted book switch (#1660).

    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] inspect [--no-lock] [--hash]
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] finish
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] quarantine --accept-current-files

inspect     Read-only report: the pending journal, each file's expected and
            current identity, the workspace, and every retained quarantine
            with its receipt state. Takes the book lock unless --no-lock.
            --hash also hashes each file and says whether it holds the
            journal's old or new bytes: timestamps alone can miss a
            same-size rewrite (FAT/exFAT keep 2-second times).
finish      Run the recovery the app runs before every book operation: restore,
            finish, or auto-quarantine when the book's files are consistent.
quarantine  Move the pending switch's workspace and journal aside even though
            the book's files do not match the journal, accepting them as they
            are now. Nothing is deleted; the evidence stays in
            .book-switch-quarantine-*/. If it is interrupted before the
            workspace moves, run it again; afterwards, finish completes it.

Receipts are not authenticated: anyone who can write the data directory can
already change the book's files, so the command trusts that directory.

The data directory defaults to the app's own (ALEXANDRIA_DATA_DIR, else the
repository root), resolved by the same function the app uses. A relative
ALEXANDRIA_DATA_DIR is refused: pass --data-dir instead.

Exit codes: 0 ok, 1 system error (including an uncertain journal write),
2 usage, 3 refused (or a journal inspect cannot read), 4 busy (another book
operation holds the lock), 5 migration required (a well-formed v1 journal).
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_SYSTEM = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BUSY = 4
EXIT_MIGRATION = 5


def get_default_data_dir():
    """-> the app's data directory; ValueError for a relative override,
    which the app and this command would resolve from different folders."""
    from utils import get_runtime_data_dir
    configured = os.environ.get('ALEXANDRIA_DATA_DIR', '').strip()
    if configured and not os.path.isabs(configured):
        raise ValueError('ALEXANDRIA_DATA_DIR is relative; pass --data-dir instead')
    return get_runtime_data_dir(str(Path(__file__).resolve().parent.parent))


def _get_completed_quarantines(report):
    return {entry['name'] for entry in report.get('quarantines', [])
            if entry.get('state') == 'completed'}


MIGRATION_MESSAGE = 'A legacy v1 journal is pending; it needs migrate-legacy.'


def _run_inspect(books, data_dir, wait, no_lock, hashes):
    def inspect(root):
        return (books.inspect_book_recovery_state(root, include_hashes=hashes),
                books.is_book_journal_migration_required(root))
    if no_lock:
        report, legacy = inspect(books.get_admitted_book_root(data_dir))
    else:
        with books.book_operator_locked(data_dir, timeout=wait) as (root, _identity):
            report, legacy = inspect(root)
    if 'retained_error' in report or report['journal'].get('error'):
        return EXIT_REFUSED, 'The book state could not be read fully; see the report.', report
    if not report['journal'].get('present'):
        return EXIT_OK, 'No book switch is pending.', report
    if legacy:
        return EXIT_MIGRATION, MIGRATION_MESSAGE, report
    return EXIT_OK, 'A book switch is pending; see the report.', report


def _run_change(books, data_dir, wait, command, seen):
    with books.book_operator_locked(data_dir, timeout=wait) as (root, identity):
        before = seen['report'] = books.inspect_book_recovery_state(root)
        if not before['journal'].get('present'):
            return EXIT_OK, 'No book switch is pending.', before
        if books.is_book_journal_migration_required(root):
            return EXIT_MIGRATION, MIGRATION_MESSAGE, before
        if command == 'finish':
            books.recover_book_state_locked(
                root, expected_root=root, expected_root_identity=identity)
        else:
            books.quarantine_book_state_locked(root, identity)
        after = books.inspect_book_recovery_state(root)
    added = sorted(_get_completed_quarantines(after) - _get_completed_quarantines(before))
    if added:
        message = f'The pending switch was quarantined in {Path(root) / added[0]}.'
    else:
        message = 'The pending switch was recovered.'
    return EXIT_OK, message, after


def run(args):
    """-> (exit code, message, report or None). A failed change reports the
    state it found before acting."""
    import book_state_transaction as books
    seen = {'report': None}
    try:
        data_dir = args.data_dir or get_default_data_dir()
        if args.command == 'inspect':
            return _run_inspect(books, data_dir, args.wait, args.no_lock, args.hash)
        return _run_change(books, data_dir, args.wait, args.command, seen)
    except TimeoutError:
        return EXIT_BUSY, 'Another book operation holds the lock; retry, or pass --wait.', None
    except books.BookJournalPublicationUncertain as error:
        return EXIT_SYSTEM, f'Journal write uncertain; evidence kept, retry: {error}', seen['report']
    except ValueError as error:
        return EXIT_REFUSED, f'Refused: {error}', seen['report']
    except OSError as error:
        return EXIT_SYSTEM, f'{type(error).__name__}: {error}', seen['report']


def _get_parser():
    parser = argparse.ArgumentParser(
        prog='book_state_recovery.py', description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', help="book data directory (default: the app's)")
    parser.add_argument('--json', action='store_true', help='print one JSON object')
    parser.add_argument('--wait', type=float, default=0.0, metavar='SECONDS',
                        help='how long to wait for the book lock (default: 0)')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='read-only report')
    inspect.add_argument('--no-lock', action='store_true',
                         help='inspect without the lock (a best-effort snapshot)')
    inspect.add_argument('--hash', action='store_true',
                         help="hash each file and compare it with the journal's")
    commands.add_parser('finish', help="run the app's recovery now")
    quarantine = commands.add_parser('quarantine', help='accept the current files')
    quarantine.add_argument('--accept-current-files', action='store_true', required=True,
                            help='required: the book keeps its files as they are now')
    return parser


def main(argv=None):
    args = _get_parser().parse_args(argv)   # usage errors exit 2
    if not math.isfinite(args.wait) or args.wait < 0:
        print('--wait must be a finite, non-negative number of seconds', file=sys.stderr)
        return EXIT_USAGE
    code, message, report = run(args)
    if args.json:
        print(json.dumps({'command': args.command, 'exit_code': code, 'message': message,
                          'report': report}, indent=2, default=str))
    else:
        print(message)
        if report is not None and (args.command == 'inspect' or code != EXIT_OK):
            print(json.dumps(report, indent=2, default=str))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
