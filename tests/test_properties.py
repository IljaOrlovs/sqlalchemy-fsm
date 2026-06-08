"""Property-based tests using Hypothesis.

These tests exercise invariants of the state-validation predicates and the
FSMMeta normalization logic across a wide range of generated inputs, rather
than asserting a handful of example cases.
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from sqlalchemy_fsm import bound
from sqlalchemy_fsm.meta import FSMMeta
from sqlalchemy_fsm.util import is_valid_fsm_state, is_valid_source_state

# Strategies -----------------------------------------------------------------

# A "valid" FSM state per the util predicate: any non-empty string.
valid_states = st.text(min_size=1).filter(lambda s: bool(s))

# Things that are NOT valid FSM states (but exclude the source-state
# specials "*" and None which is_valid_source_state treats separately).
non_string_types = st.one_of(
    st.integers(),
    st.floats(allow_nan=True),
    st.booleans(),
    st.lists(st.text()),
    st.tuples(st.text()),
    st.dictionaries(st.text(), st.text()),
    st.binary(),
)


# is_valid_fsm_state ---------------------------------------------------------


@given(valid_states)
def test_valid_fsm_state_accepts_any_non_empty_string(state):
    assert is_valid_fsm_state(state)


@given(non_string_types)
def test_valid_fsm_state_rejects_non_strings(value):
    assert not is_valid_fsm_state(value)


def test_valid_fsm_state_rejects_empty_string():
    assert not is_valid_fsm_state("")


def test_valid_fsm_state_rejects_none():
    assert not is_valid_fsm_state(None)


# is_valid_source_state ------------------------------------------------------


@given(valid_states)
def test_valid_source_state_accepts_any_non_empty_string(state):
    assert is_valid_source_state(state)


def test_valid_source_state_accepts_star():
    assert is_valid_source_state("*")


def test_valid_source_state_accepts_none():
    assert is_valid_source_state(None)


@given(non_string_types)
def test_valid_source_state_rejects_non_strings(value):
    assert not is_valid_source_state(value)


def test_valid_source_state_rejects_empty_string():
    assert not is_valid_source_state("")


# FSMMeta source normalization ----------------------------------------------


@given(st.lists(valid_states, min_size=1, max_size=10))
def test_fsm_meta_source_list_becomes_frozenset(sources):
    """Any iterable of valid sources is normalized to a frozenset of the
    same distinct elements."""
    meta = FSMMeta(sources, "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset(sources)
    assert isinstance(meta.sources, frozenset)


@given(valid_states)
def test_fsm_meta_single_string_source_becomes_singleton_frozenset(source):
    meta = FSMMeta(source, "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset({source})


@given(st.lists(valid_states, min_size=2, max_size=5))
def test_fsm_meta_source_duplicates_collapse(sources):
    """frozenset semantics: providing duplicates is equivalent to providing
    the deduplicated set."""
    deduped = list(dict.fromkeys(sources))  # preserve order, drop dupes
    assume(len(deduped) < len(sources * 2))  # ensure we actually test dupes
    meta_with_dups = FSMMeta(sources * 2, "t", (), (), bound.BoundFSMFunction)
    meta_unique = FSMMeta(deduped, "t", (), (), bound.BoundFSMFunction)
    assert meta_with_dups.sources == meta_unique.sources


@given(valid_states)
def test_fsm_meta_target_round_trip(target):
    meta = FSMMeta("*", target, (), (), bound.BoundFSMFunction)
    assert meta.target == target


@given(st.lists(valid_states, max_size=5), valid_states)
def test_fsm_meta_conditions_round_trip(conditions, target):
    """Conditions are stored as a tuple in the same order they were given."""

    # Distinct callables so the tuple comparison is order-sensitive.
    def make_cond(c):
        return lambda: c

    fns = [make_cond(c) for c in conditions]
    meta = FSMMeta("*", target, fns, (), bound.BoundFSMFunction)
    assert meta.conditions == tuple(fns)


# FSMMeta rejects invalid inputs --------------------------------------------


@given(non_string_types)
def test_fsm_meta_rejects_invalid_target(target):
    import pytest

    with pytest.raises(NotImplementedError):
        FSMMeta("*", target, (), (), bound.BoundFSMFunction)


@given(st.one_of(st.integers(), st.floats(allow_nan=True), st.booleans()))
def test_fsm_meta_rejects_invalid_scalar_source(source):
    """Non-iterable, non-string scalar sources are always rejected."""
    import pytest

    with pytest.raises(NotImplementedError):
        FSMMeta(source, "target", (), (), bound.BoundFSMFunction)


@given(
    st.lists(valid_states, min_size=1, max_size=3),
    non_string_types,
)
def test_fsm_meta_rejects_mixed_invalid_in_source_iterable(valid, invalid):
    """If an iterable source contains even one invalid item, the whole
    FSMMeta construction fails."""
    import pytest

    bad = list(valid) + [invalid]
    with pytest.raises(NotImplementedError):
        FSMMeta(bad, "target", (), (), bound.BoundFSMFunction)


# DictCache invariants -------------------------------------------------------


@given(st.lists(st.text(), min_size=1, max_size=20))
@settings(max_examples=50)
def test_dict_cache_getter_called_once_per_key(keys):
    """The cached getter is invoked once per distinct key, regardless of
    how many times the same key is requested."""
    from sqlalchemy_fsm.cache import dict_cache

    call_count = {}

    @dict_cache
    def getter(key):
        call_count[key] = call_count.get(key, 0) + 1
        return f"value-for-{key}"

    # Request each key several times.
    for k in keys:
        for _ in range(3):
            assert getter.get_value(k) == f"value-for-{k}"

    for k in set(keys):
        assert call_count[k] == 1
