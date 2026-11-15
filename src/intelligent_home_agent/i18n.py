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
    # Home devices (docs/home-devices.md): how a parked action reads.
    lock_door_summary: str
    unlock_door_summary: str
    unlatch_door_summary: str
    camera_on_summary: str
    camera_off_summary: str
    pet_feed_summary: str
    pet_clean_summary: str
    # Pet supplies, pushed as notifications (pet_alerts.py).
    pet_alert_title: str
    pet_litter_name: str
    pet_feeder_name: str
    pet_litter_low: str
    pet_litter_low_no_level: str
    pet_food_low: str
    pet_food_low_no_level: str
    pet_bin_full: str
    pet_bin_again: str
    # Leaving home (departure.py): what was left, and each fix waiting for a go.
    departure_title: str
    departure_header: str
    departure_no_fix: str
    departure_unreadable: str
    departure_lock: str
    departure_lights: str
    departure_speaker: str
    departure_tv: str
    departure_apple: str
    departure_ha: str
    departure_door_open: str
    departure_not_locked: str
    departure_not_locked_ask: str
    departure_lock_fix: str
    departure_lights_on: str
    departure_and_more: str
    departure_lights_fix: str
    departure_speaker_playing: str
    departure_speaker_fix: str
    departure_tv_on: str
    departure_tv_fix: str
    departure_apple_tv_on: str
    departure_apple_tv_fix: str
    departure_homepod_playing: str
    departure_homepod_fix: str
    departure_player_playing: str
    departure_player_fix: str


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
        lock_door_summary='Lock the door "{name}" (Nuki)',
        unlock_door_summary='Unlock the door "{name}" (Nuki)',
        unlatch_door_summary='Open the door "{name}": unlock it and pull the latch (Nuki)',
        camera_on_summary=(
            "Netatmo: turn on monitoring of the camera {camera}; it detects and records "
            "events again"
        ),
        camera_off_summary=(
            "Netatmo: turn off monitoring of the camera {camera}; no detection, alert or "
            "recording until it is turned back on"
        ),
        pet_feed_summary=(
            "PetKit feeder{device}: dispense {portions} portion(s) of food now, on top of the"
            " planned meals"
        ),
        pet_clean_summary=(
            "PetKit litter box{device}: start a cleaning cycle now (it turns and empties the "
            "waste into the bin)"
        ),
        pet_alert_title="Pet supplies",
        pet_litter_name="Litter box",
        pet_feeder_name="Feeder",
        pet_litter_low="🐱 {name}: {level}% of litter left, time to add some.",
        pet_litter_low_no_level="🐱 {name}: the litter is low, time to add some.",
        pet_food_low="🍽️ {name}: {level}% of food left, time to refill it.",
        pet_food_low_no_level="🍽️ {name}: the food is low, time to refill it.",
        pet_bin_full="🗑️ {name}: the waste bin is full, it needs emptying{again}.",
        pet_bin_again=" (reminder)",
        departure_title="Leaving home",
        departure_header="🏠 You left, but:",
        departure_no_fix="I can't do anything about it from here.",
        departure_unreadable="⚠️ I couldn't read {what}.",
        departure_lock="the lock",
        departure_lights="the lights",
        departure_speaker="the speaker",
        departure_tv="the TV",
        departure_apple="the Apple TV and the HomePods",
        departure_ha="Home Assistant",
        departure_door_open='🚪 The door of "{name}" was left open.',
        departure_not_locked='🔓 "{name}" is not locked.',
        departure_not_locked_ask='🔓 "{name}" is not locked: ask me to lock it.',
        departure_lock_fix='Lock "{name}"',
        departure_lights_on="💡 {count} light(s) on: {names}.",
        departure_and_more=" and {count} more",
        departure_lights_fix="Turn off the lights",
        departure_speaker_playing="🔊 The speaker is still playing.",
        departure_speaker_fix="Put the speaker in standby",
        departure_tv_on="📽️ The {name} is still on{app}.",
        departure_tv_fix="Turn off the {name}",
        departure_apple_tv_on='📺 The Apple TV "{name}" is on{app}.',
        departure_apple_tv_fix='Turn off the Apple TV "{name}"',
        departure_homepod_playing='🔈 The HomePod "{name}" is still playing.',
        departure_homepod_fix='Pause the HomePod "{name}"',
        departure_player_playing='🔊 "{name}" is still playing.',
        departure_player_fix='Pause "{name}"',
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
        lock_door_summary="Verrouiller la porte « {name} » (Nuki)",
        unlock_door_summary="Déverrouiller la porte « {name} » (Nuki)",
        unlatch_door_summary="Ouvrir la porte « {name} » : déverrouiller et tirer le pêne (Nuki)",
        camera_on_summary=(
            "Netatmo : activer la surveillance de la caméra {camera} ; elle détecte et "
            "enregistre de nouveau les événements"
        ),
        camera_off_summary=(
            "Netatmo : couper la surveillance de la caméra {camera} ; plus aucune détection, "
            "alerte ni enregistrement jusqu'à ce qu'elle soit réactivée"
        ),
        pet_feed_summary=(
            "Distributeur PetKit{device} : donner maintenant {portions} portion(s) de "
            "croquettes, en plus des repas prévus"
        ),
        pet_clean_summary=(
            "Litière PetKit{device} : lancer maintenant un cycle de nettoyage (elle tourne et"
            " vide les déjections dans le bac)"
        ),
        pet_alert_title="Animaux",
        pet_litter_name="Litière",
        pet_feeder_name="Distributeur",
        pet_litter_low="🐱 {name} : plus que {level} % de litière, pense à en remettre.",
        pet_litter_low_no_level="🐱 {name} : la litière est basse, pense à en remettre.",
        pet_food_low="🍽️ {name} : plus que {level} % de croquettes, pense à le remplir.",
        pet_food_low_no_level="🍽️ {name} : la réserve de croquettes est basse, pense à la remplir.",
        pet_bin_full="🗑️ {name} : le bac à déchets est plein, il faut le vider{again}.",
        pet_bin_again=" (rappel)",
        departure_title="Départ",
        departure_header="🏠 Tu es parti, mais :",
        departure_no_fix="Je ne peux rien y faire d'ici.",
        departure_unreadable="⚠️ Je n'ai pas pu lire {what}.",
        departure_lock="la serrure",
        departure_lights="les lumières",
        departure_speaker="l'enceinte",
        departure_tv="la télé",
        departure_apple="l'Apple TV et les HomePods",
        departure_ha="Home Assistant",
        departure_door_open="🚪 La porte de « {name} » est restée ouverte.",
        departure_not_locked="🔓 « {name} » n'est pas fermée à clé.",
        departure_not_locked_ask="🔓 « {name} » n'est pas fermée à clé : demande-moi de la fermer.",
        departure_lock_fix="Fermer « {name} » à clé",
        departure_lights_on="💡 {count} lumière(s) allumée(s) : {names}.",
        departure_and_more=" et {count} autres",
        departure_lights_fix="Éteindre les lumières",
        departure_speaker_playing="🔊 L'enceinte joue encore.",
        departure_speaker_fix="Mettre l'enceinte en veille",
        departure_tv_on="📽️ Encore en marche : {name}{app}.",
        departure_tv_fix="Éteindre : {name}",
        departure_apple_tv_on="📺 L'Apple TV « {name} » est allumée{app}.",
        departure_apple_tv_fix="Éteindre l'Apple TV « {name} »",
        departure_homepod_playing="🔈 Le HomePod « {name} » joue encore.",
        departure_homepod_fix="Mettre le HomePod « {name} » en pause",
        departure_player_playing="🔊 « {name} » joue encore.",
        departure_player_fix="Mettre « {name} » en pause",
    ),
}


def strings(language: str) -> Strings:
    return LANGUAGES.get(language, LANGUAGES["en"])
