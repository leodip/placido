"""Checks on what `placido pr`'s agents write: the scan, each reviewer's findings and
cross-check, the judge's final review, and the check of the fixes, each problem worded
for the agent to fix. Placido sends them back to the agent until its files pass, so the
rest of `placido pr` reads well-formed files only. The judge's check also holds it to
the rules: every reviewer finding settled exactly once, and the verdict following
`[pr] contributor_share`.

The agents write JSON alone, and placido renders it for people (`render_review`,
`render_final`): #588's agents wrote every finding twice, once in Markdown, and the
judge's Markdown alone ran to 171 lines (decided 2026-10-16).

The judge's open questions are the user's to decide, every one, before anything else
happens to the pull request: what they decide is applied to the final review
(`decided_final`), so the fix and its check work from it, whether or not it is what the
judge recommended (decided 2026-10-16).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

AREAS = ("correctness", "gaps", "bugs", "security", "standards", "tests")
SEVERITIES = ("blocking", "significant", "minor")
SIZES = ("small", "medium", "large")
CROSS = ("confirmed", "refuted", "unsure", "duplicate")
EARLIER = ("addressed", "partly", "not-addressed")
TESTS = ("yes", "partly", "no", "n/a")
MUTATIONS = ("killed", "survived", "timeout")
AGREEMENT = ("all", "confirmed", "disputed", "unsure", "single")
VERDICTS = ("merge", "fix-here", "request-changes", "close")
ROUTES = ("here", "contributor", "none")
CHECKED = ("resolved", "unresolved", "accepted-dispute", "rejected-dispute")
STILL_OPEN = ("unresolved", "rejected-dispute")
ROUTE_OF = {"fix-here": "here", "request-changes": "contributor", "merge": "none", "close": "none"}
ADDED = ("title", "area", "severity", "size", "location", "description", "fix")  # a finding a decision adds


def load(path: Path) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, f"{path} is missing."
    except ValueError as error:
        return None, f"{path.name} is not valid JSON: {error}."


def texts(item: dict[str, Any], *keys: str) -> bool:
    return all(isinstance(item.get(k), str) and item[k].strip() for k in keys)


def check_scan(path: Path) -> list[str]:
    data, error = load(path)
    if error:
        return [error]
    if not isinstance(data, dict):
        return ["scan.json must be an object."]
    problems = []
    if data.get("verdict") not in ("clear", "concerns"):
        problems.append('scan.json needs "verdict": "clear" or "concerns".')
    concerns = data.get("concerns")
    if not isinstance(concerns, list):
        return problems + ['scan.json needs a "concerns" list (empty when clear).']
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        return problems + ['scan.json\'s "notes" must be a list (empty when there are none).']
    for kind, items in (("concern", concerns), ("note", notes)):
        for n, item in enumerate(items, 1):
            if not isinstance(item, dict) or not texts(item, "title", "why"):
                problems.append(f"{kind} {n} needs a non-empty 'title' and 'why'.")
            elif not isinstance(item.get("location", ""), str):
                problems.append(f"{kind} {n} has a 'location' that is not a string.")
    if data.get("verdict") == "concerns" and not concerns:
        problems.append('a "concerns" verdict needs at least one concern.')
    if data.get("verdict") == "clear" and concerns:
        problems.append('a "clear" verdict lists no concerns; use "concerns" instead.')
    return problems


def check_review(path: Path, code: str, earlier: list[str]) -> list[str]:
    """Problems with a reviewer's findings.json, worded for the reviewer to fix."""

    data, error = load(path)
    if error:
        return [error]
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return ['findings.json needs a "findings" list (empty when there are none).']
    problems = findings_problems(data["findings"], code)
    tests = data.get("tests")
    if not isinstance(tests, dict) or tests.get("fail_without_fix") not in TESTS:
        problems.append(f'findings.json needs "tests" with "fail_without_fix" one of {", ".join(TESTS)}.')
    else:
        mutations = tests.get("mutations", [])
        if not isinstance(mutations, list):
            problems.append('"tests.mutations" must be a list.')
        else:
            for n, mutation in enumerate(mutations, 1):
                if (not isinstance(mutation, dict) or mutation.get("outcome") not in MUTATIONS
                        or not texts(mutation, "location", "change")):
                    problems.append(
                        f"mutation {n} needs a location, a change, and an outcome of {', '.join(MUTATIONS)}."
                    )
    for key in ("standards", "preexisting"):
        if not isinstance(data.get(key, []), list):
            problems.append(f'"{key}" must be a list.')
    answered = {e.get("item"): e for e in data.get("earlier", []) if isinstance(e, dict)} \
        if isinstance(data.get("earlier", []), list) else {}
    for item in earlier:
        entry = answered.get(item)
        if entry is None or entry.get("status") not in EARLIER or not texts(entry, "note"):
            problems.append(f"earlier item {item} needs an entry in \"earlier\" with a status of"
                            f" {', '.join(EARLIER)} and a note.")
    return problems


