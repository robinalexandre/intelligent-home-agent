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
    open_page_summary: str
    connector_write_summary: str
    may_destroy: str
    # A write that ran without a go, noted under the reply so it never goes unseen.
    written_line: str
    # Example of offering details in a chat, for the prompt.
    chat_detail_offer: str
    # Names of the files the agent makes when the model gives none (tools/documents.py,
    # pdf_forms.py, illustrations.py, generation.py): the user sees them in the chat.
    document_title: str
    filled_suffix: str
    drawing_name: str
    animation_name: str
    image_name: str
    video_name: str
    # The two photos of an exercise: its start and end positions.
    exercise_steps: tuple[str, str]
    # Memory (tools/memory.py, tools/procedures.py): how its writes read in that list,
    # and the words that ask to remember, for the prompt.
    remember_summary: str
    forget_summary: str
    procedure_summary: str
    remember_phrases: str
    # Mail and calendar writes, as the user reads them before giving their go.
    mail_headline: str
    draft_headline: str
    mail_flag_summary: str
    mail_create_folder_summary: str
    mail_move_summary: str
    draft_update_summary: str
    draft_delete_summary: str
    draft_change_recipients: str
    draft_nobody: str
    draft_change_subject: str
    draft_change_body: str
    draft_change_attach: str
    draft_change_remove: str
    draft_no_change: str
    calendar_add_summary: str
    calendar_where: str
    calendar_notes: str
    # A draft saved in the turn, noted under the reply so none goes unseen; and the
    # warning when it carries files after the request read content written by others.
    draft_line: str
    draft_files: str
    draft_check: str
    # A chat in a messaging app (chat_session.py, whatsapp.py, telegram.py): what the code
    # says there by itself, the choice that lists what waits for the user's go and its
    # two closing options, and what it answers to the user's picks.
    chat_stopped: str
    chat_file_unreadable: str
    chat_voice_unclear: str
    chat_still_working: str
    chat_reminders_not_created: str
    chat_choice_expired: str
    waiting_in_poll: str
    poll_name: str
    poll_validate: str
    poll_cancel: str
    poll_see_above: str
    poll_cancelled: str
    poll_cancelled_note: str
    poll_nothing_ticked: str
    go_done: str
    go_failed: str
    go_note: str
    # Marking other WhatsApp chats as read (tools/whatsapp_chats.py).
    whatsapp_mark_read_summary: str
    # Sets the register of a voice message for the speech model (transcription.py).
    voice_prompt: str
    # News of what runs in the background (progress.py).
    progress_waiting: str
    progress_late: str
    progress_share: str
    progress_item: str


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
        open_page_summary="Open the page {url} and carry on with what it says",
        connector_write_summary="{label}{warning}: {tool} {arguments}",
        may_destroy=" (may delete or overwrite)",
        written_line="Written: {summary}",
        chat_detail_offer='"Want the details?"',
        document_title="Document",
        filled_suffix="filled",
        drawing_name="drawing",
        animation_name="animation",
        image_name="image",
        video_name="video",
        exercise_steps=("start", "end"),
        remember_summary="Remember “{fact}”",
        forget_summary="Forget what I remember about “{words}”",
        procedure_summary="Keep the steps",
        remember_phrases='"remember that", "keep in mind", "note that I"',
        mail_headline='"{subject}" from {sender}',
        draft_headline='"{subject}" to {to}',
        mail_flag_summary="Flag the email {headline} ({account})",
        mail_create_folder_summary='Create the mail folder "{name}" ({account})',
        mail_move_summary=(
            'File into the folder "{folder}" ({target}) the email of {source} {headline}'
        ),
        draft_update_summary=(
            "Change the draft of {account} {headline}: {what} (the old one goes to the trash)"
        ),
        draft_delete_summary="Delete the draft of {account} {headline} (it goes to the trash)",
        draft_change_recipients="{field}: {value}",
        draft_nobody="nobody",
        draft_change_subject="subject: {subject}",
        draft_change_body="new text: {text}",
        draft_change_attach="attach {files}",
        draft_change_remove="remove {files}",
        draft_no_change="nothing changes",
        calendar_add_summary='Add to the calendar "{title}", from {start} to {end}{where}{notes}',
        calendar_where=", at {location}",
        calendar_notes=" (notes: {notes})",
        draft_line='Draft saved: {to}, "{subject}"{files}',
        draft_files=", attached: {files}",
        draft_check=(
            "Files attached after reading content written by others ({files}): check the "
            "recipients before sending."
        ),
        chat_stopped=(
            "I'm restarting and didn't finish answering this message: send it again in a minute."
        ),
        chat_file_unreadable=(
            "I can't get this file: PDF, images, text, voice messages and office files "
            "only, {megabytes} MB at most."
        ),
        chat_voice_unclear="I couldn't make out this voice message, can you write it?",
        chat_still_working=(
            "⏳ Still on your request ({minutes} min). I'll be back as soon as it's done."
        ),
        chat_reminders_not_created=(
            "I can't create reminders on your phone from this chat, ask by voice: {titles}."
        ),
        chat_choice_expired="This one is too old for me: ask me again.",
        waiting_in_poll="🕒 *Waiting for your go*, pick below 👇",
        poll_name="Which ones do I do? Tick, then Go",
        poll_validate="✅ Go",
        poll_cancel="❌ Cancel",
        poll_see_above="{number}. → see no. {number} above",
        poll_cancelled="Cancelled, I do nothing from this list.",
        poll_cancelled_note="Cancelled.",
        poll_nothing_ticked="Nothing ticked: tick what I should do, then Go.",
        go_done="✅ {summary}{note}",
        go_failed="❌ {summary}: {error}",
        go_note=" ({note})",
        whatsapp_mark_read_summary="Mark as read on WhatsApp the messages of: {names}",
        voice_prompt="Voice message to my assistant: remind me tomorrow, tonight, next week.",
        progress_waiting="waiting",
        progress_late="longer than expected, {minutes} min",
        progress_share="~{percent}% ({minutes} min of ~{estimate})",
        progress_item="{label}: {state}",
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
        open_page_summary="Ouvrir la page {url} et reprendre avec ce qu'elle dit",
        connector_write_summary="{label}{warning} : {tool} {arguments}",
        may_destroy=" (peut supprimer ou écraser)",
        written_line="Écrit : {summary}",
        chat_detail_offer='"Tu veux le détail ?"',
        document_title="Document",
        filled_suffix="rempli",
        drawing_name="schéma",
        animation_name="animation",
        image_name="image",
        video_name="vidéo",
        exercise_steps=("départ", "arrivée"),
        remember_summary="Retenir « {fact} »",
        forget_summary="Oublier ce que je retiens sur « {words} »",
        procedure_summary="Retenir la démarche",
        remember_phrases='"retiens que", "souviens-toi", "note que je"',
        mail_headline="« {subject} » de {sender}",
        draft_headline="« {subject} » à {to}",
        mail_flag_summary="Signaler le mail {headline} ({account})",
        mail_create_folder_summary="Créer le dossier de mails « {name} » ({account})",
        mail_move_summary=(
            "Ranger dans le dossier « {folder} » ({target}) le mail de {source} {headline}"
        ),
        draft_update_summary=(
            "Modifier le brouillon de {account} {headline} : {what} (l'ancien va à la corbeille)"
        ),
        draft_delete_summary=(
            "Supprimer le brouillon de {account} {headline} (il va à la corbeille)"
        ),
        draft_change_recipients="{field} : {value}",
        draft_nobody="personne",
        draft_change_subject="objet : {subject}",
        draft_change_body="nouveau texte : {text}",
        draft_change_attach="joindre {files}",
        draft_change_remove="retirer {files}",
        draft_no_change="rien ne change",
        calendar_add_summary=(
            "Ajouter au calendrier « {title} », du {start} au {end}{where}{notes}"
        ),
        calendar_where=", à {location}",
        calendar_notes=" (notes : {notes})",
        draft_line="Brouillon enregistré : {to}, « {subject} »{files}",
        draft_files=", pièces jointes : {files}",
        draft_check=(
            "Fichiers joints après lecture d'un contenu écrit par d'autres ({files}) : "
            "vérifie les destinataires avant d'envoyer."
        ),
        chat_stopped=(
            "Je redémarre et je n'ai pas fini de répondre à ce message : renvoie-le-moi "
            "dans une minute."
        ),
        chat_file_unreadable=(
            "Je ne peux pas récupérer ce fichier : PDF, images, texte, vocaux et fichiers "
            "bureautiques seulement, {megabytes} Mo max."
        ),
        chat_voice_unclear="Je n'ai pas réussi à comprendre ce vocal, tu peux me l'écrire ?",
        chat_still_working=(
            "⏳ Toujours sur ta demande ({minutes} min). Je reviens vers toi dès que c'est fini."
        ),
        chat_reminders_not_created=(
            "Je ne peux pas créer de rappel sur ton téléphone depuis ce chat, demande-le à "
            "la voix : {titles}."
        ),
        chat_choice_expired="Celui-ci est trop ancien pour moi : redemande-moi.",
        waiting_in_poll="🕒 *En attente de ton accord*, choisis ci-dessous 👇",
        poll_name="Je fais lesquels ? Coche, puis Valider",
        poll_validate="✅ Valider",
        poll_cancel="❌ Annuler",
        poll_see_above="{number}. → détail n°{number} ci-dessus",
        poll_cancelled="Annulé, je ne fais rien de cette liste.",
        poll_cancelled_note="Annulé.",
        poll_nothing_ticked="Rien de coché : coche ce que je fais, puis Valider.",
        go_done="✅ {summary}{note}",
        go_failed="❌ {summary} : {error}",
        go_note=" ({note})",
        whatsapp_mark_read_summary="Marquer comme lus sur WhatsApp les messages de : {names}",
        voice_prompt=(
            "Message vocal à mon assistant : rappelle-moi demain, ce soir, la semaine prochaine."
        ),
        progress_waiting="en attente",
        progress_late="plus long que prévu, {minutes} min",
        progress_share="~{percent} % ({minutes} min sur ~{estimate})",
        progress_item="{label} : {state}",
    ),
}


def strings(language: str) -> Strings:
    return LANGUAGES.get(language, LANGUAGES["en"])
