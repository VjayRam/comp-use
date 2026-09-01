from comp_use.tokenizer import SensitiveValueTokenizer


def test_tokenizes_and_detokenizes_the_shapes_already_in_use():
    tok = SensitiveValueTokenizer([r"\bACC-\d+\b", r"\$[\d,]+\.\d{2}"])
    tree = 'row: account "ACC-000123" balance "$1,204.50"'

    tokenized = tok.tokenize(tree)

    assert "ACC-000123" not in tokenized
    assert "$1,204.50" not in tokenized
    assert tok.detokenize(tokenized) == tree


def test_expanding_to_a_new_field_type_is_a_pattern_addition_not_a_code_change():
    # SensitiveValueTokenizer takes an arbitrary list of regexes - there is no
    # "account number" special-casing anywhere in the class. Covering a new
    # sensitive field (SSN, email, phone, whatever a future app renders) means
    # adding one pattern to Settings.redaction_patterns; both this tokenizer
    # AND Guardrail.redact() (output redaction) read that same list, so a new
    # field type is covered on both paths from a single line of config.
    patterns = [
        r"\bACC-\d+\b",              # existing: account number
        r"\b\d{3}-\d{2}-\d{4}\b",    # new: SSN shape
        r"[\w.+-]+@[\w-]+\.[\w.-]+", # new: email
    ]
    tok = SensitiveValueTokenizer(patterns)
    tree = 'account "ACC-000123" ssn "123-45-6789" contact "jane@example.com"'

    tokenized = tok.tokenize(tree)

    for raw in ("ACC-000123", "123-45-6789", "jane@example.com"):
        assert raw not in tokenized
    assert tok.detokenize(tokenized) == tree


def test_same_value_maps_to_the_same_token_across_multiple_calls():
    tok = SensitiveValueTokenizer([r"\bACC-\d+\b"])
    first = tok.tokenize("look at ACC-000123")
    second = tok.tokenize("still ACC-000123, now with ACC-000456 too")

    first_token = first.split("look at ")[1]
    assert first_token in second
    assert second.count(first_token) == 1  # only the ACC-000123 occurrence, not ACC-000456


def test_detokenize_value_recurses_through_dicts_and_lists():
    tok = SensitiveValueTokenizer([r"\bACC-\d+\b"])
    token = tok.tokenize("ACC-000123")

    restored = tok.detokenize_value(
        {"strategy": "text", "value": {"name": token, "tags": [token, "unrelated"]}}
    )

    assert restored == {
        "strategy": "text",
        "value": {"name": "ACC-000123", "tags": ["ACC-000123", "unrelated"]},
    }