def findings_problems(findings: list[Any], code: str) -> list[str]:
    """Problems with a list of findings whose ids run from <code>1."""

    problems = []
    for n, finding in enumerate(findings, 1):
        where = f"finding {n}"
        if not isinstance(finding, dict):
            problems.append(f"{where} is not an object.")
            continue
        if finding.get("id") != f"{code}{n}":
            problems.append(f"{where} has id {finding.get('id')!r}; it should be {code}{n}.")
        for key, allowed in (("area", AREAS), ("severity", SEVERITIES), ("size", SIZES)):
            if finding.get(key) not in allowed:
                problems.append(f"{where} has {key} {finding.get(key)!r}; use {', '.join(allowed)}.")
        if not texts(finding, "title", "description", "evidence", "fix"):
            problems.append(f"{where} needs a non-empty title, description, evidence, and fix.")
        if not isinstance(finding.get("location", ""), str):
            problems.append(f"{where} has a location that is not a string.")
    return problems


def check_crosscheck(path: Path, others: list[str], own: list[str]) -> list[str]:
    data, error = load(path)
    if error:
        return [error]
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        return ['crosscheck.json needs a "verdicts" list.']
    problems, seen = [], []
    for entry in verdicts:
        if not isinstance(entry, dict) or entry.get("verdict") not in CROSS or not texts(entry, "note"):
            problems.append(f"verdict {entry!r} needs an id, a verdict of {', '.join(CROSS)}, and a note.")
            continue
        if entry.get("id") not in others:
            problems.append(f"{entry.get('id')!r} is not another reviewer's finding.")
            continue
        if entry["verdict"] == "duplicate" and entry.get("duplicate_of") not in own:
            problems.append(f"{entry['id']} is a duplicate: name which of yours in 'duplicate_of' ({', '.join(own) or 'none'}).")
        seen.append(entry["id"])
    for finding_id in others:
        count = seen.count(finding_id)
        if count != 1:
            problems.append(f"{finding_id} needs exactly one verdict; it has {count}.")
    return problems


def check_final(folder: Path, ids: list[str], earlier: list[str], share: float) -> list[str]:
    """Problems with the judge's files: well formed, every reviewer finding accounted for
    exactly once, and the verdict following the routing rule."""

    data, error = load(folder / "final.json")
    if error:
        return [error]
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return ['final.json needs a "findings" list.']
    problems = []
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r}: use {', '.join(VERDICTS)}.")
    if not texts(data, "summary"):
        problems.append('final.json needs a non-empty "summary".')
    sources: list[str] = []
    kept = []
    for n, finding in enumerate(data["findings"], 1):
        where = f"finding {n}"
        if not isinstance(finding, dict):
            problems.append(f"{where} is not an object.")
            continue
        if finding.get("id") != f"F{n}":
            problems.append(f"{where} has id {finding.get('id')!r}; it should be F{n}.")
        for key, allowed in (("area", AREAS), ("severity", SEVERITIES), ("size", SIZES),
                             ("agreement", AGREEMENT), ("decision", ("keep", "drop"))):
            if finding.get(key) not in allowed:
                problems.append(f"{where} has {key} {finding.get(key)!r}; use {', '.join(allowed)}.")
        if not texts(finding, "title", "description", "fix", "reason"):
            problems.append(f"{where} needs a non-empty title, description, fix, and reason.")
        found = finding.get("sources")
        if not isinstance(found, list) or not found or not all(s in ids for s in found):
            problems.append(f"{where} needs 'sources': the reviewer findings it settles, from {', '.join(ids) or 'none'}.")
        else:
            sources += found
        if finding.get("decision") == "keep":
            kept.append(finding)
    for finding_id in ids:
        count = sources.count(finding_id)
        if count != 1:
            problems.append(f"reviewer finding {finding_id} must be the source of exactly one final finding; it is of {count}.")
    routing = data.get("routing")
    if not isinstance(routing, dict) or routing.get("route") not in ROUTES:
        problems.append(f'final.json needs "routing" with a "route" of {", ".join(ROUTES)}.')
    else:
        problems += routing_problems(routing, verdict, kept, share)
    answered = {e.get("item"): e for e in data.get("earlier", []) if isinstance(e, dict)} \
        if isinstance(data.get("earlier", []), list) else {}
    for item in earlier:
        entry = answered.get(item)
        if entry is None or entry.get("status") not in EARLIER:
            problems.append(f"earlier item {item} needs an entry in \"earlier\" with a status of {', '.join(EARLIER)}.")
    for key in ("decisions", "preexisting"):
        if not isinstance(data.get(key, []), list):
            problems.append(f'"{key}" must be a list.')
    for n, item in enumerate(data.get("decisions", []) if isinstance(data.get("decisions", []), list) else [], 1):
        if not isinstance(item, dict) or not texts(item, "question", "recommendation"):
            problems.append(f"decision {n} needs a non-empty question and recommendation.")
    if not (folder / "comment.md").is_file() or not (folder / "comment.md").read_text(encoding="utf-8").strip():
        problems.append(f"{folder / 'comment.md'} is missing or empty.")
    if verdict in ("merge", "fix-here"):
        subject = first_line(folder / "merge.md")
        if not subject:
            problems.append(f"{folder / 'merge.md'} needs the squash commit's subject on its first line.")
        elif len(subject) > 72:
            problems.append(f"the squash subject is {len(subject)} characters; keep it to 72.")
    return problems


