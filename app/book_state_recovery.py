"""Operator recovery for an interrupted book switch (#1660).

    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] inspect [--no-lock] [--hash]
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] finish
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] quarantine --accept-current-files
    python book_state_recovery.py [--data-dir DIR] [--json] [--wait SECONDS] migrate-legacy --trust-journal-hashes

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

migrate-legacy
            For a legacy v1 journal (exit 5 elsewhere). v1 journals record
            each file's hash but not its identity: with the flag, a file
            holding the hash its phase recorded is trusted as the app's.
            When every file matches, the workspace and the v1 journal move
            into .book-switch-migration-*/ and the book opens; otherwise
            each mismatched file is listed and nothing moves.

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


MIGRATION_MESSAGE = 'A legacy v1 journal is pending; run migrate-legacy --trust-journal-hashes.'
COMMAND = 'python app/book_state_recovery.py'


def get_recovery_status(report, journal_status):
    """The one decision the command and the app share (Rule 15):
    'clean' | 'pending' | 'migration_required' | 'unreadable'."""
    if ('retained_error' in report or report['journal'].get('error')
            or journal_status == 'invalid'):
        return 'unreadable'
    return {'absent': 'clean', 'legacy': 'migration_required'}.get(journal_status, 'pending')


def get_book_recovery_summary(data_dir, *, wait=0.25):
    """-> what the app shows about a book's recovery state (#1660).

    {'status': 'clean' | 'pending' | 'migration_required' | 'unreadable',
     'phase', 'locked', 'retained': [{'name', 'kind', 'state', 'forced'}],
     'commands': [{'purpose', 'command'}]}, the commands in the order to try
    them, run from the repository root. Changes no book state: it inspects
    under the lock (creating the lock file if absent), or without it when
    another operation holds it ('locked': True).

    The Home page reads the book without recovering it, so a pending
    journal here may still be one finish can restore: finish comes before
    keeping the current files."""
    import book_state_transaction as books
    try:
        with books.book_operator_locked(data_dir, timeout=wait) as (root, _identity):
            report = books.inspect_book_recovery_state(root)
            journal_status = books.get_book_journal_status(root)
        locked = False
    except TimeoutError:
        root = books.get_admitted_book_root(data_dir)
        report = books.inspect_book_recovery_state(root)
        journal_status = books.get_book_journal_status(root)
        locked = True
    journal = report['journal']
    status = get_recovery_status(report, journal_status)
    inspect = {'purpose': 'See what is pending and why', 'command': f'{COMMAND} inspect --hash'}
    commands = {
        'clean': [],
        'unreadable': [inspect],
        'migration_required': [inspect, {
            'purpose': 'Set aside a save from an older version once its files are checked',
            'command': f'{COMMAND} migrate-legacy --trust-journal-hashes'}],
        'pending': [inspect, {
            'purpose': 'Finish or roll back the interrupted save',
            'command': f'{COMMAND} finish'}, {
            'purpose': "Only if finish refuses and the current files are the ones you want, keep them",
            'command': f'{COMMAND} quarantine --accept-current-files'}],
    }[status]
    retained = [{key: entry.get(key) for key in ('name', 'kind', 'state', 'forced')}
                for entry in report.get('quarantines', [])]
    return {'status': status, 'phase': journal.get('phase'), 'locked': locked,
            'retained': retained, 'commands': commands}


def _run_inspect(books, data_dir, wait, no_lock, hashes):
    def inspect(root):
        return (books.inspect_book_recovery_state(root, include_hashes=hashes),
                books.get_book_journal_status(root))
    if no_lock:
        report, journal_status = inspect(books.get_admitted_book_root(data_dir))
    else:
        with books.book_operator_locked(data_dir, timeout=wait) as (root, _identity):
            report, journal_status = inspect(root)
    return {
        'unreadable': (EXIT_REFUSED, 'The book state could not be read, or its journal is not '
                                     'valid; see the report.'),
        'clean': (EXIT_OK, 'No book switch is pending.'),
        'migration_required': (EXIT_MIGRATION, MIGRATION_MESSAGE),
        'pending': (EXIT_OK, 'A book switch is pending; see the report.'),
    }[get_recovery_status(report, journal_status)] + (report,)


def _run_change(books, data_dir, wait, command, seen):
    with books.book_operator_locked(data_dir, timeout=wait) as (root, identity):
        before = seen['report'] = books.inspect_book_recovery_state(root)
        if not before['journal'].get('present'):
            return EXIT_OK, 'No book switch is pending.', before
        journal_status = books.get_book_journal_status(root)
        legacy = journal_status == 'legacy'
        if command == 'migrate-legacy':
            if journal_status == 'invalid':
                return (EXIT_REFUSED, 'The pending journal is not valid, so no command can act on '
                                      'it safely; run inspect.', before)
            if not legacy:
                return (EXIT_REFUSED, 'The pending journal is not a legacy v1 journal; run finish.',
                        before)
            books.migrate_legacy_book_state_locked(root, identity)
        elif legacy:
            return EXIT_MIGRATION, MIGRATION_MESSAGE, before
        elif command == 'finish':
            books.recover_book_state_locked(
                root, expected_root=root, expected_root_identity=identity)
        else:
            books.quarantine_book_state_locked(root, identity)
        after = books.inspect_book_recovery_state(root)
    added = sorted(_get_completed_quarantines(after) - _get_completed_quarantines(before))
    if added and command == 'migrate-legacy':
        message = f'The legacy journal and its workspace were moved into {Path(root) / added[0]}.'
    elif added:
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
    migrate = commands.add_parser('migrate-legacy', help='move a v1 journal aside by its hashes')
    migrate.add_argument('--trust-journal-hashes', action='store_true', required=True,
                         help="required: a file holding the journal's hash is trusted as the app's")
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
