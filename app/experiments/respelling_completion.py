"""Read-only completeness checks for cached respelling experiment rows."""
import argparse
import json
import sys


def get_respelling_completion_error(path, required_count):
    if type(required_count) is not int or required_count < 0:
        return 'requested term count must be a nonnegative integer'
    _, error = get_respelling_completion_state(path, required_count)
    return error


def get_respelling_completion_state(path, required_count):
    try:
        with open(path, encoding='utf-8') as handle:
            document = json.load(handle)
    except FileNotFoundError as error:
        return 1, 'cannot read respelling result: ' + str(error)
    except (OSError, ValueError) as error:
        return 2, 'cannot read respelling result: ' + str(error)
    if not isinstance(document, dict):
        return 2, 'respelling result must be an object'
    error = get_respelling_document_completion_error(document, required_count)
    return (1 if error else 0), error


def get_respelling_document_completion_error(document, required_count):
    if type(required_count) is not int or required_count < 0:
        return 'requested term count must be a nonnegative integer'
    if not isinstance(document, dict):
        return 'respelling result must be an object'
    if document.get('status') not in (None, 'complete'):
        return 'respelling result is not complete'
    count = document.get('candidates_considered')
    rows = document.get('results')
    if type(count) is not int or count < 1 or not isinstance(rows, list):
        return 'respelling result requires a positive count and result rows'
    if len(rows) != count:
        return 'result row count differs from candidates considered'
    if required_count and count != required_count:
        return 'result does not cover the requested term count'
    terms = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('term'), str) or not row['term'].strip():
            return 'each result requires a nonempty term'
        terms.append(row['term'])
    if len(set(terms)) != count:
        return 'respelling result contains duplicate terms'
    identity = document.get('run_identity')
    if identity is not None:
        if not isinstance(identity, dict) or type(identity.get('limit')) is not int or identity['limit'] != required_count:
            return 'result run identity differs from requested limit'
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('required_count', type=int)
    parser.add_argument('--refuse-unreadable', action='store_true')
    args = parser.parse_args()
    state, error = get_respelling_completion_state(args.path, args.required_count)
    if error:
        print('INCOMPLETE: ' + error, file=sys.stderr)
        return state if args.refuse_unreadable else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
