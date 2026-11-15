import json

from intelligent_home_agent.progress import Tracker


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def tracker(tmp_path, clock, sent, language="en", **kwargs):
    async def send(text):
        sent.append(text)

    return Tracker(send, tmp_path / "durations.json", language, clock=clock, **kwargs)


async def test_one_line_each_quarter_for_every_running_sub_task(tmp_path):
    clock, sent = Clock(), []
    news = tracker(tmp_path, clock, sent, min_gap=0)
    review = news.begin("analysis", "Report A", 400, lambda: "🔎 12")
    news.begin("analysis", "Report B", 400, waiting=True)

    await news.check()
    assert sent == []  # nothing crossed yet
    clock.now = 100
    await news.check()
    clock.now = 110
    await news.check()  # still the same quarter: no new line
    assert sent == ["⏳ Report A: ~25% (2 min of ~7), 🔎 12 · Report B: waiting"]

    clock.now = 500
    await news.check()
    assert sent[-1] == "⏳ Report A: longer than expected, 8 min, 🔎 12 · Report B: waiting"
    review.end()
    assert news.line() == "⏳ Report B: waiting"
    french = tracker(tmp_path, clock, sent, "fr")
    french.begin("analysis", "Rapport", 400, waiting=True)
    assert french.line() == "⏳ Rapport : en attente"


async def test_lines_are_spaced_out_and_none_come_once_all_ended(tmp_path):
    clock, sent = Clock(), []
    news = tracker(tmp_path, clock, sent, min_gap=120)
    run = news.begin("job", "Job 1a2b", 400)

    for now in (100, 200, 230, 300):
        clock.now = now
        await news.check()

    # 25 % at 100 s; 50 % at 200 s waits for the gap and goes with 75 % at 230 s.
    assert len(sent) == 2 and "~55%" in sent[1]
    run.end()
    clock.now = 1000
    await news.check()
    assert len(sent) == 2


async def test_estimates_come_from_the_last_runs_of_their_kind(tmp_path):
    clock, sent = Clock(), []
    news = tracker(tmp_path, clock, sent)
    assert news.estimate("analysis", 360) == 360

    for took in (200, 300, 400):
        run = news.begin("analysis", "x", 360)
        clock.now += took
        run.end()
    failed = news.begin("analysis", "x", 360)
    clock.now += 5000
    failed.end(ok=False)  # a failed run says nothing of how long they take

    assert news.estimate("analysis", 360) == 300
    assert json.loads((tmp_path / "durations.json").read_text()) == {"analysis": [200, 300, 400]}
    # A run that waited for a slot counts from when it started.
    queued = news.begin("analysis", "x", 360, waiting=True)
    clock.now += 1000
    queued.go()
    clock.now += 100
    queued.end()
    assert news.estimate("analysis", 360) == 250
