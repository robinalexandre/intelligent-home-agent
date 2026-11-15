"""Words the code itself says or listens for, per language.

The model answers in the configured language on its own; these are the few strings the
code needs: the fallback reply, the words that close a conversation, the requests that
start a briefing, the lines it writes under a reply (what waits for the user's go), and
examples in the prompt. English is the default and the fallback.
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
    # Heads the numbered list of actions waiting for the user's go, written by the code
    # under a reply; it names the words that run them, which the prompt tells the model.
    waiting_header: str
    # How a parked action reads in that list (tools pass their own values in).
    plan_summary: str
    search_summary: str
    connector_write_summary: str
    may_destroy: str
    # A write that ran without a go, noted under the reply so it never goes unseen.
    written_line: str
    # Example of offering details in a chat, for the prompt.
    chat_detail_offer: str
    # File drives (tools/drives.py): how a write reads in the list of what waits. A place
    # is a drive's label and a path; an item of the local trash is where it was.
    drive_place: str
    drive_renamed: str
    drive_create_folder: str
    drive_copy: str
    drive_move: str
    drive_copy_folder: str
    drive_move_folder: str
    drive_save: str
    drive_delete_file: str
    drive_delete_empty_folder: str
    drive_delete_folder: str
    drive_delete_file_without_copy: str
    drive_delete_folder_without_copy: str
    drive_trash_item: str
    drive_trash_unknown: str
    drive_restore: str
    drive_purge: str


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
        waiting_header='Waiting for your go ("yes", or "yes except 2"):',
        plan_summary="Plan: {plan}",
        search_summary='Search the web for "{query}" and carry on',
        connector_write_summary="{label}{warning}: {tool} {arguments}",
        may_destroy=" (may delete or overwrite)",
        written_line="Written: {summary}",
        chat_detail_offer='"Want the details?"',
        drive_place="{drive}: {path}",
        drive_renamed=' as "{name}"',
        drive_create_folder="Create the folder {place}",
        drive_copy="Copy {source} into {target}{renamed}",
        drive_move="Move {source} into {target}{renamed}",
        drive_copy_folder="Copy the folder {source} into {target}{renamed}",
        drive_move_folder="Move the folder {source} into {target}{renamed}",
        drive_save='Save "{name}" into {place}',
        drive_delete_file="Delete the file {place}",
        drive_delete_empty_folder="Delete the empty folder {place}",
        drive_delete_folder="Delete the folder {place} and everything in it",
        drive_delete_file_without_copy="Delete the file {place} without keeping a copy",
        drive_delete_folder_without_copy=(
            "Delete the folder {place} and everything in it, without keeping a copy"
        ),
        drive_trash_item="{place} ({files} file(s))",
        drive_trash_unknown="local copy {id}",
        drive_restore="Put {item} back in its drive, replacing nothing",
        drive_purge="Erase for good the local copy of {item}",
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
        waiting_header="En attente de ton accord (« oui », ou « oui sauf 2 ») :",
        plan_summary="Plan : {plan}",
        search_summary="Chercher sur le web « {query} » et reprendre",
        connector_write_summary="{label}{warning} : {tool} {arguments}",
        may_destroy=" (peut supprimer ou écraser)",
        written_line="Écrit : {summary}",
        chat_detail_offer='"Tu veux le détail ?"',
        drive_place="{drive} : {path}",
        drive_renamed=" sous le nom « {name} »",
        drive_create_folder="Créer le dossier {place}",
        drive_copy="Copier {source} dans {target}{renamed}",
        drive_move="Déplacer {source} dans {target}{renamed}",
        drive_copy_folder="Copier le dossier {source} dans {target}{renamed}",
        drive_move_folder="Déplacer le dossier {source} dans {target}{renamed}",
        drive_save="Enregistrer « {name} » dans {place}",
        drive_delete_file="Supprimer le fichier {place}",
        drive_delete_empty_folder="Supprimer le dossier vide {place}",
        drive_delete_folder="Supprimer le dossier {place} et tout son contenu",
        drive_delete_file_without_copy="Supprimer le fichier {place} sans en garder de copie",
        drive_delete_folder_without_copy=(
            "Supprimer le dossier {place} et tout son contenu, sans en garder de copie"
        ),
        drive_trash_item="{place} ({files} fichier(s))",
        drive_trash_unknown="copie locale {id}",
        drive_restore="Remettre {item} dans son drive, sans rien remplacer",
        drive_purge="Effacer pour de bon la copie locale de {item}",
    ),
}


def strings(language: str) -> Strings:
    return LANGUAGES.get(language, LANGUAGES["en"])
