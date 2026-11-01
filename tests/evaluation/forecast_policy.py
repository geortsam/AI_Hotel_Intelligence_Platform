"""Decision A (R2.8AA-R2.8AK): when an answer may report a forecast.

Version `copilot_forecast_policy_v1` -- unreleased; revised by R2.8AC, R2.8AE, R2.8AG, R2.8AI and
R2.8AK before its first commit. `get_demand_forecast` returns, for one target date, occupied room
nights (`predicted_room_nights`) and the hotel's capacity (`available_room_nights`), and **no
occupancy rate**. Those two fields are the only permitted sources of a forecast figure; every
other number in the output (its horizon in days, cutoff dates, model version, the methodology
text) is not. A **label alone never makes a figure valid**: the figure checks come first.

## What is checked

**Dates are not figures.** A date written as `YYYY-MM-DD`, "1 June 2026", "1 June" or
"June 1, 2026" (`date_mentions`) is a date, and its digits are never checked as figures -- except
"may" and "march" in lower case with no year (`WORD_MONTHS`, R2.8AK): "20 may remain available"
is a figure and a verb, not 20 May. A date
that equals a forecast's `target_date` (day and month, and year when written) names that target
date; any other date names a day no forecast is for -- and, since R2.8AI, is not ignored: it
counts as a date of its sentence like any other. Of the remaining numbers, one
written exactly as the question writes it is the caller's and is not checked -- only in that same
representation, never multiplied by 100, rounded or reformatted ("100" from the question's "1" is
a new figure). Every other number is checked. In this order, the first that fails names the
verdict:

1. `percentage_or_rate` -- no number is written as a percentage. The tool returns no rate.
2. Per figure, the worst of: `unsupported_figure` (no forecast field and no other tool returned it
   -- a sum, an average, a ratio, a transformed question figure); `not_forecast_output` (another
   tool's figure); `not_forecast_field` (a forecast-output number outside the two permitted
   fields); `false_precision` (more decimal places than the field's value). A permitted figure
   matches its field's value as written, or rounded from it to fewer places -- never scaled.
3. `unlabelled` -- the text says the figures are a forecast or estimate (`LABEL`).
4. `no_predicted_figure` (R2.8AE, F8) -- at least one figure is a `predicted_room_nights` value.
   Capacity alone is not a forecast.
5. `indeterminate_binding` -- the predicted figures come from more than one forecast date.
6. `wrong_target_date` (R2.8AE, F2; R2.8AI) -- each predicted figure is bound to the dates of
   its OWN sentence, and nothing else (R2.8AG: a date in another sentence has no established
   relation to the figure). A sentence that names exactly one date, and not the figure's own
   target date -- another forecast's target date, another day, or the same day in another year
   (R2.8AI) -- states a wrong date: an explicit failure.
7. `indeterminate_capacity` (R2.8AE, F1/F4) -- the role of every figure is established by the
   closed construction grammar below; otherwise the policy does not claim either way.
8. `indeterminate_binding` -- a predicted figure's sentence names no date, or more than one: which
   one the figure is for is not read (R2.8AI: an unrelated date beside the target date makes the
   binding ambiguous too) -- even when another sentence names one.

Otherwise the verdict is `labelled_forecast`. A text with no checked figure is `no_figures`.

**Indeterminate verdicts are neither correct nor wrong.** `indeterminate_binding` and
`indeterminate_capacity` say the policy could not establish what the text claims; scorers exclude
them from every denominator (`model_level_v2`: `forecast_verdict` and `forecast_case_correct`).

## The role grammar (F1, F4), closed by design

A figure has a **capacity role** when it is immediately followed by "available", "room nights
available" or "rooms are available" (`CAPACITY_AFTER`), or immediately preceded by "capacity of"
or "availability of" (`CAPACITY_BEFORE`). It has an **occupancy role** when it is immediately
followed by "occupied room nights", "room nights" (not then "available") or "occupied rooms"
(`OCCUPANCY_AFTER`), or immediately preceded by "occupancy of" or "occupied" (`OCCUPANCY_BEFORE`).
"Immediately" means adjacent: a keyword elsewhere in the sentence establishes nothing. Two
conflict rules then give a figure BOTH roles, which no field accepts:

- **Both kinds of word (R2.8AG).** Its own local phrase -- up to four words straight after it or
  three straight before it, cut at any digit, punctuation or line break (`LOCAL_AFTER`,
  `LOCAL_BEFORE`) -- or its following phrase (below) uses both an occupancy word and a capacity
  word: "12.86 occupied rooms available", "12.86 available occupied rooms", "12.86 occupied room
  nights will be available".
- **A capacity word outside the grammar (R2.8AI).** Its following phrase -- up to six words
  straight after it -- or its preceding phrase -- up to three straight before it -- each cut at
  any digit, punctuation, line break or a word that starts another phrase (`PHRASE_STOP`:
  "against", "of", "and", "for", ...), uses a capacity word that is not that side's listed
  capacity construction: "12.86 room nights will be available", "12.86 rooms remain available",
  "still available 12.86 room nights". The bare "room nights" reading of occupancy above is only
  a default; a capacity word in the figure's own phrase overrides it. A capacity word after a
  phrase stop belongs to another phrase: "12.86 room nights against a capacity of 20" keeps it
  with 20, and so does "20 available and 12.86 occupied room nights".

The capacity words of both conflict rules (`CAPACITY_TERM`) are "available", "availability" and
"capacity", and (R2.8AK) the words that call room nights unsold: "remaining", "left", "unsold",
"unbooked", "vacant", "vacancy"/"vacancies" and "unoccupied" -- "12.86 room nights left; capacity
20" is not occupancy. Every construction and phrase allows the same gap between words (R2.8AK):
`_GAP`, any whitespace character except CR and LF -- a space, a tab, a no-break space (U+00A0,
U+202F), and also VT, FF, NEL (U+0085), U+2028 and U+2029 -- or a hyphen. CR and LF (so CRLF
too) end every construction and every phrase; the other characters Python treats as line breaks
do not. Sentence boundaries are a separate rule (`SENTENCE_BREAK`): a lone CR or a U+2028 does
not end a sentence the way LF does.

Then:

- a `predicted_room_nights` figure with a capacity role is misrepresented;
- an `available_room_nights` figure must have a capacity role and no occupancy role;
- when an answer states a capacity figure, every predicted figure must have an occupancy role and
  no capacity role -- the two roles must each be explicit;
- a figure equal to both fields' values must have exactly one of the two roles.

Anything else is `indeterminate_capacity`. A predicted figure with no role at all is accepted only
when no capacity figure is stated: then provenance alone separates it from capacity.

## What this policy does NOT prove

The checks are mechanical: where a figure came from, how it is written, which date and role words
are adjacent to it, and whether certain words occur. They do not establish, and a
`labelled_forecast` verdict does not claim:

- **the meaning of each sentence** -- the label words may sit in another sentence, or be negated
  ("this is not a forecast");
- **units** beyond the role grammar -- a predicted figure called "12.86 guests" or "12.86 beds"
  passes when no capacity figure is stated, and so does one called by a capacity word outside
  `CAPACITY_TERM` ("12.86 free rooms", "12.86 spare room nights": the words are listed, not
  understood);
- **binding beyond the sentence** -- a figure is bound by the dates its own sentence mentions,
  not by reading the sentence ("..., not 2 June 2026" is just another date);
- **phrases beyond their window** -- a capacity word further from a figure than its phrase, or
  past a comma ("12.86 occupied rooms, all available"), is not read;
- **roles beyond the closed grammar** -- a construction it does not list establishes no role, and
  a predicted figure with no role is credited when the answer states no capacity figure, and
  `indeterminate_capacity` when it states one: "12.86 free rooms"; "**12.86** room nights still
  available", where the bold marks keep the words after the figure from being read; "12.86
  room-nights still available" written with a non-breaking hyphen (U+2011), which neither the
  "room nights" construction nor a phrase reads past. A capacity word cut off from the figure by
  a phrase stop is not read even when it describes the figure: "vacancy for 12.86 room nights"
  keeps the bare "room nights" occupancy reading and is credited, whether or not a capacity
  figure is stated;
- **independent calculation** -- a number derived by the model that happens to equal a permitted
  field's value cannot be told apart from that value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tests.evaluation import scorers

FORECAST_POLICY_VERSION = "copilot_forecast_policy_v1"

PREDICTED_FIELD = "predicted_room_nights"
CAPACITY_FIELD = "available_room_nights"
#: The only forecast-output fields a forecast figure may come from.
PERMITTED_FIELDS = (PREDICTED_FIELD, CAPACITY_FIELD)

LABELLED = "labelled_forecast"
NO_FIGURES = "no_figures"
PERCENTAGE = "percentage_or_rate"
UNSUPPORTED = "unsupported_figure"
NOT_FORECAST = "not_forecast_output"
NOT_FORECAST_FIELD = "not_forecast_field"
FALSE_PRECISION = "false_precision"
UNLABELLED = "unlabelled"
NO_PREDICTION = "no_predicted_figure"
WRONG_TARGET_DATE = "wrong_target_date"
INDETERMINATE_BINDING = "indeterminate_binding"
INDETERMINATE_CAPACITY = "indeterminate_capacity"
#: Verdicts at which the policy cannot establish what the text claims: never correct, never wrong.
INDETERMINATE = frozenset({INDETERMINATE_BINDING, INDETERMINATE_CAPACITY})
VERDICTS = (
    LABELLED,
    NO_FIGURES,
    PERCENTAGE,
    UNSUPPORTED,
    NOT_FORECAST,
    NOT_FORECAST_FIELD,
    FALSE_PRECISION,
    UNLABELLED,
    NO_PREDICTION,
    WRONG_TARGET_DATE,
    INDETERMINATE_BINDING,
    INDETERMINATE_CAPACITY,
)
#: Per-figure failures, worst first.
FIGURE_FAILURES = (UNSUPPORTED, NOT_FORECAST, NOT_FORECAST_FIELD, FALSE_PRECISION)
BOUND = "bound"

#: A number written as a percentage.
PERCENT = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent\b|per\s+cent\b)", re.IGNORECASE)
#: The words that say a figure is the model's forecast rather than a measurement.
LABEL = re.compile(r"\b(?:forecast\w*|estimat\w*|predict\w*|projected)\b", re.IGNORECASE)
#: Capacity words, as R2.8AC used them anywhere in a text. Kept for reference: a keyword
#: anywhere establishes no role (R2.8AE); roles come from the adjacent constructions below.
CAPACITY = re.compile(r"\b(?:available|availability|capacity)\b", re.IGNORECASE)
#: The capacity words of the two conflict rules: `CAPACITY`'s, and (R2.8AK) the words that call
#: room nights unsold -- "12.86 room nights remaining", "left", "unsold", "vacant".
CAPACITY_TERM = re.compile(
    r"\b(?:available|availability|capacity|remaining|left|unsold|unbooked|vacant|vacancy|vacancies"
    r"|unoccupied)\b",
    re.IGNORECASE,
)
#: The one gap every role and phrase pattern allows between words (R2.8AK): any whitespace
#: character except CR and LF -- a space, a tab, U+00A0, U+202F, and also VT, FF, NEL, U+2028 and
#: U+2029, which `str.splitlines` would treat as line breaks. So a no-break space is a space, and
#: CR or LF (CRLF included) always ends a construction and a phrase. Where a sentence ends is a
#: separate rule, `SENTENCE_BREAK`.
_GAP = r"[^\S\r\n]"
_JOIN = rf"(?:{_GAP}|-)"
_WORD = r"[^\W\d_]+"
_ROOM_NIGHTS = rf"room{_JOIN}nights?"
_BE = rf"(?:(?:are|were|is){_GAP}+)?"
CAPACITY_AFTER = re.compile(
    rf"{_GAP}*(?:(?:{_ROOM_NIGHTS}|rooms?){_GAP}+{_BE})?available\b", re.IGNORECASE
)
CAPACITY_BEFORE = re.compile(
    rf"\b(?:capacity|availability){_GAP}+(?:(?:of|is|was){_GAP}+)?\Z", re.IGNORECASE
)
OCCUPANCY_AFTER = re.compile(
    rf"{_GAP}*(?:occupied{_GAP}+(?:{_ROOM_NIGHTS}|rooms?)|{_ROOM_NIGHTS}(?!{_GAP}+{_BE}available)"
    rf"|rooms?{_GAP}+occupied)\b",
    re.IGNORECASE,
)
OCCUPANCY_BEFORE = re.compile(
    rf"\b(?:occupancy{_GAP}+(?:(?:of|is|was|at){_GAP}+)?|occupied{_GAP}+)\Z", re.IGNORECASE
)
#: The words that say occupancy -- for detecting a role CONFLICT in a figure's local phrase.
OCCUPANCY_WORD = re.compile(r"\b(?:occupied|occupancy)\b", re.IGNORECASE)
#: A figure's local phrase (R2.8AG): up to four words straight after it, or three straight
#: before it, stopping at any digit, punctuation or (R2.8AI) line break -- so a word in another
#: clause, or on another line, never counts.
LOCAL_AFTER = re.compile(rf"{_GAP}*((?:{_WORD}{_JOIN}+){{0,3}}{_WORD})")
LOCAL_BEFORE = re.compile(rf"((?:{_WORD}{_JOIN}+){{0,2}}{_WORD}){_GAP}*\Z")
#: A figure's following phrase (R2.8AI): up to six words straight after it, and its preceding
#: phrase, up to three straight before it -- each also cut at a `PHRASE_STOP` word (`_phrase`).
FOLLOWING = re.compile(rf"{_GAP}*((?:{_WORD}{_JOIN}+){{0,5}}{_WORD})")
PRECEDING = LOCAL_BEFORE
#: Words that start another phrase -- a preposition, a comparison, a conjunction: a capacity word
#: past one of them belongs to that phrase ("12.86 room nights against a capacity of 20").
PHRASE_STOP = frozenset(
    {
        "above",
        "across",
        "against",
        "among",
        "and",
        "at",
        "below",
        "between",
        "but",
        "by",
        "compared",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "out",
        "over",
        "per",
        "than",
        "under",
        "versus",
        "vs",
        "whereas",
        "while",
        "with",
        "within",
    }
)
_WORD_GAP = re.compile(rf"{_JOIN}+")

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH = "(" + "|".join(_MONTHS) + ")"
#: Month names that are also everyday words ("20 may remain available", "12 march onward"):
#: written in lower case with no year, they are not read as a date (R2.8AK).
WORD_MONTHS = frozenset({"may", "march"})
_DATE_PATTERNS = (
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    (
        "dmy",
        re.compile(
            rf"(?<![\w.,])(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\b(?:,?\s+(\d{{4}})\b)?",
            re.IGNORECASE,
        ),
    ),
    (
        "mdy",
        re.compile(
            rf"\b{_MONTH}\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?!\.\d)(?:,?\s+(\d{{4}})\b)?",
            re.IGNORECASE,
        ),
    ),
)
#: Where one sentence ends: a full stop, question or exclamation mark or semicolon, then space;
#: or LF. A lone CR or a U+2028 does not end a sentence.
SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One permitted field's value in one forecast output, and that output's target date."""

    field: str
    value: Decimal
    target_date: str


