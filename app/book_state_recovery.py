"""Operator recovery for an interrupted book switch (#1660).

    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] inspect [--no-lock]
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] finish
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] quarantine --accept-current-files

inspect     Read-only report: the pending journal, each file's expected and
            current identity, the workspace, and every retained quarantine
            with its receipt state. Takes the book lock unless --no-lock.
finish      Run the recovery the app runs before every book operation: restore,
            finish, or auto-quarantine when the book's files are consistent.
quarantine  Move the pending switch's workspace and journal aside even though
            the book's files do not match the journal, accepting them as they
            are now. Nothing is deleted; the evidence stays in
            .book-switch-quarantine-*/.

The data directory defaults to the app's own (ALEXANDRIA_DATA_DIR, else the
repository root), resolved by the same function the app uses.

Exit codes: 0 ok, 1 system error, 2 usage, 3 refused, 4 busy (another book
operation holds the lock), 5 migration required (a legacy v1 journal).
"""
import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_SYSTEM = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BUSY = 4
EXIT_MIGRATION = 5


def get_default_data_dir():
    from utils import get_runtime_data_dir
    return get_runtime_data_dir(str(Path(__file__).resolve().parent.parent))


def _get_completed_quarantines(report):
    return {entry['name'] for entry in report.get('quarantines', [])
            if entry.get('state') == 'completed'}


def _is_legacy(report):
    return report['journal'].get('version') == 1


def _run_inspect(books, data_dir, wait, no_lock):
    if no_lock:
        report = books.inspect_book_recovery_state(books.get_admitted_book_root(data_dir))
    else:
        with books.book_operator_locked(data_dir, timeout=wait) as (root, _identity):
            report = books.inspect_book_recovery_state(root)
    if not report['journal'].get('present'):
        return EXIT_OK, 'No book switch is pending.', report
    if _is_legacy(report):
        return EXIT_MIGRATION, 'A legacy v1 journal is pending; it needs migrate-legacy.', report
    return EXIT_OK, 'A book switch is pending; see the report.', report


def _run_change(books, data_dir, wait, command):
    with books.book_operator_locked(data_dir, timeout=wait) as (root, identity):
        before = books.inspect_book_recovery_state(root)
        if not before['journal'].get('present'):
            return EXIT_OK, 'No book switch is pending.', before
        if _is_legacy(before):
            return EXIT_MIGRATION, 'A legacy v1 journal is pending; it needs migrate-legacy.', before
        if command == 'finish':
            books.recover_book_state_locked(
                root, expected_root=root, expected_root_identity=identity)
        else:
            books.quarantine_book_state_locked(root, identity)
        after = books.inspect_book_recovery_state(root)
    added = sorted(_get_completed_quarantines(after) - _get_completed_quarantines(before))
    if added:
        message = f'The pending switch was quarantined in {added[0]}.'
    else:
        message = 'The pending switch was recovered.'
    return EXIT_OK, message, after


def run(args):
    """-> (exit code, message, report or None)."""
    import book_state_transaction as books
    data_dir = args.data_dir or get_default_data_dir()
    try:
        if args.command == 'inspect':
            return _run_inspect(books, data_dir, args.wait, args.no_lock)
        return _run_change(books, data_dir, args.wait, args.command)
    except TimeoutError:
        return EXIT_BUSY, 'Another book operation holds the lock; retry, or pass --wait.', None
    except ValueError as error:
        return EXIT_REFUSED, f'Refused: {error}', None
    except OSError as error:
        return EXIT_SYSTEM, f'{type(error).__name__}: {error}', None


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
    commands.add_parser('finish', help="run the app's recovery now")
    quarantine = commands.add_parser('quarantine', help='accept the current files')
    quarantine.add_argument('--accept-current-files', action='store_true', required=True,
                            help='required: the book keeps its files as they are now')
    return parser


def main(argv=None):
    args = _get_parser().parse_args(argv)   # usage errors exit 2
    if args.wait < 0:
        print('--wait must not be negative', file=sys.stderr)
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
