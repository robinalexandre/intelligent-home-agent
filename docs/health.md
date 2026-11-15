# Health: weigh-ins from a Withings scale

With a Withings scale connected, the agent can answer "what do I weigh?", "how much did I
lose this month?" or "how is my body fat doing?". It only reads; it never writes to
Withings.

`get_body_measurements` gives:

- the latest weigh-in, with what the scale measured: weight, fat percent and mass, lean
  and muscle mass, water, bone mass, visceral fat index, heart rate (which of these you
  get depends on the scale);
- how each changed over the last 7 and 30 days;
- every weigh-in of the period asked for (30 days by default, up to a year).

A weigh-in the scale could not tell apart from another person's is left out.

Other health data (sleep, activity, blood pressure) and other brands are not read. A
service that offers an MCP server can be added as a [connector](connectors.md) instead.

## Connect it

1. On [developer.withings.com](https://developer.withings.com), sign in with your Withings
   account and create an application (*Public API integration*). Its callback URL is the
   one the script prints, `http://localhost:8585` unless you pass `--port`.
2. On a computer with a browser:

   ```bash
   python scripts/withings_login.py
   ```

   Give it the application's client id and secret, sign in on the page it opens and
   accept reading your measures. It checks that the weigh-ins can be read and writes
   `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` and `WITHINGS_REFRESH_TOKEN` to
   `withings-credentials.env`.
3. Add those three lines to the `.env` the agent reads, restart the agent, and delete the
   file.

Withings renews the refresh token each time it is used. The agent keeps the current one
in `DATA_DIR/withings.json` (the data volume with Docker), so the value in `.env` is only
the first one: if that file is lost, run the script again. Don't use the same token from
two places.

To disconnect, remove the three settings, and revoke the access in the Withings app
(Settings > Partner apps).

## Privacy

- Weigh-ins are said only when you ask. The agent is told never to bring them up on its
  own, nor in the morning briefing or the evening recap, and to give a few rounded figures
  and the trend rather than the whole list. This is an instruction to the model, not a
  rule the code can enforce: on a shared speaker, think about who hears the answer.
- They are your own data, not public: after reading them, a web search in the same
  request waits for your go, so no query can carry them out.
- They go to the language model you chose, like everything the agent reads. With a hosted
  model that means the provider's servers; a local model keeps them at home.
- With the journal on (`JOURNAL_KEEP_DAYS`), the reply to such a question is kept in
  `DATA_DIR/journal` like any other reply.
