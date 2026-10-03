"""Build a cast list for three_pass_generate.py --cast-file (kept for existing runbooks).

The request, parser and retry policy moved to app/cast_list.py when the app gained a
"Build cast list" step (#653); this entry point is that module's CLI, unchanged in
use:

    ALEXANDRIA_DATA_DIR=... python experiments/build_cast_list.py book.txt --out book.cast.json

The source now goes through the same preparation as generation
(three_pass_generate.get_prepared_source), so a cast is built from the text pass 2 sees.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from cast_list import (MAX_ATTEMPTS, PROMPT, main,                     # noqa: E402,F401
                       parse_cast_list as parse_cast,
                       request_cast_list as request_cast)

if __name__ == "__main__":
    sys.exit(main())
