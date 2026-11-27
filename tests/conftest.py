"""The suite prices from the bundled sample book (demo_data/pricebook.yaml) as if it were an owner's own, so the send and
invoice paths run. tests/test_sample_book.py turns the sample mark back on to test the refusal itself."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import docket.pricebook as _pricebook  # noqa: E402

_pricebook.SAMPLE_MARK = "__sample_mark_off_in_tests__"
_pricebook.SAMPLE_BUSINESS = "__sample_business_off_in_tests__"
