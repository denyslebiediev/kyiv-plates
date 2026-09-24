#!/usr/bin/env python3
"""List available Kyiv license plates whose 4-digit block is 0000-0099.

Source: https://opendata.hsc.gov.ua/check-leisure-license-plates/ (ГСЦ МВС).
The site's `number` field is an exact *numeric* match, not a prefix ('26' == '0026',
'00' -> 0 -> nothing), so there is no range query. Submitting it EMPTY instead returns
the whole region's inventory in one POST; we filter 00xx locally. Do not loop
0000..0099 - ~100 requests trips Akamai and soft-bans the IP site-wide (see README).
No dependencies - system Python and the curl that ships with macOS.
"""
import csv
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

URL = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
REGION = "26"
REGION_LABEL = "м. Київ"
TSC = "Весь регіон"                 # literal option text, not an id
VTYPE = "light_car_and_truck"       # the site's only car category is the combined
VTYPE_LABEL = "Легковий"            # "Легковий, вантажний" - no passenger-only option exists,
                                    # so this shorter label is cosmetic; results include trucks
MAX_DIGITS = 100                    # keep plates with digits 0000..MAX_DIGITS-1
MIN_TOTAL = 500                     # Kyiv always lists thousands; fewer means the page changed
MAX_TIME = 25                       # curl --max-time, seconds (wall-clock - see _curl). A healthy
                                    # bulk answer is ~1.5 s and the slowest real one measured was
                                    # 14.2 s, so this gives up 35 s before Akamai's own 60 s
                                    # gateway 504 - the sweep below is a better use of that time.
RETRY_WAIT = 5                      # first backoff; doubles twice over -> 5 s, 20 s
SWEEP_PAUSE = 1                     # seconds between exact-number requests - see fetch_numbers
SLEPT_TOLERANCE = 5                 # wall/monotonic divergence that means "we were suspended"
BLOCK_TEXT = "Please try loading the page later"   # Akamai's soft-ban page, served 200
ABSENT_TEXT = "КОМБІНАЦІЄЮ ВІДСУТНІЙ"            # the origin's own bail-out page - see _rows


class MachineSlept(Exception):
    """The machine slept mid-request, so curl's "timeout" is an artifact, not evidence.

    Distinct from RuntimeError on purpose: a run the laptop slept through learned nothing
    about the site either way, and callers must not count it as a failure. This is not
    exotic - it produced 7 of the first 9 failures ever logged by the watcher.
    """