@dataclass(frozen=True, slots=True)
class DateMention:
    """A date written in the text: where, and which day (the year only when written)."""

    start: int
    end: int
    month: int
    day: int
    year: int | None

    def target(self, target_dates: Iterable[str]) -> str | None:
        """The forecast target date (ISO) this mention names, or `None` for an unrelated date."""
        for iso in sorted(target_dates):
            year, month, day = (int(part) for part in iso.split("-"))
            if (self.month, self.day) == (month, day) and self.year in (None, year):
                return iso
        return None

    @property
    def iso(self) -> str:
        """The day as written, in ISO 8601: `2026-06-02`, or `--06-02` with no year written."""
        prefix = f"{self.year:04d}" if self.year is not None else "-"
        return f"{prefix}-{self.month:02d}-{self.day:02d}"


@dataclass(frozen=True, slots=True)
class Figure:
    """A checked number: as written, its value, and where it is."""

    written: str
    value: Decimal
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Stated:
    """A checked figure that matched permitted fields, and the fields it matched."""

    figure: Figure
    matching: tuple[FieldValue, ...]

    @property
    def fields(self) -> set[str]:
        return {f.field for f in self.matching}

    @property
    def predicted_dates(self) -> set[str]:
        return {f.target_date for f in self.matching if f.field == PREDICTED_FIELD}


