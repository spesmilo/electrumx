#!/usr/bin/env python3
"""
GitHub Actions job: Claude Code review of ElectrumX pull requests.

Runs Claude Code in ultracode mode against the PR diff. Claude orchestrates a small
multi-agent workflow (security review and general code review in parallel, then a
verification pass that deduplicates the findings and tries to refute them) and writes
one report. The report is printed to the log and to the job summary.

Must be run from the root of the PR checkout, with the PR head checked out.

Exit codes:
    0 -- PASS (no blocking findings; non-blocking findings may still be reported)
    1 -- FAIL (blocking findings: critical/high security issues, or practically
         relevant bugs/regressions found by the code review)
    2 -- review could not run, or its result could not be parsed

Environment variables:
    Required:
        CLAUDE_CODE_OAUTH_TOKEN  -- OAuth token from `claude setup-token`
    Set by the workflow:
        PR_NUMBER                -- PR number (empty if not a PR build)
        BASE_BRANCH              -- target branch of the PR (default: master)
        REFERENCE_DIR            -- checkouts of electrum and electrum-protocol, for Claude to read
    Set by GitHub Actions runtime (optional):
        GITHUB_EVENT_NAME        -- "workflow_dispatch" for manual runs
        GITHUB_STEP_SUMMARY      -- file the report is appended to
        RUNNER_TEMP              -- where the PR diff is written for Claude
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(SCRIPT_DIR, "claude_review_prompt.md")
# (description, subdirectory of REFERENCE_DIR)
REFERENCE_SOURCES = (
    ("Electrum client", "electrum"),
    ("protocol spec", "electrum-protocol/docs"),
)

CLAUDE_TIMEOUT_SECONDS = 100 * 60
CLAUDE_MODEL = "claude-opus-5-5"
# ultracode: xhigh effort, and Claude orchestrates subagents via the Workflow tool
CLAUDE_EFFORT = "ultracode"
CLAUDE_SETTINGS = {
    # advisory: "small" aims for fewer than 5 agents per workflow
    "workflowSizeGuideline": "small",
    # Hide Claude Code's built-in review skills, our prompt replaces them. E.g. security-review
    # excludes DoS issues, which are central for a server.
    "skillOverrides": {"security-review": "off", "code-review": "off", "simplify": "off"},
}

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
BLOCKING_SEVERITIES = ("CRITICAL", "HIGH")
# finding headings of the output format, e.g. "### [HIGH] Unbounded loop in ...", tolerating markdown variations
FINDING_RE = re.compile(r"^[ \t]{0,3}#{2,4}[ \t]*[*_]*\[(CRITICAL|HIGH|MEDIUM|LOW)\][*_]*[ \t]*(.*)$",
                        re.MULTILINE | re.IGNORECASE)
# a line like "VERDICT: PASS", also e.g. "**VERDICT:** PASS" or "`VERDICT: FAIL`"
VERDICT_RE = re.compile(r"[#>*_`\s]*VERDICT[*_`\s]*:[*_`\s]*(PASS|FAIL)[*_`.\s]*", re.IGNORECASE)

# stream-json system events emitted when Claude Code switches to another model,
# e.g. after a refusal by the cyber safeguards
FALLBACK_EVENT_SUBTYPES = ("model_refusal_fallback", "model_fallback")
# model name of assistant messages generated locally by Claude Code, not by the API
SYNTHETIC_MODEL = "<synthetic>"


def git(*args: str) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout


def fetch_base_branch(base: str) -> None:
    git("fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    # Shallow clones may lack the history needed for the three-dot diff (merge-base computation).
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        git("fetch", "--no-tags", "--unshallow", "origin")


def write_pr_files(base: str) -> dict[str, str] | None:
    """Writes the PR's diff, changed files and commit messages to files outside the checkout.

    Claude reads them from there instead of getting them in the prompt: the prompt would
    expand "@path" file mentions, and the PR content could break out of markdown code fences.
    Returns the file paths, or None if the diff is empty.
    """
    diff = git("diff", f"origin/{base}...HEAD")
    if not diff.strip():
        return None
    out_dir = tempfile.mkdtemp(prefix="claude-review-", dir=os.environ.get("RUNNER_TEMP") or None)
    contents = {
        "diff": diff,
        "files": git("diff", "--stat=200", f"origin/{base}...HEAD"),
        "commits": git("log", f"origin/{base}..HEAD"),
    }
    paths = {}
    for name, text in contents.items():
        paths[name] = os.path.join(out_dir, f"pr-{name}.txt")
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(text)
    print_untrusted(contents["files"])
    return paths


def reference_sources() -> str:
    """User prompt line with the paths of the reference checkouts that exist (none when run locally)."""
    ref_dir = os.environ.get("REFERENCE_DIR", "").strip()
    if not ref_dir:
        return ""
    paths = [(name, os.path.join(ref_dir, sub)) for name, sub in REFERENCE_SOURCES]
    refs = [f"{name} `{path}`" for name, path in paths if os.path.isdir(path)]
    if not refs:
        return ""
    return f"- Reference sources (trusted, see \"Tools and references\" in the guidelines): {', '.join(refs)}\n"


def build_user_prompt(*, pr: str, head: str, base: str, manual: bool, pr_files: dict[str, str]) -> str:
    with open(pr_files["diff"], encoding="utf-8") as f:
        diff_lines = sum(1 for _ in f)
    trigger = (
        "This review was started manually; that is how PRs by external contributors are reviewed."
        if manual else
        "This PR is by a maintainer or collaborator of the repository."
    )
    return (
        f"Review pull request #{pr} according to the review guidelines in your system prompt.\n\n"
        f"- {trigger}\n"
        f"- The PR head commit {head} is checked out in the current working directory. "
        f"The PR targets `{base}`.\n"
        f"- The PR content is in these files (untrusted, see the ground rules):\n"
        f"  - diff ({diff_lines} lines): `{pr_files['diff']}`, "
        f"the output of `git diff origin/{base}...HEAD`\n"
        f"  - changed files: `{pr_files['files']}`\n"
        f"  - commit messages: `{pr_files['commits']}`\n"
        f"- The review guidelines (your system prompt) are also in `{PROMPT_FILE}`, "
        f"so subagents can read them.\n"
        f"{reference_sources()}"
        f"- The review is stopped after {CLAUDE_TIMEOUT_SECONDS // 60} minutes, and then nothing "
        f"is reported. Aim to write the report within an hour; check the time with `date`.\n"
    )


class ProgressPrinter:
    """Prints a short line for what Claude does, so the CI log shows that the review is alive."""
    PROGRESS_INTERVAL_SECONDS = 60

    def __init__(self) -> None:
        self.last_progress = 0.0

    def __call__(self, msg: dict) -> None:
        msg_type, subtype = msg.get("type"), msg.get("subtype")
        if msg_type == "assistant" and msg.get("parent_tool_use_id") is None:
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    self.print(f"{block.get('name')}: {self.tool_detail(block)}")
        elif msg_type == "system" and subtype == "task_started":
            self.print(f"started {msg.get('task_type')}: {msg.get('workflow_name') or msg.get('description')}")
        elif msg_type == "system" and subtype == "task_notification":
            self.print(f"{msg.get('status')}: {msg.get('summary')}")
        elif msg_type == "system" and subtype == "task_progress":
            now = time.monotonic()
            if now - self.last_progress >= self.PROGRESS_INTERVAL_SECONDS:
                self.last_progress = now
                usage = msg.get("usage") or {}
                self.print(f"  ... {msg.get('description')} ({usage.get('tool_uses')} tool uses, "
                           f"{usage.get('total_tokens')} tokens so far)")

    @staticmethod
    def tool_detail(block: dict) -> str:
        tool_input = block.get("input") or {}
        if block.get("name") == "Workflow":
            match = re.search(r"name:\s*['\"]([^'\"]+)", tool_input.get("script") or "")
            return match.group(1) if match else str(tool_input.get("name") or "")
        for key in ("command", "file_path", "pattern", "description"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
        return ""

    @staticmethod
    def print(line: str) -> None:
        print("[claude] " + " ".join(line.split())[:200], flush=True)


def _feed_stdin(stream, text: str) -> None:
    try:
        stream.write(text)
        stream.close()
    except OSError:
        pass  # claude exited early; reported via its exit code


def run_claude(user_prompt: str, system_prompt: str) -> list[dict] | None:
    """Invoke Claude Code CLI in print mode. Returns the stream-json messages, or None on failure.

    Passes the prompt via stdin to avoid OS argument length limits (MAX_ARG_STRLEN).
    """
    cmd = [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--model", CLAUDE_MODEL,
        "--effort", CLAUDE_EFFORT,
        "--settings", json.dumps(CLAUDE_SETTINGS),
        # Don't load settings (e.g. hooks) and MCP servers from the PR checkout:
        # they would run commands before Claude even looks at the PR.
        "--setting-sources", "user",
        "--strict-mcp-config",
        # all results go into the report, not to this findings tool of the code review skill
        "--disallowedTools", "ReportFindings",
        "--output-format", "stream-json",
        "--verbose",  # required by stream-json in print mode
        "--append-system-prompt", system_prompt,
    ]
    env = dict(os.environ)
    # In print mode, Claude Code stops background work (the review workflow) 10 minutes after
    # the main agent's last turn by default. Wait for it; CLAUDE_TIMEOUT_SECONDS bounds the run.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    # Workflow agents mostly wait for the API. The default limit (number of CPUs - 2) would only
    # let 2 of them run concurrently on GitHub's 4 vCPU runners.
    env["CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"] = "4"
    # Don't attach the contents of files "@mentioned" in the prompt.
    env["CLAUDE_CODE_DISABLE_FILE_MENTIONS"] = "1"
    # Stay on the CLI version the workflow installed (one released at least a week ago).
    env["DISABLE_AUTOUPDATER"] = "1"

    try:
        # own process group, so that on timeout we can also kill its children (which may hold stdout open)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                encoding="utf-8", errors="replace", start_new_session=True)
    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found. Is @anthropic-ai/claude-code installed?")
        return None
    # feed stdin from a thread, so that a full stdout pipe can't deadlock us
    threading.Thread(target=_feed_stdin, args=(proc.stdin, user_prompt), daemon=True).start()
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited

    timer = threading.Timer(CLAUDE_TIMEOUT_SECONDS, kill)
    timer.start()
    messages = []
    print_progress = ProgressPrinter()
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                messages.append(msg)
                print_progress(msg)
        returncode = proc.wait()
    finally:
        timer.cancel()

    if timed_out.is_set():
        print(f"ERROR: Claude Code timed out after {CLAUDE_TIMEOUT_SECONDS}s.")
        return None
    if returncode != 0:
        print(f"ERROR: Claude Code exited with code {returncode}")
        # the reason (e.g. invalid token, usage limit, API error) is only in the result messages
        for msg in messages:
            if msg.get("type") == "result" and msg.get("is_error"):
                print(f"  {result_error(msg)}")
        return None
    return messages


def result_error(msg: dict) -> str:
    return str(msg.get("result") or "; ".join(map(str, msg.get("errors") or [])) or msg.get("subtype"))


def parse_stream_output(messages: list[dict]) -> dict:
    """Extract the review text, model downgrades and run stats from stream-json messages.

    Downgrades are detected from the fallback events Claude Code emits, and as a
    backstop from the model that actually served each main-thread response.
    With background workflows there are several turns, each ending with a result
    message; the last one contains the final report.
    """
    run = {
        "review": None,
        "downgrades": [],
        "workflow_tool_available": None,
        "workflows_started": 0,
        "cost_usd": None,
        "safety_stops": 0,
    }
    fallback_models = set()
    served_models = set()
    for msg in messages:
        msg_type = msg.get("type")
        if msg_type == "system" and msg.get("subtype") == "init":
            run["workflow_tool_available"] = "Workflow" in (msg.get("tools") or [])
        elif msg_type == "system" and msg.get("subtype") in FALLBACK_EVENT_SUBTYPES:
            reason = msg.get("api_refusal_category") or msg.get("trigger") or "unknown reason"
            run["downgrades"].append(f"{msg.get('original_model')} -> {msg.get('fallback_model')} ({reason})")
            fallback_models.add(msg.get("fallback_model"))
        elif msg_type == "assistant" and msg.get("parent_tool_use_id") is None:
            message = msg.get("message", {})
            served_models.add(message.get("model"))
            run["workflows_started"] += sum(
                1 for block in message.get("content", [])
                if block.get("type") == "tool_use" and block.get("name") == "Workflow"
            )
        elif msg_type == "result":
            if msg.get("is_error"):
                print(f"ERROR: Claude Code reported an error: {result_error(msg)}")
                run["review"] = None
            else:
                run["review"] = msg.get("result")
            # cumulative over the session, so the last result counts
            run["cost_usd"] = msg.get("total_cost_usd", run["cost_usd"])
            run["safety_stops"] = msg.get("safety_stops") or 0
    unexpected_models = served_models - fallback_models - {CLAUDE_MODEL, SYNTHETIC_MODEL, None}
    for model in sorted(unexpected_models):
        run["downgrades"].append(f"{CLAUDE_MODEL} -> {model} (served without a fallback event)")
    return run


def parse_verdict(review: str) -> str | None:
    for line in reversed(review.strip().splitlines()):
        match = VERDICT_RE.fullmatch(line.strip())
        if match:
            return match.group(1).upper()
    return None


def decide(review: str) -> tuple[str | None, list[str], int]:
    """Returns (verdict, blocking finding titles, number of findings).

    Any finding with a blocking severity fails the review, even if the verdict line says otherwise.
    """
    findings = [(severity.upper(), title.strip()) for severity, title in FINDING_RE.findall(review)]
    blocking = [f"[{severity}] {title}" for severity, title in findings if severity in BLOCKING_SEVERITIES]
    verdict = parse_verdict(review)
    if blocking and verdict != VERDICT_FAIL:
        print(f"WARNING: verdict line is {verdict}, but there are blocking findings. Treating as {VERDICT_FAIL}.")
        verdict = VERDICT_FAIL
    elif verdict == VERDICT_FAIL and not blocking:
        print("WARNING: verdict line is FAIL, but no CRITICAL/HIGH finding headings were found.")
    return verdict, blocking, len(findings)


def print_untrusted(text: str) -> None:
    """Print text that may contain lines like "::error::", without GitHub running them as workflow commands."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}")
    print(text)
    print(f"::{token}::")