# Akamai needs BOTH to answer 200:
#  * HTTP/2 - over HTTP/1.1 the same request 403s, which is why this shells out to curl
#    instead of using urllib (stdlib speaks HTTP/1.1 only). requests/httpx aren't installed.
#  * a realistic Chrome header set. There is no JS challenge, so no browser automation needed.
HEADERS = {
    # Bump the Chrome major here and in sec-ch-ua *together*, and keep it roughly current.
    # August 2026 logged two Akamai IP blocks - 20 h 12 m and 9 h 29 m, blind throughout - on a
    # header set still claiming Chrome/131 (Nov 2024). A 20-month-stale UA is exactly what bot
    # scoring penalises; this is the only lever we have on that, and it costs nothing.
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": '"Chromium";v="139", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}
POST_HEADERS = {
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://opendata.hsc.gov.ua",
    "referer": URL,
    "sec-fetch-site": "same-origin",   # overrides the GET's "none"
}
# ponytail: regex over a fixed server-side template (parsed 6092/6092 rows, zero misses).
# If the markup ever changes, switch to bs4 + lxml - both already installed.
ROW_RE = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", re.S)
PLATE_RE = re.compile(r"\D{2}(\d{4})\D{2}")  # \D, not [A-Z]: plates may use Cyrillic homoglyphs
TOKEN_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
STAMP_RE = re.compile(r'<span class="mr-5"><a>(.*?)</a>')

HERE = Path(__file__).resolve().parent


def _curl(jar, form=None):
    """One curl call sharing `jar` as both cookie source and sink. Raises on any HTTP error.

    macOS suspends this process mid-request whenever the laptop sleeps, and curl's
    --max-time is wall-clock, so on wake it reports a "timeout" of however long the nap
    was - 1063949 ms against a 60 s limit, in the logged case. time.monotonic() stops
    during sleep while time.time() doesn't, so their divergence measures the nap exactly:
    verified at 0.0000 s apart while awake, and 65 h apart across this machine's uptime.
    """
    cmd = ["curl", "-sS", "--http2", "--compressed", "--fail", "--max-time", str(MAX_TIME),
           "-c", jar, "-b", jar]
    headers = dict(HEADERS, **POST_HEADERS) if form else HEADERS
    for key, value in headers.items():
        cmd += ["-H", "%s: %s" % (key, value)]
    for key, value in (form or {}).items():
        cmd += ["--data-urlencode", "%s=%s" % (key, value)]   # curl handles the UTF-8 tsc
    cmd.append(URL)
    wall, awake = time.time(), time.monotonic()
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    slept = (time.time() - wall) - (time.monotonic() - awake)
    if done.returncode:
        # Only reinterpret a *failure*: a fetch that survived a nap is still good data.
        if slept > SLEPT_TOLERANCE:
            raise MachineSlept("machine slept %.0f s mid-request" % slept)
        raise RuntimeError("curl failed (%d): %s"
                           % (done.returncode, done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout.decode("utf-8", "replace")


def fetch(attempts=3):
    """GET for Akamai cookies + CSRF token, then POST with an empty number -> whole region.

    Retried, because one blip otherwise burns a whole 30-minute watcher slot - both real
    failures ever logged had healed by the next run. MachineSlept escapes only when
    *every* attempt was suspended; any failure we were awake for outranks it, because
    that one is actual evidence about the site.
    ponytail: nowhere near the Akamai limit - the ban in the README came from 100
    requests in 9.2 s, this is at most 3 per failing run, twice an hour.
    """
    slept = last = None
    for attempt in range(attempts):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                jar = str(Path(tmp) / "cookies")
                token = TOKEN_RE.search(_curl(jar))
                if not token:
                    raise RuntimeError("no csrfmiddlewaretoken on the form page - site changed?")
                return _curl(jar, {"csrfmiddlewaretoken": token.group(1), "region": REGION,
                                   "tsc": TSC, "type_venichle": VTYPE, "number": ""})
        except MachineSlept as e:
            slept = e                  # retry straight away - we are demonstrably awake now
        except RuntimeError as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(RETRY_WAIT * 4 ** attempt)
    raise last or slept


def _sweep_one(jar, token, number, attempts):
    """One exact number's rows, or None when we never got a readable answer.

    Never invents []. A number we could not read is not a number that is unavailable, and
    evaluate() compares against the last delivered snapshot - so a fabricated gap reads as
    "that plate was bought" and sends a wrong message. Saying None instead lets the caller
    carry the number forward, which is the honest answer; see fetch_numbers.

    The ABSENT_TEXT page runs at 30-45% during the day and arrives in bursts, so the retries
    are spaced out (1, 2, 3 s ...) rather than hammered a second apart - that spacing is what
    the extra attempts are actually buying.
    """
    for attempt in range(attempts):
        if attempt:
            time.sleep(SWEEP_PAUSE * attempt)
        try:
            rows = _rows(_curl(jar, {"csrfmiddlewaretoken": token, "region": REGION, "tsc": TSC,
                                     "type_venichle": VTYPE, "number": str(number)}))
        except RuntimeError:           # MachineSlept is not a RuntimeError: it escapes, as it must
            continue
        if rows is not None:
            return rows
    return None


def fetch_numbers(numbers, attempts=5):
    """Query each number exactly, sharing one GET's cookies and token.

    -> (rows as parse() returns, set of the numbers we could not read). Raises only when the
    whole session is dead - a failed GET, no CSRF token - or MachineSlept. One unreadable
    number is not a dead session, so it is reported rather than raised.

    The fallback for when the empty-`number` bulk query is down. It has been dying since
    2026-07-28: measured 2026-07-29, the whole-region POST returned HTTP 504 after 60 s on 5 of 5
    attempts, and splitting it per ТСЦ was no better, while these exact-number queries answered in
    0.4-0.7 s throughout. Re-measured 2026-08-19: unchanged - the whole region still 504s at
    60.3 s, one ТСЦ alone took 59.7 s for 522 rows. The origin is fine - it just cannot build a
    ~6100-row page inside Akamai's 60 s gateway budget any more.

    ponytail: this is the sweep README warns against, at a rate that is not the one it measured.
    The ban there came from 100 requests at concurrency 5 in 9.2 s (11 req/s); this is 24
    sequential a second apart, ~37 requests on a typical day and at most 24 x 5 = 120 spread over
    ~5.5 min (0.36 req/s). Keep it sequential, keep SWEEP_PAUSE, and do not widen it to all 100
    numbers - the shape that got banned was the rate, but the width is what made it expensive.
    """
    rows, unresolved = [], set()
    with tempfile.TemporaryDirectory() as tmp:
        jar = str(Path(tmp) / "cookies")
        token = TOKEN_RE.search(_curl(jar))
        if not token:
            raise RuntimeError("no csrfmiddlewaretoken on the form page - site changed?")
        for index, number in enumerate(numbers):
            if index:
                time.sleep(SWEEP_PAUSE)
            got = _sweep_one(jar, token.group(1), number, attempts)
            if got is None:
                unresolved.add(number)
            else:
                rows += got
    return rows, unresolved


def _rows(html):
    """Result rows, or None when the response carries no results table at all.

    Three responses are possible and only two of them are answers. The results template with an
    empty <tbody> (~92.6 KB) means "nothing for this number"; the same template with rows is a
    hit. The third, ~89 KB, is a *different* template - ABSENT_TEXT, "НОМЕРНИЙ ЗНАК З ДАННОЮ
    КОМБІНАЦІЄЮ ВІДСУТНІЙ", no table anywhere - and it is not an answer at all. Measured
    2026-08-19: number 3719, which had two plates on sale throughout, returned it 7 times in 29
    requests, while 6471 returned it once and then an empty table 14 times running. It is the
    origin bailing out early - `origin; dur=156` against ~400 ms for a real answer, with
    `cdn-cache: MISS` on every response - so no header or CDN trick avoids it and retrying is
    the only response. None says exactly that: this is not an answer, ask again.
    """
    tbody = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    if not tbody:
        return None
    return [tuple(c.strip() for c in r) for r in ROW_RE.findall(tbody.group(1))]


def parse(html):
    """-> (all rows, matching rows sorted, 'станом на' stamp). Row = (plate, price, tsc).

    A near-empty parse must fail loudly, not read as "nothing available" - but each mode
    may claim only what it can prove. "Layout or filters changed" is a strong, alarming
    claim, so it needs a table that actually exists and is short. Not getting the results
    page at all is a different and usually transient thing; conflating the two is what
    sent a false "layout changed" alert on 2026-07-26 for a site that was simply busy.
    """
    if BLOCK_TEXT in html:
        raise RuntimeError("Akamai soft-block - %r" % BLOCK_TEXT)
    rows = _rows(html)
    if rows is None:
        raise RuntimeError("the 'номерний знак відсутній' page (%d bytes) - origin bail-out"
                           % len(html) if ABSENT_TEXT in html else
                           "no results table in the %d-byte response - blocked, maintenance, "
                           "or the site changed" % len(html))
    if len(rows) < MIN_TOTAL:
        raise RuntimeError("only %d rows in the table (expected >%d) - layout or filters "
                           "changed" % (len(rows), MIN_TOTAL))
    hits = [r for r in rows
            if PLATE_RE.fullmatch(r[0]) and int(PLATE_RE.fullmatch(r[0]).group(1)) < MAX_DIGITS]
    stamp = STAMP_RE.search(html)
    return rows, sorted(hits), stamp.group(1) if stamp else "?"


def main():
    rows, hits, stamp = parse(fetch())
    print("\n0000-%04d · %s · %s · %s" % (MAX_DIGITS - 1, REGION_LABEL, TSC, VTYPE_LABEL))
    print("%d available of %d in region (станом на %s)\n" % (len(hits), len(rows), stamp))
    for plate, price, tsc in hits:
        print("  %s   %6s UAH   %s" % (plate, price, tsc))
    out = HERE / "plates_{:%Y%m%d}.csv".format(date.today())
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plate", "price_uah", "tsc"])
        w.writerows(hits)
    print("\nwrote %s" % out.name)


def self_test():
    def row(plate, price="12000", tsc="ТСЦ 8041"):
        return "<tr>\n<td>%s</td>\n  <td>%s</td>\n<td>%s</td>\n</tr>\n" % (plate, price, tsc)

    filler = "".join(row("KA%04dEP" % n) for n in range(1000, 1000 + MIN_TOTAL + 1))
    page = ('<span class="mr-5"><a>25/07/2026, 18:13:58</a></span><tbody>'
            + row("KA0099EP") + row("KA0100EP") + row("КА0042ЕР", "18000", "ТСЦ 8047")
            + row("KA0012EP") + filler + "</tbody>")
    rows, hits, stamp = parse(page)

    assert stamp == "25/07/2026, 18:13:58", stamp
    assert len(rows) == MIN_TOTAL + 5, len(rows)
    # 0099 is the last in range, 0100 the first out; Cyrillic КА0042ЕР must count
    assert [h[0] for h in hits] == ["KA0012EP", "KA0099EP", "КА0042ЕР"], hits
    assert hits[2][1:] == ("18000", "ТСЦ 8047"), hits[2]  # price/tsc carried through
    # A bad page must raise rather than report "0 available" - and each mode must name
    # itself. Only a table that exists and is short may blame the layout; asserting the
    # message, not just the raise, is what the old test missed.
    for bad, expect, forbid in [
            ("<tbody>" + row("KA0012EP") + "</tbody>", "layout or filters changed", None),
            ("<html>nothing at all</html>", "no results table", "layout"),
            ("<html>" + ABSENT_TEXT + "</html>", "відсутній", "layout"),
            ("<html>" + BLOCK_TEXT + "</html>", "Akamai soft-block", "layout")]:
        try:
            parse(bad)
        except RuntimeError as e:
            assert expect in str(e), (expect, str(e))
            assert forbid is None or forbid not in str(e), ("over-claims", str(e))
        else:
            raise AssertionError("bad page did not raise: %r" % bad[:40])

    # The load-bearing arithmetic behind MachineSlept: a curl failure spanning a nap is an
    # artifact, the same failure with the clocks in step is real. Faked, so no network.
    real_run, real_time = subprocess.run, time.time
    try:
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess([], 28, b"", b"timed out")
        ticks = iter([1000.0, 1000.0 + 900])          # wall jumps 15 min across the call...
        time.time = lambda: next(ticks)               # ...while monotonic (untouched) doesn't
        try:
            _curl("unused")
        except MachineSlept as e:
            assert "slept 900 s" in str(e), e
        else:
            raise AssertionError("a failure spanning a sleep was counted as a real failure")
        time.time = real_time
        try:
            _curl("unused")
        except RuntimeError as e:
            assert "curl failed (28)" in str(e), e
        else:
            raise AssertionError("an awake failure did not raise")
    finally:
        subprocess.run, time.time = real_run, real_time

    # --- the per-number sweep: what it must never do is answer with a gap in it ---
    real_curl, real_sleep = _curl, time.sleep
    try:
        time.sleep = lambda seconds: None      # no real backoff in a test
        asked = []

        def fake_curl(jar, form=None):
            if form is None:
                return '<input name="csrfmiddlewaretoken" value="tok"/>'
            asked.append(form["number"])
            if form["number"] == "2" and asked.count("2") == 1:
                return "<html>the same page minus the table</html>"   # one transient miss
            if form["number"] == "3":
                return "<html>the same page minus the table</html>"   # never readable
            if form["number"] == "4":
                return "<tbody></tbody>"                              # readable, nothing on sale
            if form["number"] == "5":
                return "<html>" + ABSENT_TEXT + "</html>"             # the origin's bail-out page
            return "<tbody>" + row("KA%04dEP" % int(form["number"])) + "</tbody>"

        globals()["_curl"] = fake_curl
        got, unresolved = fetch_numbers([1, 2])
        assert [g[0] for g in got] == ["KA0001EP", "KA0002EP"], got
        assert not unresolved and asked == ["1", "2", "2"], (asked, unresolved)  # 0002 retried
        # An unreadable number must be *reported*, never dropped out of the rows: the gap would
        # read as "that plate was bought" and send a wrong message. This is the silent drop
        # README warns about, and the one way the sweep could be worse than no sweep. The caller
        # carries such a number forward from the last snapshot instead of guessing about it.
        got, unresolved = fetch_numbers([1, 3])
        assert [g[0] for g in got] == ["KA0001EP"] and unresolved == {3}, (got, unresolved)
        # ...but an empty table IS a real answer - 0004 is simply not for sale - so it must not
        # be reported unresolved, and must not retry either: 20 of the 24 wanted numbers answer
        # this way every run
        asked.clear()
        assert fetch_numbers([4]) == ([], set()) and asked == ["4"], asked
        # The origin's bail-out page is the opposite: a non-answer that must never read as
        # "nothing for sale", however many times in a row it arrives. This is the 2026-08-19
        # bug - 71 runs killed in August because it was being taken at face value.
        asked.clear()
        assert fetch_numbers([5]) == ([], {5}), "the bail-out page must not read as empty"
        assert asked == ["5"] * 5, asked                 # and it is retried, not believed
    finally:
        globals()["_curl"], time.sleep = real_curl, real_sleep
    print("self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