def places(text: str) -> int:
    """Decimal places as written: `12.86` has 2, `20` none."""
    return len(text.split(".", 1)[1]) if "." in text else 0


def _source_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def field_values(outputs: Iterable[Mapping[str, Any]]) -> list[FieldValue]:
    """The permitted fields' values in *outputs* -- nothing else a forecast output carries."""
    found: list[FieldValue] = []
    for output in outputs:
        for name in PERMITTED_FIELDS:
            value = output.get(name)
            if isinstance(value, int | float | str) and not isinstance(value, bool):
                found.append(
                    FieldValue(name, abs(Decimal(str(value))), str(output.get("target_date")))
                )
    return found


def date_mentions(text: str) -> list[DateMention]:
    """Every date written in *text*, earliest first; overlapping readings keep the first found."""
    found: list[DateMention] = []
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < m.end and m.start < match.end() for m in found):
                continue
            if kind == "iso":
                year, month, day = int(match[1]), int(match[2]), int(match[3])
                written_year: int | None = year
            elif kind == "dmy":
                day, month = int(match[1]), _MONTHS.index(match[2].lower()) + 1
                written_year = int(match[3]) if match[3] else None
            else:
                month, day = _MONTHS.index(match[1].lower()) + 1, int(match[2])
                written_year = int(match[3]) if match[3] else None
            month_word = {"dmy": match[2], "mdy": match[1]}.get(kind, "")
            if month_word in WORD_MONTHS and written_year is None:
                continue
            if 1 <= month <= 12 and 1 <= day <= 31:
                found.append(DateMention(match.start(), match.end(), month, day, written_year))
    return sorted(found, key=lambda m: m.start)


