import pytest

from intelligent_home_agent.tools.base import ToolError
from intelligent_home_agent.tools.unlock_codes import (
    LOCKOUT_SECONDS,
    MAX_WRONG_CODES,
    UnlockCodes,
    letters,
    new_secret,
    otpauth_uri,
    totp,
)

# RFC 6238 test key ("12345678901234567890"), whose 6-digit code at t=59 is 287082.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
PHRASE = "The wind blows at North Beach"


class Clock:
    def __init__(self, now=59.0):
        self.now = now

    def __call__(self):
        return self.now


def codes(tmp_path, clock, totp_secret=RFC_SECRET, phrase=PHRASE):
    return UnlockCodes(
        tmp_path / "codes.json", totp_secret, phrase, clock=clock, draw=lambda n, k: [9, 2, 5]
    )


def test_totp_matches_the_rfc_vector():
    assert totp(RFC_SECRET, 1) == "287082"


def test_a_new_secret_makes_codes_and_an_authenticator_link():
    secret = new_secret()
    assert len(totp(secret, 7)) == 6
    assert otpauth_uri(secret, "door", "Home agent").startswith("otpauth://totp/Home%20agent:")


def test_the_authenticator_code_works_once(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    c.check("287 082")
    with pytest.raises(ToolError, match="already used"):
        c.check("287082")


def test_a_code_from_the_step_before_is_still_accepted(tmp_path):
    c = codes(tmp_path, Clock(now=59.0 + 30))
    c.check("287082")


def test_the_phrase_challenge_asks_positions_and_takes_those_letters(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    question = c.ask()
    assert "letters 2, 5 and 9" in question
    assert "6-digit code" in question
    # "thewindblowsatnorthbeach": 2 = h, 5 = i, 9 = l
    c.check("H I L")


def test_a_challenge_is_spent_by_any_answer(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    c.ask()
    with pytest.raises(ToolError, match="Wrong"):
        c.check("xyz")
    with pytest.raises(ToolError, match="No question is pending"):
        c.check("hil")


def test_asking_again_cannot_reroll_the_positions(tmp_path):
    clock = Clock()
    draws = iter([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    c = UnlockCodes(tmp_path / "c.json", None, PHRASE, clock=clock, draw=lambda n, k: next(draws))
    assert "letters 1, 2 and 3" in c.ask()
    clock.now += 60
    assert "letters 1, 2 and 3" in c.ask()
    assert "expires in 4 min" in c.ask()
    clock.now += 5 * 60
    assert "letters 4, 5 and 6" in c.ask()


def test_a_challenge_expires(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    c.ask()
    clock.now += 6 * 60
    with pytest.raises(ToolError, match="expired"):
        c.check("hil")


def test_letters_without_a_question_never_open(tmp_path):
    c = codes(tmp_path, Clock())
    with pytest.raises(ToolError, match="No question is pending"):
        c.check("hil")


def test_wrong_answers_block_opening_for_a_while(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    for _ in range(MAX_WRONG_CODES - 1):
        with pytest.raises(ToolError, match="Wrong"):
            c.check("000000")
    with pytest.raises(ToolError, match="blocked for 15 min"):
        c.check("000000")
    with pytest.raises(ToolError, match="blocked"):
        c.check("287082")
    with pytest.raises(ToolError, match="blocked"):
        c.ask()
    clock.now = 59.0 + LOCKOUT_SECONDS + 1
    c.check(totp(RFC_SECRET, int(clock.now // 30)))


def test_a_right_answer_clears_the_wrong_count(tmp_path):
    clock = Clock()
    c = codes(tmp_path, clock)
    for _ in range(MAX_WRONG_CODES - 1):
        with pytest.raises(ToolError):
            c.check("000000")
    c.check("287082")
    with pytest.raises(ToolError, match="Wrong"):
        c.check("111111")


def test_phrase_only(tmp_path):
    c = codes(tmp_path, Clock(), totp_secret=None)
    assert "6-digit" not in c.ask()
    c.check("hil")


def test_a_short_phrase_is_refused(tmp_path):
    with pytest.raises(ValueError, match="12 letters"):
        UnlockCodes(tmp_path / "c.json", None, "too short")


def test_letters_drop_accents_spaces_and_case():
    assert letters("À l'Île-d'Été 2") == "aliledete2"
