import pytest

from g2g.analysis.parser import parse_decision


def wire(explanation="Grounded evidence", score="42", rec="allow"):
    return f"<decision><explanation>{explanation}</explanation><recommendation>{rec}</recommendation><suspicion_score>{score}</suspicion_score></decision>"


@pytest.mark.parametrize(
    "text",
    ["R&D", "x < y", "?a=1&b=2", "<alice@company.com>", "List<String>", "<script>alert(1)</script>", "]]>"],
)
def test_plain_text_body(text):
    assert parse_decision(wire(text)).status == "valid"


@pytest.mark.parametrize("score", ["1.0", "+1", "-1", "1e2", "NaN", "Infinity", "101", "١", ""])
def test_bad_score(score):
    assert parse_decision(wire(score=score)).status == "invalid"


@pytest.mark.parametrize(
    "body", ["< Decision>", "</ explanation>", '<recommendation x="a">', "<SUSPICION_SCORE>", "<decision/>"]
)
def test_reserved_tags(body):
    assert parse_decision(wire(body)).status == "invalid"


def test_outer_and_duplicate():
    assert parse_decision(wire() + wire()).status == "invalid"
    assert parse_decision(wire() + " prose").status == "invalid"
    assert parse_decision("```xml\n" + wire() + "\n```").status == "invalid"
    assert parse_decision(wire().replace("<decision>", '<decision x="1">')).status == "invalid"
    assert parse_decision(wire("I refuse the unsupported conclusion.")).status == "valid"
    assert parse_decision(wire(), "refused").status == "refused"
    assert parse_decision(wire("x" * 70000)).reason == "output_size"