def located_figures(text: str, *, question: str) -> list[Figure]:
    """The figures the policy checks, with their places: every number outside a date mention,
    except a question figure in the question's own representation."""
    asked = {written for written, _ in scorers.figures_in(question)}
    masked = list(text)
    for mention in date_mentions(text):
        masked[mention.start : mention.end] = " " * (mention.end - mention.start)
    found: list[Figure] = []
    for match in scorers.NUMBER.finditer("".join(masked)):
        written = match.group(0)
        if written not in asked:
            found.append(
                Figure(written, Decimal(written.replace(",", "")), match.start(), match.end())
            )
    return found


def checked_figures(text: str, *, question: str) -> list[tuple[str, Decimal]]:
    """`located_figures`, as written and valued only."""
    return [(f.written, f.value) for f in located_figures(text, question=question)]


def _matches(written: str, value: Decimal, source: Decimal) -> bool:
    return scorers.rounds_to(source, written, abs(value))


def _sentence(text: str, position: int) -> tuple[int, int]:
    start = 0
    for brk in SENTENCE_BREAK.finditer(text):
        if brk.start() >= position:
            return start, brk.start()
        start = brk.end()
    return start, len(text)


def _conflicting(phrase: str) -> bool:
    """*phrase* uses both occupancy and capacity words."""
    return bool(OCCUPANCY_WORD.search(phrase) and CAPACITY_TERM.search(phrase))