def routing_problems(routing: dict[str, Any], verdict: Any, kept: list[dict[str, Any]], share: float) -> list[str]:
    route, measured = routing.get("route"), routing.get("share")
    if verdict == "close":
        return [] if route == "none" else ['a "close" verdict routes nothing: use route "none".']
    if not kept:
        problems = [] if route == "none" else ['with no finding kept, the route is "none".']
        return problems + ([] if verdict == "merge" else ['with no finding kept, the verdict is "merge" (or "close").'])
    if type(measured) not in (int, float) or measured < 0:
        return ['"routing.share" must be the fixes\' size as a fraction of the pull request\'s, such as 0.2.']
    if not texts(routing, "estimate"):
        return ['"routing.estimate" must say how the share was estimated.']
    wanted = "contributor" if measured >= share else "here"
    problems = [] if route == wanted else [
        f"a share of {measured:g} against the threshold of {share:g} routes to {wanted!r}, not {route!r}."
    ]
    verdict_for = {"here": "fix-here", "contributor": "request-changes"}[wanted]
    if verdict != verdict_for:
        problems.append(f'kept findings routed {wanted!r} make the verdict "{verdict_for}" (or "close").')
    return problems


def decision_items(final: dict[str, Any]) -> list[dict[str, Any]]:
    """The judge's open questions, each as a dict, whatever form an older judge wrote."""

    return [copy.deepcopy(d) if isinstance(d, dict) else {"question": str(d), "recommendation": ""}
            for d in final.get("decisions", [])]


def check_decided(folder: Path, final: dict[str, Any]) -> list[str]:
    """Problems with the user's decisions as the decide step wrote them: one answer per
    open question, in order; what each changes naming kept findings; a verdict that
    fits the findings left; and the drafts it brought in line."""

    data, error = load(folder / "decided.json")
    if error:
        return [error]
    if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
        return ['decided.json needs an "answers" list.']
    asked = decision_items(final)
    problems = []
    if len(data["answers"]) != len(asked):
        problems.append(f"the final review has {len(asked)} decision(s), and decided.json answers"
                        f" {len(data['answers'])}: one answer per decision, in the final review's order.")
    kept = [f["id"] for f in final["findings"] if f.get("decision") == "keep"]
    touched: list[str] = []
    for n, answer in enumerate(data["answers"], 1):
        where = f"answer {n}"
        if not isinstance(answer, dict):
            problems.append(f"{where} is not an object.")
            continue
        if not texts(answer, "question", "decision", "why"):
            problems.append(f"{where} needs a non-empty question, decision, and why.")
        if not isinstance(answer.get("as_recommended"), bool):
            problems.append(f'{where} needs "as_recommended": true or false.')
        fixes, drop, add = answer.get("fixes", {}), answer.get("drop", []), answer.get("add", [])
        if not isinstance(fixes, dict) or not all(isinstance(v, str) and v.strip() for v in fixes.values()):
            problems.append(f'{where}: "fixes" maps a kept finding\'s id to its fix as decided.')
            fixes = {}
        if not isinstance(drop, list):
            problems.append(f'{where}: "drop" is a list of kept findings\' ids.')
            drop = []
        for finding_id in [*fixes, *drop]:
            if finding_id not in kept:
                problems.append(f"{where} names {finding_id!r}, which is no kept finding ({', '.join(kept) or 'none'}).")
            elif finding_id in touched:
                problems.append(f"{finding_id} is changed twice; say in one answer what becomes of it.")
            touched.append(finding_id)
        if not isinstance(add, list):
            problems.append(f'{where}: "add" is a list of findings.')
            continue
        for m, item in enumerate(add, 1):
            problems += added_problems(item, f"{where}'s added finding {m}")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r}: use {', '.join(VERDICTS)}.")
    if problems:
        return problems
    left = [f["id"] for f in decided_final(final, data)["findings"] if f["decision"] == "keep"]
    if verdict == "merge" and left:
        problems.append(f'a "merge" verdict fixes nothing, but {", ".join(left)} are still kept: drop them,'
                        ' or keep the verdict that fixes them.')
    if verdict in ("fix-here", "request-changes") and not left:
        problems.append(f'no finding is left to fix, so the verdict is "merge" or "close", not "{verdict}".')
    if not (folder / "comment.md").is_file() or not (folder / "comment.md").read_text(encoding="utf-8").strip():
        problems.append(f"{folder / 'comment.md'} is missing or empty.")
    if verdict in ("merge", "fix-here"):
        subject = first_line(folder / "merge.md")
        if not subject:
            problems.append(f"{folder / 'merge.md'} needs the squash commit's subject on its first line.")
        elif len(subject) > 72:
            problems.append(f"the squash subject is {len(subject)} characters; keep it to 72.")
    return problems


