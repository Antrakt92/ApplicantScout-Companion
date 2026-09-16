"""Manual column ordering preserves application groups and missing evidence."""

from dataclasses import asdict

import pytest

from applicant_scout.overlay_rows import sort_rows_by_value
from applicant_scout.state import Applicant


def _row(applicant_id: str, name: str = "Player") -> Applicant:
    return Applicant(applicant_id, name, "MAGE", 62, 700, 2500, "DAMAGER")


@pytest.mark.parametrize("descending", [False, True])
def test_numeric_values_sort_numerically_with_missing_last(descending):
    rows = [_row(str(index)) for index in range(5)]
    values = {"0": 10, "1": None, "2": 100, "3": 0, "4": -5}

    result = sort_rows_by_value(
        rows, value_for=lambda row: values[row.applicant_id],
        descending=descending, grouped=False,
    )

    assert [row.applicant_id for row in result] == (
        ["2", "0", "3", "4", "1"] if descending else ["4", "3", "0", "2", "1"]
    )


@pytest.mark.parametrize("descending", [False, True])
def test_text_values_casefold_and_preserve_equal_value_order(descending):
    rows = [_row("1", "beta"), _row("2", "Straße"), _row("3", "ALPHA"), _row("4", "STRASSE")]

    result = sort_rows_by_value(
        rows, value_for=lambda row: row.name, descending=descending, grouped=False,
    )

    assert [row.applicant_id for row in result] == (
        ["2", "4", "1", "3"] if descending else ["3", "1", "2", "4"]
    )


@pytest.mark.parametrize("descending", [False, True])
def test_nonfinite_boolean_and_missing_values_stay_last(descending):
    values = [None, float("nan"), float("inf"), -float("inf"), True, False, 10, 100]
    rows = [_row(str(index)) for index in range(len(values))]

    result = sort_rows_by_value(
        rows, value_for=lambda row: values[int(row.applicant_id)],
        descending=descending, grouped=False,
    )

    assert [row.applicant_id for row in result] == (
        (["7", "6"] if descending else ["6", "7"]) + ["0", "1", "2", "3", "4", "5"]
    )


@pytest.mark.parametrize("descending", [False, True])
def test_groups_use_weakest_member_keep_original_member_order_and_sink_partial_data(descending):
    rows = [_row(value) for value in ["a:2", "b:1", "c:1", "a:1", "c:2", "d:1"]]
    values = {"a:2": 100, "a:1": 10, "b:1": 50, "c:1": 99, "c:2": None, "d:1": None}

    result = sort_rows_by_value(
        rows, value_for=lambda row: values[row.applicant_id],
        descending=descending, grouped=True,
    )

    assert [row.applicant_id for row in result] == (
        (["b:1", "a:2", "a:1"] if descending else ["a:2", "a:1", "b:1"])
        + ["c:1", "c:2", "d:1"]
    )


@pytest.mark.parametrize("descending", [False, True])
def test_text_groups_use_lowest_casefolded_name(descending):
    rows = [_row("a:2", "Zulu"), _row("b:1", "Beta"), _row("a:1", "alpha")]

    result = sort_rows_by_value(
        rows, value_for=lambda row: row.name, descending=descending, grouped=True,
    )

    assert [row.applicant_id for row in result] == (
        ["b:1", "a:2", "a:1"] if descending else ["a:2", "a:1", "b:1"]
    )


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
def test_ties_preserve_incoming_order_without_mutating_rows(descending, grouped):
    rows = [_row("b:2"), _row("a:1"), _row("b:1")]
    before = [asdict(row) for row in rows]

    result = sort_rows_by_value(
        iter(rows), value_for=lambda _row: 10, descending=descending, grouped=grouped,
    )

    expected = [rows[0], rows[2], rows[1]] if grouped else rows
    assert all(actual is original for actual, original in zip(result, expected, strict=True))
    assert [asdict(row) for row in rows] == before
    assert [row.applicant_id for row in rows] == ["b:2", "a:1", "b:1"]


def test_party_rows_do_not_group_shared_application_prefixes():
    rows = [_row("a:1"), _row("a:2"), _row("b:1")]
    values = {"a:1": 10, "a:2": 100, "b:1": 50}

    result = sort_rows_by_value(
        rows, value_for=lambda row: values[row.applicant_id], descending=True, grouped=False,
    )

    assert [row.applicant_id for row in result] == ["a:2", "b:1", "a:1"]


@pytest.mark.parametrize("descending", [False, True])
def test_mixed_value_types_are_deterministic_and_mixed_groups_are_missing(descending):
    rows = [_row(value) for value in ["a:1", "b:1", "c:1", "a:2"]]
    values = {"a:1": 10, "a:2": "alpha", "b:1": 100, "c:1": "beta"}

    result = sort_rows_by_value(
        rows, value_for=lambda row: values[row.applicant_id],
        descending=descending, grouped=True,
    )

    assert [row.applicant_id for row in result] == (
        (["c:1", "b:1"] if descending else ["b:1", "c:1"]) + ["a:1", "a:2"]
    )


def test_large_finite_integer_is_not_converted_to_float():
    rows = [_row("1"), _row("2")]
    values = {"1": 10, "2": 10 ** 400}

    assert sort_rows_by_value(
        rows, value_for=lambda row: values[row.applicant_id], descending=True, grouped=False,
    ) == [rows[1], rows[0]]


def test_empty_input_does_not_read_values():
    def unexpected_value(_row):
        pytest.fail("Empty inputs have no sort values")

    assert sort_rows_by_value(
        [], value_for=unexpected_value, descending=True, grouped=True,
    ) == []
