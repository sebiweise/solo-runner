import pytest

from solo_runner.miniyaml import YAMLError, loads

EXAMPLE = """\
---
# solo-runner settings per repo
repos:
  me/heavy-build:          # one job at a time, on the big labels
    max_concurrent: 1
    labels: [solo, "big disk", 'it''s']
  "*-sandbox":
    labels:
      - sandbox
      - solo # trailing comment
  docs:
    max_concurrent: 2
  empty:
  url: "http://x/#not-a-comment"
  plain: a#b
  flags: [true, null, ~, -3, "007", +4]
"""

VALID = [
    EXAMPLE,
    "",
    "# only a comment\n",
    "a: 1\nb:\n- x\n- y\n",
    "a: []\nb: {}\n",
    "key with spaces: value with spaces\n",
    "a:\n  b:\n    c: d\n  e: f\n",
    'esc: "tab\\tnew\\nline \\u00e9"\n',
]


def test_example():
    assert loads(EXAMPLE) == {
        "repos": {
            "me/heavy-build": {"max_concurrent": 1, "labels": ["solo", "big disk", "it's"]},
            "*-sandbox": {"labels": ["sandbox", "solo"]},
            "docs": {"max_concurrent": 2},
            "empty": None,
            "url": "http://x/#not-a-comment",
            "plain": "a#b",
            "flags": [True, None, None, -3, "007", 4],
        }
    }


@pytest.mark.parametrize("text", VALID)
def test_same_result_as_pyyaml(text):
    # Whatever the subset accepts must mean the same in a real YAML parser.
    yaml = pytest.importorskip("yaml")
    assert loads(text) == yaml.safe_load(text)


@pytest.mark.parametrize(
    "text, line, message",
    [
        ("a:\n\tb: 1\n", 2, "tabs"),
        ("a: 1\n  b: 2\n", 2, "indentation"),
        ("a: 1\na: 2\n", 2, "duplicate"),
        ("a: &anchor 1\n", 1, "quote"),
        ("a: *alias\n", 1, "quote"),
        ("*-sandbox:\n  labels: [x]\n", 1, "quote"),
        ("a: !tag x\n", 1, "quote"),
        ("a: |\n  text\n", 1, "quote"),
        ("a: {b: 1}\n", 1, "quote"),
        ("a: [x, [y]]\n", 1, "nested"),
        ("a: [x,\n  y]\n", 1, "same line"),
        ("a:\n  - b: 1\n", 2, "single values"),
        ("a:\n  - - x\n", 2, "single values"),
        ('a: "open\n', 1, "unterminated"),
        ("a: 1\n---\nb: 2\n", 2, "one document"),
        ("just a string\n", 1, "key: value"),
        ("a: b: c\n", 1, "quote"),
        ("  a: 1\n", 1, "indented"),
        ("a: [x, , y]\n", 1, "empty"),
        ("a:\n  - x\n  b: 1\n", 3, "indentation"),
    ],
)
def test_rejected(text, line, message):
    with pytest.raises(YAMLError, match=message) as info:
        loads(text)
    assert info.value.line == line