def added_problems(item: Any, where: str) -> list[str]:
    if not isinstance(item, dict):
        return [f"{where} is not an object."]
    problems = [] if texts(item, "title", "description", "fix") else [f"{where} needs a non-empty title, description, and fix."]
    for key, allowed in (("area", AREAS), ("severity", SEVERITIES), ("size", SIZES)):
        if item.get(key) not in allowed:
            problems.append(f"{where} has {key} {item.get(key)!r}; use {', '.join(allowed)}.")
    return problems


def decided_final(final: dict[str, Any], decided: dict[str, Any]) -> dict[str, Any]:
    """The final review with the user's decisions applied: each question carries its
    decision; a finding fixed another way, dropped, or added says which decision did it;
    and a verdict the user changed routes the fixes, keeping the judge's beside it."""

    data = copy.deepcopy(final)
    by_id = {f["id"]: f for f in data["findings"]}
    items = decision_items(data)
    for n, (item, answer) in enumerate(zip(items, decided["answers"]), 1):
        item["decided"] = {key: answer[key] for key in ("question", "decision", "why", "as_recommended")}
        because = f"Your decision {n}: {answer['decision']}"
        for finding_id, fix in answer.get("fixes", {}).items():
            by_id[finding_id].update(fix=fix, decided_by=n)
        for finding_id in answer.get("drop", []):
            by_id[finding_id].update(decision="drop", reason=because, decided_by=n)
        for added in answer.get("add", []):
            data["findings"].append({
                **{key: added[key] for key in ADDED if key in added}, "id": f"F{len(data['findings']) + 1}",
                "sources": [f"decision {n}"], "agreement": "yours", "decision": "keep", "reason": because,
                "decided_by": n,
            })
    data["decisions"] = items
    if decided["verdict"] != data["verdict"]:
        data["routing"] = {**data["routing"], "route": ROUTE_OF[decided["verdict"]], "judge_verdict": data["verdict"]}
        data["verdict"] = decided["verdict"]
    return data


def first_line(path: Path) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return lines[0].strip() if lines else ""


def check_verify(path: Path, ids: list[str]) -> list[str]:
    """Problems with a check of the fixes: one verdict for each finding the fixer
    answered, and any new problem the fixes brought as a finding V1, V2, …"""

    data, error = load(path)
    if error:
        return [error]
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return ['verify.json needs a "verdicts" list.']
    problems, seen = [], []
    for entry in data["verdicts"]:
        if not isinstance(entry, dict) or entry.get("verdict") not in CHECKED or not texts(entry, "note"):
            problems.append(f"verdict {entry!r} needs an id, a verdict of {', '.join(CHECKED)}, and a note.")
            continue
        seen.append(entry.get("id"))
    for finding_id in ids:
        count = seen.count(finding_id)
        if count != 1:
            problems.append(f"{finding_id} needs exactly one verdict; it has {count}.")
    for finding_id in sorted({i for i in seen if i not in ids}):
        problems.append(f"{finding_id!r} is not a finding the fix answered ({', '.join(ids)}).")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return problems + ['"findings" must be a list (empty when the fixes brought no new problem).']
    return problems + findings_problems(findings, "V")


