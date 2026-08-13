#!/usr/bin/env python
"""Build the combined design plan from the split source documents.

    python documentations/build.py

Reads the numbered documents + ADRs in this directory and writes:

    pgfoundation-fastapi-design.md    — everything in one Markdown file
    pgfoundation-fastapi-design.html  — the same, rendered for a browser

The split files are the source of truth; both outputs are generated. Edit a
split file, re-run this, and the combined artifacts stay in sync.

What the build does beyond concatenating:
  * demotes headings by two levels so the combined file has one H1 — while
    skipping anything inside a fenced code block, so YAML/bash/ini comments
    that start with '#' are not corrupted;
  * rewrites file-to-file links (./06-....md) into internal anchors (#doc-06);
  * emits an explicit <a id> before each document so those anchors resolve.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent

DOCS = [
    "01-scope-and-parity",
    "02-architecture",
    "03-design-patterns",
    "04-declarative-operation-registry",
    "05-configuration",
    "06-concurrency-batch-parallel",
    "07-performance",
    "08-api-surface",
    "09-project-structure",
    "10-testing-and-delivery-plan",
    "11-observability-instrumentation-points",
    "12-deployment-targets",
    "13-implementation-plan",
    "14-build-status",
]
ADRS = [
    "ADR-001-fastapi-service-shell",
    "ADR-002-core-ported-unchanged",
    "ADR-003-declarative-operation-registry",
    "ADR-004-batch-and-parallel-execution",
    "ADR-005-di-via-lifespan-and-depends",
    "ADR-006-streaming-delivery",
    "ADR-007-serialization-and-row-format",
    "ADR-008-auth-boundary",
]

MD_OUT = HERE / "pgfoundation-fastapi-design.md"
HTML_OUT = HERE / "pgfoundation-fastapi-design.html"
HEADER = HERE / "_header.md"          # front matter for the combined file
SHELL = HERE / "_shell.html"          # HTML template containing @@MARKDOWN@@

FENCE = re.compile(r"^```")
HEADING = re.compile(r"^(#{1,4}) ")


def demote(text: str, levels: int = 2) -> str:
    """Add `levels` to every ATX heading OUTSIDE fenced code blocks."""
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and HEADING.match(line):
            out.append("#" * levels + line)
            continue
        out.append(line)
    return "\n".join(out)


def anchor_map() -> dict[str, str]:
    """basename stem -> in-page anchor id."""
    m = {stem: f"doc-{stem.split('-')[0]}" for stem in DOCS}
    m.update({stem: stem[:7].lower() for stem in ADRS})  # ADR-001 -> adr-001
    return m


def fix_links(text: str, anchors: dict[str, str]) -> str:
    """Rewrite ](<any path>/<stem>.md) -> ](#anchor), ignoring the path prefix."""
    for stem, anc in anchors.items():
        text = re.sub(
            r"\]\([^)]*" + re.escape(stem) + r"\.md\)", f"](#{anc})", text
        )
    # The docs index has no combined-file equivalent; point at the contents block.
    return re.sub(r"\]\([^)]*documentations/README\.md\)", "](#contents)", text)


def section(path: Path, anchor: str, anchors: dict[str, str]) -> str:
    body = fix_links(demote(path.read_text(encoding="utf-8")), anchors)
    return f'\n<a id="{anchor}"></a>\n\n{body}\n\n---\n'


def main() -> int:
    for required in (HEADER, SHELL):
        if not required.exists():
            print(f"missing template: {required.name}", file=sys.stderr)
            return 1

    anchors = anchor_map()
    parts = [HEADER.read_text(encoding="utf-8").rstrip(), "\n\n## Part I — Design\n"]
    for stem in DOCS:
        parts.append(section(HERE / f"{stem}.md", anchors[stem], anchors))
    parts.append("\n## Part II — Decisions (ADRs)\n")
    for stem in ADRS:
        parts.append(section(HERE / "adr" / f"{stem}.md", anchors[stem], anchors))

    md = "\n".join(parts)
    MD_OUT.write_text(md, encoding="utf-8")

    # The markdown is embedded in <script type="text/markdown">; only a literal
    # "</script" can break out of it.
    if "</script" in md.lower():
        print("refusing to build HTML: markdown contains a script end-tag", file=sys.stderr)
        return 1
    shell = SHELL.read_text(encoding="utf-8")
    if shell.count("@@MARKDOWN@@") != 1:
        print("shell template must contain exactly one @@MARKDOWN@@", file=sys.stderr)
        return 1
    HTML_OUT.write_text(shell.replace("@@MARKDOWN@@", md), encoding="utf-8")

    print(f"{MD_OUT.name:36} {len(md)/1024:7.1f} KB  ({md.count(chr(10))+1} lines)")
    print(f"{HTML_OUT.name:36} {HTML_OUT.stat().st_size/1024:7.1f} KB")
    print(f"sections: {len(DOCS)} documents + {len(ADRS)} ADRs"
          f" · mermaid diagrams: {md.count('```mermaid')}"
          f" · OBSERVE markers: {md.count('OBSERVE (')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
