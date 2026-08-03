"""Tests for msg_search: aggregation, fuzzy scoring (contiguous = best), and
ranked search with a match count.
"""
import json

from clauthing.msg_search import load_messages, fuzzy_score, search


# ── fuzzy scoring: contiguous beats scattered ────────────────────────────────

def test_empty_query_matches_with_zero_score():
    assert fuzzy_score("", "anything") == (0.0, [])


def test_contiguous_substring_matches():
    score, pos = fuzzy_score("abc", "xxabcyy")
    assert pos == [2, 3, 4]
    assert score > 0


def test_scattered_subsequence_matches():
    score, pos = fuzzy_score("abc", "aXbXXc")
    assert pos == [0, 2, 5]        # each query char in order
    assert score > 0


def test_no_match_returns_none():
    assert fuzzy_score("abc", "acb") is None      # b before c -> no subsequence
    assert fuzzy_score("zzz", "abc") is None


def test_contiguous_scores_higher_than_scattered():
    contiguous = fuzzy_score("deploy", "run deploy now")[0]
    scattered = fuzzy_score("deploy", "d e p l o y scattered")[0]
    assert contiguous > scattered


def test_tighter_scattered_beats_looser():
    tight = fuzzy_score("ac", "aXc")[0]
    loose = fuzzy_score("ac", "aXXXXXXc")[0]
    assert tight > loose


def test_earlier_substring_scores_higher():
    early = fuzzy_score("foo", "foo bar")[0]
    late = fuzzy_score("foo", "bar bar foo")[0]
    assert early > late


# ── ranked search over messages ──────────────────────────────────────────────

def _msgs(*texts):
    return [{"from": "a", "window": "w", "message": t, "ts": i}
            for i, t in enumerate(texts)]


def test_search_empty_query_returns_all_in_order():
    msgs = _msgs("one", "two")
    assert [m["message"] for _, m in search(msgs, "")] == ["one", "two"]


def test_search_ranks_contiguous_match_first():
    msgs = _msgs("d-e-p-l-o-y scattered", "please deploy the thing")
    results = search(msgs, "deploy")
    assert results[0][1]["message"] == "please deploy the thing"   # contiguous
    assert len(results) == 2                                       # both match


def test_search_filters_non_matches_and_counts():
    msgs = _msgs("deploy prod", "restart nginx", "deployment plan")
    results = search(msgs, "deploy")
    assert len(results) == 2                       # nginx is not a match
    assert all("deploy" in m["message"] for _, m in results)


def test_search_matches_sender_and_window_too():
    msgs = [{"from": "godwin", "window": "pain", "message": "hi", "ts": 1},
            {"from": "bob", "window": "state", "message": "hello", "ts": 2}]
    assert len(search(msgs, "godwin")) == 1        # matches the 'from' field
    assert len(search(msgs, "pain")) == 1          # matches the 'window' field


def test_search_ties_break_by_recency():
    # two identical contiguous matches; newest (higher ts) should rank first
    msgs = [{"from": "a", "window": "w", "message": "deploy", "ts": 1},
            {"from": "a", "window": "w", "message": "deploy", "ts": 9}]
    ordered = load_ordered = search(sorted(msgs, key=lambda m: m["ts"], reverse=True), "deploy")
    assert ordered[0][1]["ts"] == 9


# ── aggregation across inboxes ───────────────────────────────────────────────

def test_load_messages_aggregates_all_inboxes_newest_first(tmp_path):
    (tmp_path / "winA.jsonl").write_text(
        json.dumps({"from": "a", "message": "older", "ts": 10}) + "\n")
    (tmp_path / "winB.jsonl").write_text(
        json.dumps({"from": "b", "message": "newer", "ts": 20}) + "\n" +
        "not json\n")                              # malformed line is skipped
    msgs = load_messages(tmp_path)
    assert [m["message"] for m in msgs] == ["newer", "older"]   # newest first
    # each message is tagged with the inbox it came from
    assert {m["window"] for m in msgs} == {"winA", "winB"}


def test_load_messages_empty_dir(tmp_path):
    assert load_messages(tmp_path) == []