def verify_passed(data: dict[str, Any]) -> bool:
    """Every finding resolved, or its dispute accepted, and no new blocking or
    significant problem."""

    open_ = [v for v in data["verdicts"] if v["verdict"] in STILL_OPEN]
    serious = [f for f in data.get("findings", []) if f.get("severity") != "minor"]
    return not open_ and not serious


# ---- for people -------------------------------------------------------------------------


def _finding_lines(finding: dict[str, Any], extra: str = "") -> list[str]:
    where = f" · `{finding['location']}`" if finding.get("location") else ""
    lines = [
        f"### {finding['id']}. {finding['title']}",
        "",
        f"{finding['severity']} · {finding['area']} · {finding.get('size', '?')}{where}{extra}",
        "",
        finding.get("description", "").strip(),
    ]
    if finding.get("evidence"):
        lines += ["", f"**Evidence:** {finding['evidence'].strip()}"]
    if finding.get("fix"):
        decided = f" (your decision {finding['decided_by']})" if finding.get("decided_by") and finding.get("decision") == "keep" else ""
        lines += ["", f"**Fix{decided}:** {finding['fix'].strip()}"]
    if finding.get("reason"):
        lines += ["", f"**Why {'kept' if finding.get('decision') == 'keep' else 'dropped'}:** {finding['reason'].strip()}"]
    return lines + [""]


def render_review(data: dict[str, Any], code: str) -> str:
    """A reviewer's findings.json, for people."""

    findings = data.get("findings", [])
    lines = [f"# Reviewer {code}: {len(findings)} finding(s)", ""]
    for finding in findings:
        lines += _finding_lines(finding)
    tests = data.get("tests") or {}
    lines += ["## Tests", "", f"Fail without the fix: {tests.get('fail_without_fix', '?')}. {tests.get('note', '')}".strip(), ""]
    for mutation in tests.get("mutations", []):
        lines.append(f"- {mutation.get('outcome')}: `{mutation.get('location')}`, {mutation.get('change')}")
    for key, title in (("standards", "Standards read"), ("preexisting", "Already there before this change")):
        if data.get(key):
            lines += ["", f"## {title}", ""]
            for item in data[key]:
                label = item.get("standard") or item.get("title") or "?"
                note = item.get("note") or item.get("description") or ""
                lines.append(f"- **{label}:** {note}")
    if data.get("earlier"):
        lines += ["", "## The round before", ""]
        lines += [f"- {e.get('item')}: {e.get('status')}. {e.get('note', '')}" for e in data["earlier"]]
    return "\n".join(lines).rstrip() + "\n"


def render_final(data: dict[str, Any]) -> str:
    """The judge's final.json, for people."""

    routing = data.get("routing") or {}
    judged = f" (yours; the judge's was {routing['judge_verdict']})" if routing.get("judge_verdict") else ""
    lines = [f"# Verdict: {data['verdict']}{judged}", "", data.get("summary", "").strip(), ""]
    if routing.get("route") and routing["route"] != "none":
        lines += [f"Fixes go to: {routing['route']} (share {routing.get('share')}). {routing.get('estimate', '')}".strip(), ""]
    kept = [f for f in data["findings"] if f.get("decision") == "keep"]
    dropped = [f for f in data["findings"] if f.get("decision") != "keep"]
    for title, group in (("Kept", kept), ("Dropped", dropped)):
        if group:
            lines += [f"## {title}", ""]
            for finding in group:
                lines += _finding_lines(finding, f" · from {', '.join(finding.get('sources', []))} ({finding.get('agreement')})")
    if data.get("decisions"):
        lines += ["## Decisions for the maintainer", ""]
        for n, item in enumerate(decision_items(data), 1):
            line = f"{n}. {item['question']} **Recommendation:** {item['recommendation']}"
            decided = item.get("decided")
            if decided:
                taken = "as recommended" if decided["as_recommended"] else "not as recommended"
                line += f" **Decided ({taken}):** {decided['decision']} **Why:** {decided['why']}"
            lines.append(line)
        lines.append("")
    if data.get("preexisting"):
        lines += ["## Already there before this change (for the maintainer only)", ""]
        for item in data["preexisting"]:
            flag = " [security]" if item.get("security") else ""
            lines.append(f"- **{item.get('title')}**{flag} (`{item.get('location', '')}`): {item.get('description', '')}")
        lines.append("")
    if data.get("earlier"):
        lines += ["## The round before", ""]
        lines += [f"- {e.get('item')}: {e.get('status')}. {e.get('note', '')}" for e in data["earlier"]]
    return "\n".join(lines).rstrip() + "\n"
