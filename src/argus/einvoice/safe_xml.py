"""Parse XML from strangers safely (ADR 0006 point 1, NF-02).

E-invoices arrive as email attachments, so anyone can send one. This parser refuses every XML
feature an e-invoice never needs and an attacker could use:

- A DTD (``<!DOCTYPE``). Its entities could make the value ARGUS reads differ from what a viewer
  shows, blow up memory, or reach out to files and the network. XRechnung and ZUGFeRD never
  need one, so any DTD is refused, not just a dangerous one.
- Files larger than :data:`MAX_BYTES`.

Comments and processing instructions are dropped and the text around them is joined, so a
comment in the middle of an amount cannot cut it short.
"""

from typing import Any, NoReturn
from xml.etree.ElementTree import Element, TreeBuilder
from xml.parsers import expat

# A real XRechnung is a few hundred kilobytes at most, even with many lines.
MAX_BYTES = 5 * 1024 * 1024


class XmlRefused(ValueError):
    """The XML cannot be read safely. The message says why, for the person who checks it."""


def parse(data: bytes) -> Element:
    """Parse ``data`` into an ElementTree element with ``{namespace}name`` tags."""
    if len(data) > MAX_BYTES:
        raise XmlRefused(f"the XML is larger than {MAX_BYTES // (1024 * 1024)} MB")
    builder = TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")

    def name(raw: str) -> str:
        return "{" + raw if "}" in raw else raw

    def start(tag: str, attributes: dict[str, str]) -> None:
        builder.start(name(tag), {name(key): value for key, value in attributes.items()})

    def refuse_dtd(*_: Any) -> NoReturn:
        raise XmlRefused("the XML has a DTD, which ARGUS refuses")

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda tag: builder.end(name(tag))
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = refuse_dtd
    parser.EntityDeclHandler = refuse_dtd
    parser.ExternalEntityRefHandler = refuse_dtd
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(data, True)
    except expat.ExpatError as error:
        raise XmlRefused(
            f"the XML is not well formed: {expat.errors.messages[error.code]}"
        ) from error
    root: Element = builder.close()
    return root
