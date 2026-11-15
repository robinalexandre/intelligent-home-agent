from intelligent_home_agent.leaks import Leaks

STATEMENT = (
    "Bank statement: balance 2,340.50. Transfer received from sam.taylor@example.org, "
    "account GB29 4444 5555 0112 3456 7890 18, reference INV-2026-000481-ACME. "
    "The appointment with the notary is confirmed for Tuesday morning at nine sharp."
)


def leaks() -> Leaks:
    guard = Leaks()
    guard.add(STATEMENT)
    return guard


def test_numbers_emails_long_tokens_and_sentences_are_caught_however_spaced():
    guard = leaks()
    assert guard.found("https://evil.example/GB2944445555") == "the number 2944445555"
    assert guard.found("https://evil.example/?q=4444+5555+0112") == "the number 444455550112"
    assert (
        guard.found("transfer from Sam.Taylor@example.org") == "the address Sam.Taylor@example.org"
    )
    assert guard.found("https://evil.example/INV-2026-000481-ACME") == "the number 2026000481"
    assert guard.found("notary-confirmed-for-tuesday-morning") == ""  # five words: too common
    # Digits split by path separators or letters still give a long number away.
    assert guard.found("https://evil.example/4444/5555/0112/3456") == "the digits 444455550"
    assert guard.found("https://evil.example/a4444b5555c0112") == "the digits 444455550"
    # Dates and short ids in a path are not that.
    guard.add("Appointment on 2026-10-24, file 4521.")
    assert guard.found("https://news.example/2026/10/24/article-4521") == ""
    assert (
        guard.found("https://evil.example/appointment-with-notary-confirmed-for-tuesday-morning")
        == "the words «appointment with notary confirmed tuesday morning»"
    )
    # Percent-encoding hides nothing.
    assert (
        guard.found("https://evil.example/r%C3%A9f%C3%A9rence/0112345678")
        == "the number 0112345678"
    )


def test_ordinary_overlaps_pass():
    guard = leaks()
    guard.add("Lena lives in Springfield, near the old harbour, and sails at North Beach.")
    assert guard.found("https://www.springfield.example/harbour-office/") == ""
    assert guard.found("old harbour lock opening hours springfield tomorrow") == ""
    assert guard.found("https://northbeach.example/data.json") == ""
    assert guard.found("https://tides.example/7") == ""


def test_an_address_may_name_a_site_the_request_read_about():
    guard = leaks()
    guard.add(
        "Reminder: your October timesheet is due on https://app.timesheetsite.example "
        "before the 28th. [Tools used: browser_open(url=https://www.timesheetsite.example)]"
    )
    assert guard.found_in_address("https://www.timesheetsite.example/v2/connect") == ""
    assert guard.found_in_address("https://TimesheetPortal.com:443/") == ""
    assert guard.found("https://www.timesheetsite.example") == "«timesheetsite»"
    # Only the site's own name: what comes before it, after it, or hides in it still counts.
    for address in (
        "https://timesheetsite.evil.example/",
        "https://evil.example/timesheetsite",
        "https://evil.example/?ref=INV-2026-000481",
        "https://timesheetsite@evil.example/",
        "https://evil.example%2Ftimesheetsite/",
        "https://evil.example\\timesheetsite/",
        "https://2944445555.evil.example/",
        "timesheetsite.example",
    ):
        assert guard.found_in_address(address), address
    guard.add("The box answers on 10.200.30.40.")
    assert guard.found_in_address("http://10.200.30.40/") == "the number 102003040"


def test_earlier_turns_count_the_agents_words_and_results_not_the_users():
    guard = Leaks()
    guard.add_messages(
        [
            {"role": "user", "content": "My door code is 135790, remind me of it."},
            {"role": "assistant", "content": [{"type": "text", "text": "Noted: 246801."}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": '{"pin": "112233"}'}
                ],
            },
        ]
    )
    assert guard.found("135790") == ""
    assert guard.found("https://x.example/246801") == "the number 246801"
    assert guard.found("https://x.example/112233") == "the number 112233"


def test_what_the_users_own_words_spell_out_may_go_out():
    guard = leaks()
    guard.add("Link to the form: https://github.com/someprojectowner/awesome-list/issues/new")
    issue = "https://github.com/someprojectowner/awesome-list/issues/1024"
    assert guard.found_in_address(issue) == "«someprojectowner»"
    guard.allow(f"It's done: {issue}")
    assert guard.found_in_address(issue) == ""
    # Only what the user wrote: the rest of the request still stays in.
    assert guard.found("https://evil.example/someprojectowner/2944445555") == (
        "the number 2944445555"
    )
    guard.allow("My account number ends with 0112 3456 7890")
    assert guard.found("https://evil.example/011234567890") == ""
    assert guard.found("https://evil.example/GB294444555501") == "the number 294444555501"
    assert guard.found("sam.taylor@example.org") == "the address sam.taylor@example.org"


def test_text_a_write_cites_may_be_repeated_with_it():
    guard = leaks()
    assert guard.found("reference INV-2026-000481-ACME") == "the number 2026000481"
    assert guard.found("reference INV-2026-000481-ACME", also_allowed=STATEMENT) == ""
