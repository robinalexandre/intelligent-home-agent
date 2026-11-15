"""Words the code itself says or listens for, per language.

The model answers in the configured language on its own; these are the few strings the
code needs: the fallback reply, the words that close a conversation, the requests that
start a briefing, and examples in the prompt. English is the default and the fallback.
To add a language, copy the "en" entry and translate it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Strings:
    name: str
    fallback_reply: str
    closing_words: frozenset[str]
    briefing_request: str
    recap_request: str
    briefing_phrases: str
    recap_phrases: str
    detail_offer: str
    number_examples: str
    leave_example: str
    # Heads the list of actions waiting for a yes, written by the code under a reply.
    to_confirm: str = "To confirm:"


LANGUAGES: dict[str, Strings] = {
    "en": Strings(
        name="English",
        fallback_reply="Sorry, I couldn't finish that request.",
        closing_words=frozenset(
            "no thanks thank you that's all bye goodbye ok okay fine great perfect "
            "cool nothing else stop cheers done good".split()
        ),
        briefing_request="Give me the morning briefing",
        recap_request="Give me the evening recap",
        briefing_phrases='"the briefing", "my morning", "my day"',
        recap_phrases='"the recap", "my evening", "wrap up the day"',
        detail_offer='"Want the details for {spot}?"',
        number_examples="8:20, 13 knots, 1.5 m",
        leave_example='"leave at twenty past eight"',
    ),
    "fr": Strings(
        name="French",
        fallback_reply="Désolé, je n'ai pas réussi à terminer cette demande.",
        closing_words=frozenset(
            "non merci beaucoup c'est tout bon rien d'autre stop ok d'accord ça ira parfait "
            "super nickel top cool au revoir pour moi".split()
        ),
        briefing_request="Fais-moi le briefing du matin",
        recap_request="Fais-moi le récap du soir",
        briefing_phrases='"le briefing", "mon point du matin", "ma journée"',
        recap_phrases='"le récap", "le point du soir", "ma soirée"',
        detail_offer='"Tu veux le détail pour {spot} ?"',
        number_examples="8h20, 13 nœuds, 1,5 m",
        leave_example='"pars à huit heures vingt"',
        to_confirm="À confirmer :",
    ),
}


def strings(language: str) -> Strings:
    return LANGUAGES.get(language, LANGUAGES["en"])