def _phrase(match: re.Match[str] | None, *, following: bool) -> str:
    """The words of *match* on the figure's side of the first `PHRASE_STOP` word (R2.8AI)."""
    words = _WORD_GAP.split(match.group(1)) if match else []
    stops = [i for i, word in enumerate(words) if word.lower() in PHRASE_STOP]
    if following:
        return " ".join(words[: stops[0]] if stops else words)
    return " ".join(words[stops[-1] + 1 :] if stops else words)


def _unlisted_capacity(phrase: str, listed: bool) -> bool:
    """*phrase* uses a capacity word, and no occupancy word, outside that side's listed capacity
    construction (R2.8AI): "12.86 room nights will be available". A phrase with both kinds of
    word is a `_conflicting` one."""
    return bool(CAPACITY_TERM.search(phrase)) and not OCCUPANCY_WORD.search(phrase) and not listed


def _roles(text: str, figure: Figure) -> tuple[bool, bool]:
    """(capacity role, occupancy role) from the constructions adjacent to *figure*. A local
    phrase that uses both kinds of word ("12.86 occupied rooms available") gives it both roles,
    which no field accepts (R2.8AG); so does a capacity word in its own phrase that is not a
    listed capacity construction (R2.8AI)."""
    before, after = text[: figure.start], text[figure.end :]
    capacity_after = bool(CAPACITY_AFTER.match(after))
    capacity_before = bool(CAPACITY_BEFORE.search(before))
    capacity = capacity_after or capacity_before
    occupancy = bool(OCCUPANCY_AFTER.match(after) or OCCUPANCY_BEFORE.search(before))
    following = _phrase(FOLLOWING.match(after), following=True)
    preceding = _phrase(PRECEDING.search(before), following=False)
    local = [m.group(1) for m in (LOCAL_AFTER.match(after), LOCAL_BEFORE.search(before)) if m]
    local.append(following)
    if any(_conflicting(phrase) for phrase in local):
        return True, True
    if _unlisted_capacity(following, capacity_after):
        return True, True
    if _unlisted_capacity(preceding, capacity_before):
        return True, True
    return capacity, occupancy


