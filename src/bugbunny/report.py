from __future__ import annotations

from typing import Any


def render_markdown(artifact: dict[str, Any]) -> str:
    pr = artifact["pr"]
    findings = artifact.get("findings", [])
    coverage = artifact.get("coverage", {})
    calls = artifact.get("calls", [])
    lines = [
        "# BugBunny review",
        "",
        f"PR: [{pr['owner']}/{pr['repo']}#{pr['number']}]({pr['url']})",
        f"Status: `{artifact['status']}`",
        f"Model: `{artifact['config']['model']}`",
        f"Base/head: `{pr['base_sha'][:12]}` → `{pr['head_sha'][:12]}`",
        (
            "Coverage: "
            f"{len(set(coverage.get('completed_hunks', [])))}/"
            f"{coverage.get('eligible_hunks', 0)} eligible hunks"
        ),
        f"Duration: {artifact.get('duration_ms', 0) / 1000:.1f}s; model calls: {len(calls)}",
        "",
    ]
    if not findings:
        lines.extend(["No publishable defects found.", ""])
    else:
        lines.extend([f"## Findings ({len(findings)})", ""])
        for index, finding in enumerate(findings, start=1):
            lines.extend(
                [
                    f"### {index}. {finding['title']}",
                    "",
                    (
                        f"`{finding['severity']}` · `{finding['category']}` · "
                        f"[{finding['path']}:{finding['line']} ({finding.get('side', 'RIGHT')})](#)"
                    ),
                    "",
                    f"Trigger: {finding['trigger']}",
                    "",
                    f"Impact: {finding['impact']}",
                    "",
                    f"Evidence: `{finding['evidence']}`",
                    "",
                ]
            )
            if finding.get("suggested_fix"):
                lines.extend([f"Suggested direction: {finding['suggested_fix']}", ""])
    rejected = artifact.get("rejected_findings", [])
    if rejected:
        lines.extend(
            [
                "## Audit trail",
                "",
                f"{len(rejected)} candidate(s) were retained in JSON but excluded from publication.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
