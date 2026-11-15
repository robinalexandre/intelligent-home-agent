# Documents, PDFs and pictures

The agent can make files and hand them to you: a letter as a PDF, a form filled in, a few
pages taken out of a long PDF, a diagram, a short animation. With a key of your own it can
also ask an image model for a picture or a clip, and run calculations in a sandbox.

| What | Tools | On when | Leaves your machine |
|---|---|---|---|
| The conversation's files | `list_saved_files`, `read_saved_file` | `DOCUMENTS_ENABLED` (default) | no |
| PDFs it writes | `create_pdf` | same, with the `documents` extra | no |
| PDF forms | `read_pdf_layout`, `fill_pdf` | same | no |
| PDF pages | `copy_pdf_pages` | same | no |
| Drawings and animations | `draw_image`, `make_animation` | same | no |
| Exercise photos | `find_exercise_photos`, `save_exercise_photos` | `EXERCISE_PHOTOS_ENABLED=true` | a download from GitHub |
| Generated pictures and clips | `generate_image`, `generate_video` | a `GENERATION_*` key | the prompt and the pictures you name, to Google or OpenAI |
| Calculations and charts | `run_code` | `CODE_EXECUTION_ENABLED=true`, Anthropic only | the task and the files you name, to Anthropic |

A file made by the agent comes back in the `files` list of the `/v1/ask` reply, to download
from `GET /v1/files/{name}`. Files live as long as the conversation
(`CONVERSATION_TTL_SECONDS`, `CHAT_CONVERSATION_TTL_SECONDS`): there is no long-term
storage yet, so download what you want to keep.

## Installing

The Docker image has everything for the first five rows: the `documents` extra and the
DejaVu fonts. Without Docker:

```bash
pip install -e ".[documents]"          # fpdf2, pypdf, pdfminer.six, resvg-py
pip install -e ".[documents,video]"    # also MP4 animations (PyAV, some 40 MB)
```

Without the extra the agent starts as usual and those tools are simply not there.
Without `video`, animations are GIFs. To change what the image holds:

```bash
docker compose build --build-arg EXTRAS=documents,video      # MP4 animations
docker compose build --build-arg EXTRAS= --build-arg FONT_PACKAGES=   # none of it
```

`DOCUMENTS_ENABLED=false` turns the local tools off whatever is installed.

## PDFs the agent writes

"Write a letter to my landlord about the leak, as a PDF." The model writes simple HTML
(headings, paragraphs, lists, tables, images already in the conversation) and the server
lays it out. Nothing else gets through: no link, style or address can load anything.

- **Paper**: `PAPER_SIZE` is `a4`, `letter` or `auto`. `auto` picks US Letter when
  `HOME_COUNTRY` is a country where it is the usual paper (the United States, Canada,
  Mexico, the Philippines and some of Latin America), A4 everywhere else.
- **Layout, dates, addresses**: the code has no template. The model is told the language
  (`LANGUAGE`) and the country (`HOME_COUNTRY`) and lays a letter out the way it is done
  there: where the sender and the recipient go, how the date and the amounts are
  written, the usual closing. Ask for another convention in your request when you need
  one ("in German, for a German office").
- **Fonts**: DejaVu Sans, which covers Latin with accents, Greek, Cyrillic and currency
  signs. For another script, put a `Regular` and a `Bold` file of one family (`.ttf` or
  `.otf`, e.g. `NotoSansArabic-Regular.ttf` and `NotoSansArabic-Bold.ttf`) in a folder and
  set `PDF_FONT_DIR` to it; in Docker, mount the folder. Drawings use the same fonts.
  With no font at all (a Python install on a machine without that folder), PDFs fall
  back to Helvetica, characters outside Latin-1 become `?`, and a drawing's text is not
  drawn: install DejaVu (`fonts-dejavu-core` on Debian and Ubuntu) or set `PDF_FONT_DIR`.
  Right-to-left and complex scripts are not shaped by the PDF writer: check the
  result.
- The agent does not write an invoice or a receipt in a vendor's place.

## PDF forms

Send a form and ask the agent to fill it. A PDF with real form fields gets them filled.
Most official forms are "paper" PDFs with dotted lines and boxes: the agent reads where
the text, the blanks and the boxes sit, writes over the pages, looks at the result and
corrects it before handing it over. The original is never changed. It leaves blank what
it doesn't know, never signs, and tells you what is left. Forms of more than 20 pages
are refused: ask for the pages you need first.

## Pages

"Send me only page 2", "merge these three PDFs": a new PDF is made from the pages you
want, in the order you want, up to 200 pages. A PDF protected by a password it would have
to type is refused.

## Drawings, animations and exercise photos

When a picture says it better, the agent writes SVG and the server renders it to PNG: a
plan, a schedule, a chart, a position. An SVG can load nothing from outside; it may only
show images already in the conversation. Several frames make an animation: an MP4 with
the `video` extra, a GIF without.

`EXERCISE_PHOTOS_ENABLED=true` adds a bank of some 870 gym and home exercises with a photo
of their start and end positions
([free-exercise-db](https://github.com/yuhonas/free-exercise-db), public domain), read
from GitHub at a pinned version. The bank is in English; the photos are kept under the
name you use.

## Generated pictures and clips

A language model writes no pictures. For a photo-like image or a real video clip the
agent calls an image model, on an account of yours:

| Provider | Setting | Pictures | Clips |
|---|---|---|---|
| Google (a key from [AI Studio](https://aistudio.google.com/apikey)) | `GENERATION_GEMINI_API_KEY` | Gemini's image model | Veo |
| OpenAI (a key from [platform.openai.com](https://platform.openai.com/api-keys)) | `GENERATION_OPENAI_API_KEY` | gpt-image | Sora |
| A server that speaks OpenAI's images API, yours or hosted | `GENERATION_OPENAI_BASE_URL` (a key only if it wants one) | its model | set `GENERATION_OPENAI_VIDEO_MODEL=off` |

One is enough. With both, the model can pick; `GENERATION_DEFAULT_PROVIDER` says which
one otherwise. The model names are settings (`GENERATION_GEMINI_IMAGE_MODEL`...), since
providers rename them often; a model set to `off` removes that kind for that provider.
These keys are separate from `LLM_API_KEY`, even when the provider is the same, so that
nothing is billed without you asking for it. Each is billed per picture or per second of
video on that provider's account: check their price list.

What goes out: the prompt, and the pictures you name as a reference. The prompt is
checked like a web search query (it may not repeat a number, an address or a run of words
from what the request read); only pictures you sent or the agent made can go along, never
one that came from a mailbox or a web page; and a request makes at most
`GENERATION_MAX_IMAGES` pictures and `GENERATION_MAX_VIDEOS` clips.

## Calculations and charts

`CODE_EXECUTION_ENABLED=true` gives the agent `run_code`: a loan's monthly payments, a
budget, a table totalled from a CSV you sent, a chart. The Python runs in the model
provider's sandbox, never on your machine, and sees only the task and the files named.

This needs a code execution tool and a file store on the provider's side, which only
Anthropic's API has among the supported providers: with any other `LLM_PROVIDER` the
setting does nothing. It is billed on the same key as the model. A run on a file written
by someone else taints the request like reading it.

## What it may and may not do

- Reading a kept file or a form taints the request, like an email: nothing acts on the
  home or leaves it afterwards without your go.
- The PDF and SVG renderers load nothing: no address, no path on the server.
- Originals are never changed; a filled form or a copy is a new file.
- It never signs for you, and never writes a vendor's invoice or receipt.