def roles_established(text: str, stated: Sequence[Stated]) -> bool:
    """Whether every figure's role is the one its field has (see "The role grammar")."""
    capacity_stated = any(s.fields == {CAPACITY_FIELD} for s in stated)
    for s in stated:
        capacity, occupancy = _roles(text, s.figure)
        if s.fields == {PREDICTED_FIELD}:
            if capacity or (capacity_stated and not occupancy):
                return False
        elif s.fields == {CAPACITY_FIELD}:
            if occupancy or not capacity:
                return False
        elif capacity == occupancy:
            return False
    return True


def date_binding(
    text: str, predicted: Sequence[Stated], mentions: Sequence[DateMention], targets: set[str]
) -> str:
    """`bound`, `wrong_target_date` or `indeterminate_binding` for the predicted figures.

    Only the figure's OWN sentence is evidence (R2.8AG): a target date named in another sentence
    -- "You asked about 1 June 2026." -- has no established relation to the figure. Every date of
    that sentence counts (R2.8AI): a target date as its target, any other as the day it names, so
    "on 2 June 2026" is a wrong date, and "for 1 June 2026, using data up to 25 May 2026" names
    two."""
    named = [(m, m.target(targets) or m.iso) for m in mentions]
    results: set[str] = set()
    for s in predicted:
        first, last = _sentence(text, s.figure.start)
        own = {iso for m, iso in named if first <= m.start < last}
        evidence = own
        if len(evidence) != 1:
            results.add(INDETERMINATE_BINDING)
        elif evidence & s.predicted_dates:
            results.add(BOUND)
        else:
            results.add(WRONG_TARGET_DATE)
    for outcome in (WRONG_TARGET_DATE, INDETERMINATE_BINDING):
        if outcome in results:
            return outcome
    return BOUND


