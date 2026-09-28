"""Portable, bounded source extractors used by the change inventory.

The interface deliberately distinguishes a semantic inventory from a file/hunk
fallback.  Consumers must inspect ``capability`` before treating a row as a
symbol-level conclusion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedSymbol:
    name: str
    kind: str
    line: int
    end_line: int
    signature: str = ""


@dataclass
class Extraction:
    symbols: list[ExtractedSymbol]
    capability: str  # semantic | hunk-only | unsupported
    reason: str | None = None


class SourceExtractor:
    """One language extractor contract for inventory and classification."""
    name = "fallback"
    extensions: tuple[str, ...] = ()
    capability = "unsupported"

    def supports(self, path: str) -> bool:
        return path.lower().endswith(self.extensions)

    def inventory(self, source: str | None) -> Extraction:
        return Extraction([], self.capability, "semantic extraction is unavailable for this file type")


class TypeScriptExtractor(SourceExtractor):
    """Bounded source parser, intentionally not a TypeScript compiler."""
    name = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts")
    capability = "hunk-only"
    _DECL = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:(async)\s+)?"
        r"(function|class|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)\s*(\([^)]*\))?"
    )

    def inventory(self, source: str | None) -> Extraction:
        if source is None:
            return Extraction([], self.capability, "source is unavailable")
        symbols: list[ExtractedSymbol] = []
        for line, text in enumerate(source.splitlines(), 1):
            match = self._DECL.match(text)
            if not match:
                continue
            async_, kind, name, params = match.groups()
            symbols.append(ExtractedSymbol(name, "async_function" if async_ else kind, line, line, params or ""))
        return Extraction(symbols, self.capability, "TypeScript uses bounded regex/source parsing; types and nesting may be incomplete")


class GenericFallbackExtractor(SourceExtractor):
    name = "generic"
    capability = "hunk-only"


_EXTRACTORS: list[SourceExtractor] = [TypeScriptExtractor()]
_FALLBACK = GenericFallbackExtractor()


def for_path(path: str) -> SourceExtractor:
    return next((extractor for extractor in _EXTRACTORS if extractor.supports(path)), _FALLBACK)
