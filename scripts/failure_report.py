"""Turn a red run into an email that says what broke and whether to act.

GitHub's own failure email says "calibrate failed (3 annotations)" and
nothing else, so every red run meant opening the log. This reads the
captured output of the failed step, names each failure in plain English
with a verdict (self-heals / needs you / code bug), and files it as a
GitHub issue that @mentions the owner - the issue email carries the
diagnosis.

One incident is one issue, not one email per run: the first failure opens
it, repeats only edit the body (edits send no email), and the next green
run of the same workflow closes it with a recovery comment. Roughly two
emails per incident instead of one every 30 minutes.

    python scripts/failure_report.py fail <workflow> <log>
    python scripts/failure_report.py recovered <workflow>

Stdlib only, so it still runs when `pip install` is the step that broke.
The repo is public and so are its issues: every line quoted from the log
goes through redact() first.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

OWNER = "rob211"
TITLE_PREFIX = "[scanner]"
FP_RE = re.compile(r"<!-- fp:(\S+) -->")

SELF_HEALS = "Self-heals - no action unless it is still open in a day."
NEEDS_YOU = "Needs you."
CODE_BUG = "Code bug - needs a fix in the repo."
DECISION = "Your call - nothing is broken, a correction has drifted."


@dataclass
class Finding:
    key: str  # stable across repeats of the same fault; drives dedup
    title: str
    meaning: str
    action: str
    evidence: str


def _stale_feed(name: str, m: re.Match) -> tuple[str, str]:
    return (
        f"{name} feed not updating",
        f"{name}'s newest reading is {m.group(1)} min old. The station or "
        "its publisher has stalled; the scanner refuses to judge live wind "
        "on old data rather than guess.",
    )


# (pattern, key, builder) - first match per line wins. Builders return
# (title, meaning, action). Order matters: specific before generic.
RULES: list[tuple[re.Pattern, str, object]] = [
    (
        re.compile(r"last successful scan was ([\d.]+) h ago"),
        "scan_dead",
        lambda m: (
            f"No successful scan for {float(m.group(1)):.0f} h",
            "Live itself is fine; it fails because the forecast it checks "
            "against is too old. This is a symptom - the cause is whatever "
            "is breaking the scan job (see the open scan issue). If there "
            "is none, scans are not being triggered at all: check the "
            "Cloudflare Worker and its GitHub token.",
            NEEDS_YOU + " Fix the scan and this clears with it.",
        ),
    ),
    (
        re.compile(r"GitHub PAT EXPIRED"),
        "pat_expired",
        lambda m: (
            "GitHub token for the Cloudflare Worker has expired",
            "The Worker can no longer trigger runs; only GitHub's sparse "
            "backstop schedule is running.",
            NEEDS_YOU + " Roll the token at "
            "github.com/settings/personal-access-tokens, then "
            "`wrangler secret put GITHUB_TOKEN` in cron-worker/.",
        ),
    ),
    (
        re.compile(r"BOM observations.*?latest reading is (\d+) min old"),
        "bom_stale",
        lambda m: (*_stale_feed("BOM Bellambi", m), SELF_HEALS),
    ),
    (
        re.compile(r"Holfuy \d+: latest reading is (\d+) min old"),
        "holfuy_stale",
        lambda m: (*_stale_feed("Holfuy lake station", m), SELF_HEALS),
    ),
    (
        re.compile(r"wave buoy: newest reading is (\d+) min old"),
        "buoy_stale",
        lambda m: (*_stale_feed("Port Kembla wave buoy", m), SELF_HEALS),
    ),
    (
        re.compile(r"no marine data for (\S+)"),
        "marine_gap",
        lambda m: (
            "Wind hour with no matching swell data",
            f"A trigger asked for swell at {m.group(1)} and the marine feed "
            "had no row for it. Fixed in PR #35; if this is back, a new "
            "code path is doing an unguarded marine lookup.",
            CODE_BUG,
        ),
    ),
    (
        # Greedy .* so the innermost cause wins: a timeout reads
        # "HTTPSConnectionPool(...): Read timed out", and the pool is noise.
        re.compile(r"SOURCE FAILED (\S+): .*(\b[45]\d\d [A-Za-z ]+?(?=:| for url|$)|timed out|Connection(?:Error|Refused|Reset)\w*)"),
        "source_down",
        lambda m: (
            f"Data source down: {m.group(1)}",
            f"{m.group(1)} failed with '{m.group(2).strip()}' after every retry. "
            "An outage at the provider (usually Open-Meteo); the rest of the "
            "scan still ran and existing calendar events were kept.",
            SELF_HEALS,
        ),
    ),
    (
        re.compile(r"(?:SchemaError|StaleDataError): ([^:]+): (forecast only reaches|forecast ends|forecast starts)"),
        "forecast_truncated",
        lambda m: (
            f"{m.group(1)} returned a short or stale forecast",
            "The provider served an old or truncated response. Usually a "
            "cache hiccup at their end.",
            SELF_HEALS,
        ),
    ),
    (
        re.compile(r"SchemaError: (.+)"),
        "schema",
        lambda m: (
            "A data feed returned something the scanner doesn't understand",
            f"{m.group(1)}. Either the provider changed its format or the "
            "code is missing a case. Fails loud on purpose: guessing here is "
            "how a calm-looking week hides a broken scanner.",
            CODE_BUG + " (or a provider format change)",
        ),
    ),
    (
        re.compile(r"(invalid_grant|unauthorized_client|PERMISSION_DENIED|HttpError 40[13]|notFound)"),
        "gcal_auth",
        lambda m: (
            "Google Calendar refused the scanner",
            f"Calendar API said {m.group(1)}. The service-account key may "
            "have been revoked/rotated, or the Foiling calendar is no longer "
            "shared with it.",
            NEEDS_YOU + " Check GCAL_SERVICE_ACCOUNT_JSON and the calendar's sharing.",
        ),
    ),
    (
        re.compile(r"CalendarError: (.+)"),
        "calendar",
        lambda m: (
            "Calendar sync problem",
            m.group(1),
            "Check live.json notes; usually a window whose event failed to sync.",
        ),
    ),
    (
        re.compile(r"ConfigError: (.+)"),
        "config",
        lambda m: (
            "Configuration error",
            m.group(1),
            NEEDS_YOU + " A secret is missing or a config constant is invalid.",
        ),
    ),
    (
        re.compile(r"(lake|coast): measured ([\d.+-]+) against a configured ([\d.+-]+) \((\d+)% off\)"),
        "drift_wind_{0}",
        lambda m: (
            f"Wind calibration drift ({m.group(1)})",
            f"Measured correction {m.group(2)} vs {m.group(3)} configured "
            f"({m.group(4)}% off). The constant in config.py no longer "
            "matches what the stations show.",
            DECISION + " Review WIND_BIAS in config.py.",
        ),
    ),
    (
        re.compile(r"DRIFTED: tide timing residual ([+-]?\d+) min"),
        "drift_tide",
        lambda m: (
            "Tide timing calibration drift",
            f"Modelled tides are {m.group(1)} min off the Port Kembla gauge "
            "with the current offset applied.",
            DECISION + " Re-run calibrate_tide.py and update TIDE_TIME_OFFSET_MIN.",
        ),
    ),
    (
        re.compile(r"tide height ([+-]?[\d.]+) against a configured ([\d.+-]+)"),
        "drift_tide_height",
        lambda m: (
            "Tide height calibration drift",
            f"Gauge says the datum offset is {m.group(1)} m; {m.group(2)} is configured.",
            DECISION + " Update TIDE_HEIGHT_OFFSET_M in config.py.",
        ),
    ),
]

# Traceback context quotes the source, e.g. `raise SchemaError(f"no marine
# data for {t}")` - matching that would report the template, not the value.
_CODE_LINE = re.compile(r"^\s*(?:File \"|raise |return |[\^~ ]+$)")

TRACEBACK_TAIL = re.compile(r"^(?:\w+\.)*(\w+(?:Error|Exception)): (.+)$")


_SECRET_PARAM = re.compile(r"((?:pw|key|token|password|secret)=)[^&\s'\"]+", re.I)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    text = _SECRET_PARAM.sub(r"\1***", text)
    # Service-account / calendar ids are not keys, but nothing public needs them.
    text = re.sub(r"[\w.-]+@[\w-]+\.iam\.gserviceaccount\.com", "<service-account>", text)
    text = re.sub(r"[\w.-]+@group\.calendar\.google\.com", "<calendar-id>", text)
    return text


def diagnose(log: str) -> list[Finding]:
    found: dict[str, Finding] = {}
    for raw in log.splitlines():
        if _CODE_LINE.match(raw):
            continue
        # The calibrate scripts put several drifts on one line, "; "-joined.
        for line in raw.strip().split("; "):
            for pat, key, build in RULES:
                m = pat.search(line)
                if m:
                    key = key.format(*m.groups())
                    if key not in found:
                        title, meaning, action = build(m)
                        found[key] = Finding(key, title, meaning, action, raw.strip())
                    break
    if found:
        return list(found.values())

    # Nothing recognised: fall back to the last exception line, which is
    # still more than GitHub's email says.
    last = None
    for line in log.splitlines():
        m = TRACEBACK_TAIL.match(line.strip())
        if m:
            last = (m.group(1), m.group(2), line.strip())
    if last:
        return [Finding(
            "unknown_" + last[0].lower(),
            f"Unrecognised failure: {last[0]}",
            last[1],
            "Not one of the known failure types - look at the run log.",
            last[2],
        )]
    return [Finding(
        "unknown",
        "Run failed with no recognisable error",
        "The step produced no exception line. Usually an infrastructure "
        "problem (pip install, git push, runner) rather than the scanner.",
        "Open the run log.",
        "",
    )]


def _tail(log: str, n: int = 25) -> str:
    lines = [l for l in log.splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def render(workflow: str, findings: list[Finding], log: str, run_url: str,
           first_seen: str, last_seen: str, count: int, related: list[int]) -> str:
    out = [f"@{OWNER} the **{workflow}** job is failing.", ""]
    for f in findings:
        out += [f"### {f.title}", f.meaning, "", f"**Action:** {f.action}", ""]
        if f.evidence:
            out += ["```", f.evidence[:500], "```", ""]
    if related:
        out += ["Related open issue(s): " + ", ".join(f"#{n}" for n in related), ""]
    out += [
        f"- First failed: {first_seen}",
        f"- Last failed: {last_seen} ([run]({run_url}))",
        f"- Failed runs so far: {count}",
        "",
        "This issue closes itself on the next green run of this workflow.",
        "",
        "<details><summary>Last lines of the log</summary>",
        "",
        "```",
        _tail(log),
        "```",
        "</details>",
    ]
    return "\n".join(out)


# ------------------------------------------------------------------ github

def _gh(*args: str, body: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], input=body, text=True, capture_output=True, check=True
    ).stdout


def _repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def _open_issues() -> list[dict]:
    out = _gh("api", f"repos/{_repo()}/issues?state=open&per_page=100")
    return [i for i in json.loads(out)
            if i["title"].startswith(TITLE_PREFIX) and "pull_request" not in i]


def _fp(issue: dict) -> str | None:
    m = FP_RE.search(issue.get("body") or "")
    return m.group(1) if m else None


def _meta(issue: dict, name: str, default: str) -> str:
    m = re.search(rf"<!-- {name}:(\S+) -->", issue.get("body") or "")
    return m.group(1) if m else default


def report_failure(workflow: str, log: str) -> None:
    findings = diagnose(log)
    fp = f"{workflow}:" + "+".join(sorted(f.key for f in findings))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{_repo()}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

    issues = _open_issues()
    mine = next((i for i in issues if _fp(i) == fp), None)
    related = [i["number"] for i in issues
               if i is not mine and not (_fp(i) or "").startswith(workflow + ":")]

    first_seen = _meta(mine, "first", now.replace(" ", "_")).replace("_", " ") if mine else now
    count = int(_meta(mine, "count", "0")) + 1 if mine else 1
    body = render(workflow, findings, log, run_url, first_seen, now, count, related)
    body += (f"\n<!-- fp:{fp} -->\n<!-- first:{first_seen.replace(' ', '_')} -->"
             f"\n<!-- count:{count} -->\n")

    if mine:
        # Edit, don't comment: an edit sends no email, so a fault that
        # repeats every 30 min stays one email until it recovers.
        _gh("api", "-X", "PATCH", f"repos/{_repo()}/issues/{mine['number']}",
            "-F", "body=@-", body=body)
        print(f"updated #{mine['number']} ({count} failures)")
    else:
        title = f"{TITLE_PREFIX} {workflow}: " + "; ".join(f.title for f in findings)
        out = _gh("api", f"repos/{_repo()}/issues", "-f", f"title={title[:250]}",
                  "-F", "body=@-", body=body)
        print(f"opened #{json.loads(out)['number']}: {title}")


def report_recovered(workflow: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for i in _open_issues():
        if not (_fp(i) or "").startswith(workflow + ":"):
            continue
        count = _meta(i, "count", "?")
        first = _meta(i, "first", "?").replace("_", " ")
        _gh("api", f"repos/{_repo()}/issues/{i['number']}/comments", "-F", "body=@-",
            body=f"Recovered at {now}: {workflow} is green again after "
                 f"{count} failed run(s) since {first}.")
        _gh("api", "-X", "PATCH", f"repos/{_repo()}/issues/{i['number']}",
            "-f", "state=closed", "-f", "state_reason=completed")
        print(f"closed #{i['number']}")


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "recovered":
        report_recovered(argv[1])
        return 0
    if len(argv) >= 3 and argv[0] == "fail":
        workflow, path = argv[1], argv[2]
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                log = fh.read()
        except FileNotFoundError:
            log = ""
        secrets = [v for k, v in os.environ.items() if k.startswith("REDACT_")]
        report_failure(workflow, redact(log, secrets))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