def annotation(level: str, title: str, message: str) -> None:
    """Print a GitHub Actions annotation, shown on the workflow run summary page."""
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{message}")


def write_job_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def main() -> int:
    separator = "=" * 60

    print(separator)
    print("Claude Code Review (security + code review)")
    print(separator)

    pr = os.environ.get("PR_NUMBER", "").strip()
    if not pr:
        print("Not a PR build (PR_NUMBER is empty). Skipping.")
        return 0

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        print("ERROR: CLAUDE_CODE_OAUTH_TOKEN is not set.")
        return 2

    base = os.environ.get("BASE_BRANCH", "").strip() or "master"
    try:
        head = git("rev-parse", "HEAD").strip()
        print(f"PR #{pr} at {head} -> base branch: {base}")
        print("\nFetching base branch...")
        fetch_base_branch(base)
        pr_files = write_pr_files(base)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: {exc}\n{exc.stderr}")
        return 2
    if pr_files is None:
        print("Empty diff -- nothing to review.")
        return 0

    user_prompt = build_user_prompt(
        pr=pr, head=head, base=base, pr_files=pr_files,
        manual=os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
    )
    with open(PROMPT_FILE, encoding="utf-8") as f:
        system_prompt = f.read()

    print(f"\nRunning Claude Code review (model: {CLAUDE_MODEL}, effort: {CLAUDE_EFFORT}, "
          f"settings: {json.dumps(CLAUDE_SETTINGS)})...\n", flush=True)
    start = time.monotonic()
    messages = run_claude(user_prompt, system_prompt)
    minutes = (time.monotonic() - start) / 60
    if messages is None:
        print("Review failed to produce output.")
        return 2
    run = parse_stream_output(messages)
    review = run["review"]
    if not review:
        print("Review failed to produce output.")
        return 2

    print(separator)
    print("REVIEW OUTPUT")
    print(separator)
    print_untrusted(review)
    print(separator)

    notes = []
    if run["downgrades"]:
        notes.append(f"Model downgrade detected, the review was not (fully) done by {CLAUDE_MODEL}: "
                     f"{'; '.join(run['downgrades'])}. Review quality may be lower than expected.")
        annotation("warning", "Claude review model downgraded", "; ".join(run["downgrades"]))
    if run["safety_stops"]:
        notes.append(f"{run['safety_stops']} model call(s) were stopped by a safety system, possibly in "
                     f"workflow agents: some review work may have been refused or done by a fallback model.")
        annotation("warning", "Claude review safety stops", notes[-1])
    if run["workflows_started"] == 0:
        availability = {True: "available but not used", False: "not available"}.get(
            run["workflow_tool_available"], "availability unknown")
        notes.append(f"No multi-agent workflow was run (Workflow tool {availability}); "
                     f"the review was done by a single agent.")
    for note in notes:
        print(f"NOTE: {note}")

    stats = f"{CLAUDE_MODEL}, effort {CLAUDE_EFFORT}, {run['workflows_started']} workflow(s), {minutes:.0f} min"
    if run["cost_usd"] is not None:
        stats += f", estimated API cost ${run['cost_usd']:.2f}"
    print(f"Run: {stats}")

    verdict, blocking, num_findings = decide(review)
    if verdict == VERDICT_FAIL:
        result_line = f"**Result: {VERDICT_FAIL}** -- " + (
            f"{len(blocking)} blocking finding(s)" if blocking else "the review's verdict is FAIL, see below")
    elif verdict == VERDICT_PASS:
        result_line = f"**Result: {VERDICT_PASS}** -- {num_findings} non-blocking finding(s)"
    else:
        result_line = "**Result: unknown** -- could not parse the verdict, see the review below"
    summary = f"## Claude Code review: PR #{pr} @ `{head[:12]}`\n\n{result_line}\n\n"
    summary += "".join(f"> [!WARNING]\n> {note}\n\n" for note in notes)
    summary += f"{review}\n\n---\n*{stats}*\n"
    write_job_summary(summary)

    if verdict == VERDICT_FAIL:
        print(f"\nVERDICT: {VERDICT_FAIL} -- blocking findings:")
        for title in blocking:
            print(f"  {title}")
        annotation("error", "Claude review found blocking issues",
                   "; ".join(blocking) or "see the job summary")
        return 1
    if verdict == VERDICT_PASS:
        print(f"\nVERDICT: {VERDICT_PASS} -- no blocking findings ({num_findings} non-blocking finding(s)).")
        if num_findings:
            annotation("notice", "Claude review passed with remarks",
                       f"{num_findings} non-blocking finding(s), see the job summary")
        return 0

    print("\nWARNING: Could not parse verdict from review output.")
    print("Review logged above for manual inspection.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # exit code 1 means "blocking findings", so report unexpected errors as 2
        traceback.print_exc()
        sys.exit(2)
