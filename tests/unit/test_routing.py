import pytest

from app.domain.routing import Leg, choose_next_leg, suitable_legs

IMAGE_FAST = [
    Leg(1, "alpha", 1, 4, False),
    Leg(2, "beta", 2, 10, False),
]
VIDEO_LONG = [
    Leg(3, "beta", 1, 2, False),
    Leg(4, "alpha", 2, 1, True),
]
CHAIN = [
    Leg(10, "alpha", 1, 5, False),
    Leg(11, "beta", 2, 5, True),
    Leg(12, "alpha", 3, 5, True),
    Leg(13, "beta", 4, 5, False),
]


def test_refs_limit():
    assert [leg.provider for leg in suitable_legs(IMAGE_FAST, 4)] == ["alpha", "beta"]
    assert [leg.provider for leg in suitable_legs(IMAGE_FAST, 5)] == ["beta"]
    assert suitable_legs(IMAGE_FAST, 11) == []


def test_sorted():
    assert [leg.priority for leg in suitable_legs(reversed(CHAIN), 0)] == [1, 2, 3, 4]


@pytest.mark.parametrize("error", ["technical", "timeout"])
def test_next_leg(error):
    assert choose_next_leg(IMAGE_FAST, 1, 0, error).provider == "beta"
    assert choose_next_leg(VIDEO_LONG, 1, 0, error).provider == "alpha"


def test_moderation():
    assert choose_next_leg(VIDEO_LONG, 1, 0, "moderation") is None


def test_moderation_no_flag():
    assert choose_next_leg(IMAGE_FAST, 1, 0, "moderation").provider == "beta"


def test_moderation_chain():
    assert choose_next_leg(CHAIN, 1, 0, "moderation").id == 13


def test_technical_no_skip():
    assert choose_next_leg(CHAIN, 1, 0, "technical").id == 11


def test_refs_on_transition():
    assert choose_next_leg(VIDEO_LONG, 1, 2, "technical") is None


def test_last_leg():
    assert choose_next_leg(IMAGE_FAST, 2, 0, "technical") is None