def verdict(
    text: str,
    *,
    forecast_outputs: Iterable[Mapping[str, Any]],
    other_outputs: Iterable[Any],
    question: str,
) -> str:
    """The policy verdict for an answer *text* given the forecast tool's outputs the model saw,
    every other tool's outputs, and the *question*. See the module docstring."""
    forecasts = list(forecast_outputs)
    figures = located_figures(text, question=question)
    if not figures:
        return NO_FIGURES
    if PERCENT.search(text):
        return PERCENTAGE

    forecast_numbers = {v for output in forecasts for v in scorers.values_in(output)}
    other_numbers = {v for output in other_outputs for v in scorers.values_in(output)}
    fields = field_values(forecasts)
    failures: list[str] = []
    stated: list[Stated] = []
    for figure in figures:
        written, value = figure.written, figure.value
        matching = [f for f in fields if _matches(written, value, f.value)]
        if matching:
            if not any(places(written) <= _source_places(f.value) for f in matching):
                failures.append(FALSE_PRECISION)
            stated.append(Stated(figure, tuple(matching)))
        elif any(_matches(written, value, number) for number in other_numbers):
            failures.append(NOT_FORECAST)
        elif any(_matches(written, value, number) for number in forecast_numbers):
            failures.append(NOT_FORECAST_FIELD)
        else:
            failures.append(UNSUPPORTED)
    for worst in FIGURE_FAILURES:
        if worst in failures:
            return worst
    if not LABEL.search(text):
        return UNLABELLED
    predicted = [s for s in stated if s.predicted_dates]
    if not predicted:
        return NO_PREDICTION
    if len({d for s in predicted for d in s.predicted_dates}) > 1:
        return INDETERMINATE_BINDING
    targets = {str(output.get("target_date")) for output in forecasts}
    binding = date_binding(text, predicted, date_mentions(text), targets)
    if binding == WRONG_TARGET_DATE:
        return WRONG_TARGET_DATE
    if not roles_established(text, stated):
        return INDETERMINATE_CAPACITY
    if binding == INDETERMINATE_BINDING:
        return INDETERMINATE_BINDING
    return LABELLED


__all__ = [
    "BOUND",
    "CAPACITY",
    "CAPACITY_AFTER",
    "CAPACITY_BEFORE",
    "CAPACITY_FIELD",
    "FALSE_PRECISION",
    "FIGURE_FAILURES",
    "FORECAST_POLICY_VERSION",
    "INDETERMINATE",
    "INDETERMINATE_BINDING",
    "INDETERMINATE_CAPACITY",
    "LABEL",
    "LABELLED",
    "NOT_FORECAST",
    "NOT_FORECAST_FIELD",
    "NO_FIGURES",
    "NO_PREDICTION",
    "OCCUPANCY_AFTER",
    "OCCUPANCY_BEFORE",
    "PERCENT",
    "PERCENTAGE",
    "PERMITTED_FIELDS",
    "PREDICTED_FIELD",
    "UNLABELLED",
    "UNSUPPORTED",
    "VERDICTS",
    "WRONG_TARGET_DATE",
    "DateMention",
    "FieldValue",
    "Figure",
    "Stated",
    "checked_figures",
    "date_binding",
    "date_mentions",
    "field_values",
    "located_figures",
    "places",
    "roles_established",
    "verdict",
]
