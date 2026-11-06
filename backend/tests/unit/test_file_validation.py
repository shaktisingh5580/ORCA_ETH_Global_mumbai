"""Purpose: reject invalid or active-content upload formats before file publication.
Behavior: cover finite JSON, CSV shape, formula-bearing headers and bounded safe numerical data.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

import pytest

from orca.application.files import validate_format


@pytest.mark.parametrize(
    "body,media",
    [
        (b'{"value":NaN}', "application/json"),
        (b'{"value":1e999}', "application/json"),
        (b"42", "application/json"),
        (b"\xff\xfe", "text/csv"),
        (b'=HYPERLINK("https://invalid"),x\n1,2\n', "text/csv"),
        (b"x,y\n1,=SUM(1)\n", "text/csv"),
        (b"x,x\n1,2\n", "text/csv"),
        (b"x,y\n1\n", "text/csv"),
    ],
)
def test_invalid_upload_content_is_rejected(body, media):
    with pytest.raises((ValueError, UnicodeError)):
        validate_format(body, media)


def test_numerical_csv_and_structured_json_are_accepted():
    validate_format(b"date,sst\n2025-01-01,-1.5\n", "text/csv")
    validate_format(b'{"values":[26.5,null,-1.5]}', "application/json")
