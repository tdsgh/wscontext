"""Split a Markdown file into sections: one per ATX heading, plus any text before the first."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass
class Section:
    heading: str
    line: int  # 1-based line of the heading; 1 for the text before the first heading
    text: str


def sections(text: str, path: str) -> list[Section]:
    """Sections of `text`. A heading outside a fenced code block starts one; it runs to the next
    heading of any level. Text before the first heading is named by the file name. A section
    with neither heading text nor body is dropped."""
    out: list[Section] = []
    heading, line, body = PurePosixPath(path).name, 1, []
    fence = ""

    def close() -> None:
        content = "\n".join(body).strip()
        if heading or content:
            out.append(Section(heading, line, content))

    started = False  # a real heading was seen
    for n, raw in enumerate(text.splitlines(), 1):
        fenced = _FENCE.match(raw)
        if fence:
            if fenced and fenced.group(1)[0] == fence[0] and len(fenced.group(1)) >= len(fence):
                fence = ""
            body.append(raw)
            continue
        if fenced:
            fence = fenced.group(1)
            body.append(raw)
            continue
        m = _HEADING.match(raw)
        if m:
            if started or "\n".join(body).strip():
                close()
            heading, line, body, started = m.group(1).strip(), n, [], True
        else:
            body.append(raw)
    if started or "\n".join(body).strip():
        close()
    return out
