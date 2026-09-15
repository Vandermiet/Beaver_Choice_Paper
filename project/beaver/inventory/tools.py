"""The inventory agent's tools.

Arithmetic lives in tools, judgement lives in the model — so every
deterministic rule this agent applies is ordinary function code here, testable
without a model call.

All four resolution rules are in this module, and between them they leave the
model less to do than the design first expected. That is rule 4's doing: *two
or more survivors always ask*, so once the deterministic narrowing has run
there is nothing left to break a tie with. What the model contributes is
reading the line; what the code contributes is every consequence of it.

The scoring behind the rules is deliberately not a similarity threshold — a
magic number is exactly what #6 rejected. Instead the **catalogue decides which
words mean something**:

- a word the product universe never uses is a sales adjective, and drops
  ("high-quality", "sturdy", "white");
- a word the universe does use selects ("glossy", "kraft", "colored" — the
  catalogue names `Colored paper`, so colour is not always an adjective);
- a size never selects. It vetoes when the universe has never heard of it, and
  otherwise only says which universe item the customer had in mind, which is
  what tells `poster boards (24" x 36")` from `colorful poster paper`.

Tool docstring convention: only the leading description, the parameter
descriptions and the *first* returns entry reach the model. `Raises`, `Notes`
and `Examples` are dropped, so anything a tool contract depends on is stated in
the description or in the parameter docs.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from beaver import starter
from beaver.contract import CarriedItemName, RequestedLine, carried_catalogue
from beaver.inventory.models import (
    CarriedItem,
    LineVerdict,
    ProductCategory,
    ResolutionDecision,
    NameVerdict,
    StockReading,
)

#: The item a generic request for ordinary paper resolves to. "Printer paper",
#: "copy paper" and "white paper" name no product in particular; they name the
#: everyday sheet a paper company is expected to sell, and the carried
#: catalogue holds exactly one.
DEFAULT_PLAIN_PAPER = "A4 paper"

#: Words that name the trade rather than a product. They are dropped from
#: candidate scoring, and their presence is what makes a line *generically
#: papery* — the trigger for the default plain-paper item.
PAPER_WORDS: frozenset[str] = frozenset({"paper", "sheet", "ream"})

#: Words that describe ordinary paper rather than a finish or material. Dropped
#: with the paper words, which is what routes "standard copy paper" to the
#: default rather than to the uncarried `Standard copy paper`.
PLAIN_WORDS: frozenset[str] = frozenset(
    {
        "printer",
        "printing",
        "copy",
        "plain",
        "standard",
        "regular",
        "everyday",
        "multipurpose",
        "office",
        "blank",
        "white",
    }
)

#: Packaging and quantity words that carry no product meaning.
PACKAGING_WORDS: frozenset[str] = frozenset(
    {"pack", "packet", "box", "case", "carton", "bundle", "pallet", "unit", "piece", "item"}
)

#: Every word that describes the trade, the packaging or the ordinary sheet
#: rather than a product. A phrase made only of these names no item in
#: particular, which is the condition the default plain-paper rule tests.
GENERIC_WORDS: frozenset[str] = PAPER_WORDS | PLAIN_WORDS | PACKAGING_WORDS

#: Words that carry no meaning in either a customer's phrase or an item name:
#: grammar, hedges, and the words a dimension leaves behind once the size
#: itself has been lifted out of the text.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "of",
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "with",
        "in",
        "on",
        "to",
        "some",
        "our",
        "we",
        "need",
        "want",
        "please",
        "would",
        "like",
        "assorted",
        "various",
        "each",
        "per",
        "x",
        "approx",
        "about",
        "size",
        "sized",
        "width",
        "inch",
        "inches",
    }
)

#: Units that mean "some number of the thing we price, and you have to know
#: which". We refuse them rather than convert: a ream-to-sheet conversion
#: asserts an accounting fact the data never contains — units exist only in
#: Python comments on `paper_supplies` and are invisible at runtime — on a line
#: that would then bill 500x what the customer read.
MULTIPLIER_UNITS: frozenset[str] = frozenset(
    {
        "ream",
        "pack",
        "packet",
        "packaging",
        "box",
        "case",
        "carton",
        "bundle",
        "pallet",
        "dozen",
        "gross",
        "crate",
        "set",
        "kit",
    }
)

#: Sizes named as words rather than as dimensions.
_SIZE_WORDS: frozenset[str] = frozenset({"letter", "legal", "tabloid", "foolscap", "ledger"})

#: A dimension pair, with or without inch marks: `24x36`, `24" x 36"`, `8.5 x 11`.
_DIMENSION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:\"|”|''|in\.?|inch(?:es)?)?\s*[x×]\s*(\d+(?:\.\d+)?)"
    r"\s*(?:\"|”|''|in\.?|inch(?:es)?)?",
    re.IGNORECASE,
)

#: An ISO paper size: A3, A4, A5.
_A_SERIES = re.compile(r"\b[aA]([0-9])\b")

#: A single dimension given in inches: `36-inch width`.
_INCHES = re.compile(r"\b(\d+(?:\.\d+)?)\s*[-\s]?inch(?:es)?\b", re.IGNORECASE)

#: Dimensions the trade names in words. Recorded so that a customer's
#: `8.5" x 11"` is recognised as the Letter the universe does name, rather than
#: vetoed as a size nobody has heard of.
_SIZE_ALIASES: dict[str, str] = {
    "8.5x11": "letter",
    "8.5x14": "legal",
    "11x17": "tabloid",
}


def _trim_number(value: str) -> str:
    return value[:-2] if value.endswith(".0") else value


def _extract_sizes(text: str) -> tuple[list[str], str]:
    """Pull every size out of a phrase, and hand back what is left of it.

    Sizes are removed from the text before it is tokenised, which is how the
    design's "a size never selects" survives contact with `A4 paper` being an
    item name: the size cannot score for anything, because by then it is gone.

    Args:
        text: The phrase, as the customer or the catalogue states it.

    Returns:
        The normalised size names found, and the text with their spans blanked.
    """
    sizes: list[str] = []
    remainder = text

    def take(pattern: re.Pattern[str], name_of: Callable[[re.Match[str]], str]) -> None:
        nonlocal remainder
        for match in pattern.finditer(remainder):
            sizes.append(name_of(match))
        remainder = pattern.sub(" ", remainder)

    take(_DIMENSION, lambda m: f"{_trim_number(m.group(1))}x{_trim_number(m.group(2))}")
    take(_A_SERIES, lambda m: f"a{m.group(1)}")
    take(_INCHES, lambda m: f"{_trim_number(m.group(1))}-inch")

    words = []
    for word in re.findall(r"[a-z]+", remainder.lower()):
        if word in _SIZE_WORDS:
            words.append(word)
    sizes.extend(words)
    for word in words:
        remainder = re.sub(rf"\b{word}\b", " ", remainder, flags=re.IGNORECASE)

    normalised = [_SIZE_ALIASES.get(size, size) for size in sizes]
    return list(dict.fromkeys(normalised)), remainder


def _words(text: str) -> list[str]:
    """Singularised words, stopwords and punctuation gone, order kept."""
    raw = re.findall(r"[a-z0-9.]+", text.lower())
    words = []
    for word in raw:
        word = word.strip(".")
        if not word or word in _STOPWORDS:
            continue
        if word.endswith("es") and word[:-2].endswith(("x", "z", "ch", "sh", "ss")):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


def _terms(text: str) -> set[str]:
    """The terms a phrase can be matched on: substantive words and joined pairs.

    Two rules earn their keep here. A bare number is dropped as a word but kept
    inside a pair, so `100 lb cover stock` matches on `100lb` without `100`
    matching everything numeric. And a pair is dropped when neither half says
    anything — so "copy paper" cannot match the uncarried `Standard copy paper`
    through a `copypaper` nobody meant, and "500 sheets" raises no term at all.

    Args:
        text: The phrase, already stripped of its sizes.

    Returns:
        The match terms.
    """
    words = _words(text)
    terms = {word for word in words if word not in GENERIC_WORDS and not _is_number(word)}
    for left, right in zip(words, words[1:]):
        if _says_nothing(left) and _says_nothing(right):
            continue
        terms.add(f"{left}{right}")
    return terms


def _says_nothing(word: str) -> bool:
    """Whether a word names neither a product nor a property of one."""
    return word in GENERIC_WORDS or _is_number(word)


def _is_number(word: str) -> bool:
    try:
        float(word)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _UniverseItem:
    """One product-universe item, indexed for matching.

    The terms and the sizes are what an item name *offers* a phrase, worked out
    once at import rather than on every comparison.
    """

    name: str
    category: ProductCategory
    terms: set[str]
    sizes: list[str]


class _Catalogue:
    """The product universe and the carried subset, indexed for matching once.

    Built lazily and rebuilt when the carried set changes underneath it, which
    is a test swapping in a temporary database rather than anything the harness
    does.
    """

    def __init__(self) -> None:
        self._universe: dict[str, _UniverseItem] = {}
        self._carried: frozenset[str] = frozenset()
        self._size_vocabulary: frozenset[str] = frozenset()

    def _build(self) -> None:
        universe = {}
        sizes: set[str] = set()
        for row in starter.paper_supplies:
            name = row["item_name"]
            item_sizes, remainder = _extract_sizes(name)
            sizes.update(item_sizes)
            universe[name] = _UniverseItem(
                name=name,
                category=ProductCategory(row["category"]),
                terms=_terms(remainder),
                sizes=item_sizes,
            )
        self._universe = universe
        self._size_vocabulary = frozenset(sizes)

    def refresh(self) -> None:
        """Re-read the carried catalogue, building the universe index if needed."""
        if not self._universe:
            self._build()
        self._carried = carried_catalogue()

    @property
    def universe(self) -> dict[str, _UniverseItem]:
        """Every item the business could conceivably sell, by name."""
        self.refresh()
        return self._universe

    @property
    def carried(self) -> frozenset[str]:
        """The names the business actually sells, read from the `inventory` table."""
        self.refresh()
        return self._carried

    @property
    def size_vocabulary(self) -> frozenset[str]:
        """Every size the product universe names, anywhere in any item name."""
        self.refresh()
        return self._size_vocabulary

    def category(self, item_name: str) -> ProductCategory:
        """The product category an item name sits in."""
        return self.universe[item_name].category


_CATALOGUE = _Catalogue()


def _score(item_terms: set[str], stated_terms: set[str]) -> int:
    """How strongly one item name answers a phrase.

    Longer matched terms count for more, which is what makes `card stock` mean
    `Cardstock` rather than `Invitation cards` — a joined pair outweighs the
    single word it contains. It is a preference for the longest match, not a
    threshold anyone has to justify.

    Args:
        item_terms: The item name's match terms.
        stated_terms: The phrase's match terms.

    Returns:
        The score, in matched characters.
    """
    return sum(len(term) for term in item_terms & stated_terms)


def _universe_best_match(
    stated_terms: set[str], stated_sizes: list[str], head_terms: set[str]
) -> tuple[str | None, ProductCategory | None]:
    """The item in the whole product universe the phrase most nearly names.

    Two things count *here* and nowhere else, because this answers "what did
    they name?" rather than "what would we sell them?".

    Sizes: a size never selects between things we carry, but it does say which
    universe item the customer had in mind, which is the difference between
    `poster boards (24" x 36")` — plainly the large-format sheet — and
    `colorful poster paper`, which is not.

    The head noun: a phrase is what its last noun says it is, and the words in
    front of it say what it is like. "Recycled kraft paper envelopes" are
    envelopes, and matching two words of "kraft paper" does not make them
    sheets — the category guard then refuses the substitution.

    Args:
        stated_terms: The phrase's match terms.
        stated_sizes: The sizes it named.
        head_terms: The terms that end in the phrase's head noun.

    Returns:
        The best-matching universe item name and its category, or two `None`s.
    """
    sizes = set(stated_sizes)
    best_name, best_score, best_spare = None, 0, 0
    for name, item in _CATALOGUE.universe.items():
        matched_head = item.terms & head_terms
        score = (
            _score(item.terms, stated_terms)
            + sum(len(s) for s in sizes & set(item.sizes))
            + (max(len(term) for term in matched_head) if matched_head else 0)
        )
        if score == 0:
            continue
        spare = len(item.terms - stated_terms)
        if score > best_score or (score == best_score and spare < best_spare):
            best_name, best_score, best_spare = name, score, spare
    if best_name is None:
        return None, None
    return best_name, _CATALOGUE.category(best_name)


def shortlist_candidates(items_as_stated: list[str]) -> list[NameVerdict]:
    """Decide, from the catalogue alone, which item each line's words name.

    Applies all four resolution rules to every line at once and returns one
    verdict per line, in the order you gave them: the item we would sell them,
    or the reason we cannot. A finish or material word selects the item; colour
    and quality words that the catalogue never uses are dropped; a size the
    product universe has never heard of refuses the line; a lone candidate in a
    different product category from the item the customer actually named is
    refused rather than substituted; and two or more survivors are always asked
    about rather than guessed between. A generic request for ordinary paper
    resolves to the everyday sheet we sell.

    A verdict is final. There is no second opinion to be had by asking again in
    other words, and a refused line stays refused.

    Args:
        items_as_stated: The customer's own words for each requested line, with
            the quantities and units left out.

    Returns:
        One verdict per line, in the order the lines were given.
    """
    return [resolve_name(item) for item in items_as_stated]


def resolve_name(item_as_stated: str) -> NameVerdict:
    """Decide, from the catalogue alone, which item one line's words name.

    The whole of the resolution rules, and the function `shortlist_candidates`
    maps over. It is a separate function so that every rule can be tested one
    line at a time, without a model and without a list.

    The size veto is applied last, over the decision the other three rules
    reached, because a size disqualifies a resolution and there has to be one
    to disqualify.

    Args:
        item_as_stated: The customer's own words for one requested line.

    Returns:
        The verdict, the surviving carried candidates, and the reasoning.
    """
    decided = _decide(item_as_stated)
    if decided.unrecognised_size is None:
        return decided
    if not _vetoed(decided, decided.universe_best_match):
        return decided
    return decided.model_copy(
        update={
            "decision": ResolutionDecision.SIZE_VETO,
            "resolved_item": None,
            "detail": (
                f"{decided.unrecognised_size!r} is a size no item in the product "
                f"universe is sold in"
            ),
        }
    )


def _decide(item_as_stated: str) -> NameVerdict:
    """The four rules, with the size noted but not yet allowed to veto.

    Args:
        item_as_stated: The customer's own words for one requested line.

    Returns:
        The verdict the rules reach on the words alone.
    """
    sizes, remainder = _extract_sizes(item_as_stated)
    stated_terms = _terms(remainder)
    unrecognised = next((s for s in sizes if s not in _CATALOGUE.size_vocabulary), None)
    best_match, best_category = _universe_best_match(
        stated_terms, sizes, _head_terms(remainder, stated_terms)
    )

    def verdict(
        decision: ResolutionDecision,
        detail: str,
        resolved: str | None = None,
        candidates: list[str] | None = None,
    ) -> NameVerdict:
        return NameVerdict(
            item_as_stated=item_as_stated,
            decision=decision,
            resolved_item=resolved,
            carried_candidates=candidates or [],
            universe_best_match=best_match,
            universe_best_category=best_category,
            sizes_named=sizes,
            unrecognised_size=unrecognised,
            selecting_terms=sorted(stated_terms),
            detail=detail,
        )

    scored = {
        name: _score(_CATALOGUE.universe[name].terms, stated_terms)
        for name in _CATALOGUE.carried
    }
    candidates = sorted(name for name, score in scored.items() if score > 0)

    if not stated_terms:
        if _names_paper(remainder):
            return verdict(
                ResolutionDecision.RESOLVED,
                "no finish or material named, so the default plain-paper item",
                resolved=DEFAULT_PLAIN_PAPER,
            )
        return verdict(
            ResolutionDecision.NO_CANDIDATE,
            "names nothing the product universe sells",
        )

    if not candidates:
        if best_match is None and _names_paper(remainder):
            return verdict(
                ResolutionDecision.RESOLVED,
                "nothing in the line names a product, so the default plain-paper item",
                resolved=DEFAULT_PLAIN_PAPER,
            )
        return verdict(
            ResolutionDecision.NO_CANDIDATE,
            f"nothing carried matches {sorted(stated_terms)}; "
            f"the universe's nearest is {best_match!r}",
        )

    top = max(scored[name] for name in candidates)
    survivors = [name for name in candidates if scored[name] == top]

    if len(survivors) > 1:
        return verdict(
            ResolutionDecision.AMBIGUOUS,
            f"{len(survivors)} carried candidates survive: {survivors}",
            candidates=survivors,
        )

    only = survivors[0]
    if best_category is not None and _CATALOGUE.category(only) is not best_category:
        return verdict(
            ResolutionDecision.CATEGORY_GUARD,
            f"the only candidate {only!r} is {_CATALOGUE.category(only)}, "
            f"while the named item {best_match!r} is {best_category}",
            candidates=[only],
        )

    return verdict(
        ResolutionDecision.RESOLVED,
        f"{only!r} is the only carried candidate",
        resolved=only,
        candidates=[only],
    )


def _vetoed(decided: NameVerdict, best_match: str | None) -> bool:
    """Whether an unrecognised size should refuse a line the rules have decided.

    A size disqualifies a *resolution*, so there has to be one to disqualify.
    "A3 matte paper" is vetoed — matte paper is a product the universe names,
    and the honest answer is that we do not sell it in that size. "Balloons in
    A3" is not: we do not sell balloons in any size, and saying "would A4 do?"
    about them would suspend a request to ask a question with no answer, and
    count a refusal under the wrong code in a trail built to be grouped by it.

    Args:
        decided: The verdict the four rules reached, ignoring the size.
        best_match: The universe item the line names, if it names one.

    Returns:
        Whether the veto overrides that verdict.
    """
    return best_match is not None or decided.decision is not ResolutionDecision.NO_CANDIDATE


def _head_terms(text: str, stated_terms: set[str]) -> set[str]:
    """The terms that end in the phrase's head noun — its last word.

    A joined pair counts as ending in the head, which is what keeps "card
    stock" meaning `Cardstock`: the head is "stock", and `cardstock` ends in
    it just as plainly as `stock` does, so the longer match still wins.

    Args:
        text: The phrase, already stripped of its sizes.
        stated_terms: The phrase's match terms.

    Returns:
        The terms that name the head.
    """
    words = _words(text)
    if not words:
        return set()
    head = words[-1]
    return {term for term in stated_terms if term.endswith(head)}


def _names_paper(text: str) -> bool:
    """Whether a phrase with no finish or material in it is asking for paper."""
    return any(word in PAPER_WORDS for word in _words(text))


def resolve_requested_line(line: RequestedLine) -> LineVerdict:
    """Decide one requested line: what it names, how many, and in what unit.

    The three checks run in the order the *one blocker per line* rule fixes —
    name, then quantity, then unit. A line that names something we do not sell
    and counts it in reams has failed once, not twice, and the audit trail must
    not read it as two rejections.

    Args:
        line: The requested line, in the customer's own words.

    Returns:
        The verdict: the exact item and count if it resolved, the decision that
        refused it otherwise.
    """
    name_verdict = resolve_name(line.item_as_stated)

    def verdict(
        decision: ResolutionDecision, detail: str, quantity: int | None = None
    ) -> LineVerdict:
        resolved = name_verdict.resolved_item if decision is ResolutionDecision.RESOLVED else None
        return LineVerdict(
            line_id=line.line_id,
            decision=decision,
            resolved_item=resolved,
            category=_CATALOGUE.category(resolved) if resolved else None,
            quantity=quantity,
            name_verdict=name_verdict,
            detail=detail,
        )

    if name_verdict.decision is not ResolutionDecision.RESOLVED:
        return verdict(name_verdict.decision, name_verdict.detail)

    if line.quantity_as_stated is None or line.quantity_as_stated <= 0:
        return verdict(
            ResolutionDecision.QUANTITY_MISSING,
            f"{name_verdict.resolved_item!r} resolved, but the line states no usable quantity "
            f"({line.quantity_as_stated!r})",
        )

    unit = _unpriceable_unit(line.unit_as_stated)
    if unit is not None:
        return verdict(
            ResolutionDecision.UNIT_NOT_UNDERSTOOD,
            f"{unit!r} is a unit we cannot price: it is some number of the thing we sell, "
            "and the data never says how many",
        )

    return verdict(
        ResolutionDecision.RESOLVED,
        name_verdict.detail,
        quantity=math.ceil(line.quantity_as_stated),
    )


def _unpriceable_unit(unit_as_stated: str | None) -> str | None:
    """The multiplier word in a stated unit, if it has one.

    A blocklist rather than an allowlist of units we accept, deliberately: an
    allowlist would refuse "300 poster boards", a line we can serve perfectly
    well, for naming its own goods.

    Args:
        unit_as_stated: The unit in the customer's own words, if they gave one.

    Returns:
        The multiplier word, or `None` if the unit is one we can price in.
    """
    if not unit_as_stated:
        return None
    for word in _words(unit_as_stated):
        if word in MULTIPLIER_UNITS:
            return word
    return None


def list_carried_catalogue(as_of_date: str) -> list[CarriedItem]:
    """List everything the business sells, with its category and what we hold.

    Carried and stocked are different answers: an item we hold none of is
    still on offer and still appears here, while an item that is not in this
    list is one we do not sell at all.

    Args:
        as_of_date: The date to read stock as of, as `YYYY-MM-DD`.

    Returns:
        One entry per carried item, in catalogue order.
    """
    _CATALOGUE.refresh()
    stock = starter.get_all_inventory(as_of_date)
    return [
        CarriedItem(
            item_name=name,
            category=_CATALOGUE.category(name),
            stock_on_hand=int(stock.get(name, 0)),
        )
        for name in sorted(_CATALOGUE.carried)
    ]


def check_stock(item_names: list[CarriedItemName], as_of_date: str) -> list[StockReading]:
    """Read how many units of each resolved item we hold on a given date.

    The authoritative per-item count, and the number a shortfall is computed
    from. Every name must be exactly as the carried catalogue spells it —
    anything else is refused rather than guessed at — so only ever pass names a
    verdict resolved to.

    Args:
        item_names: The exact carried-catalogue names to read.
        as_of_date: The date to read stock as of, as `YYYY-MM-DD`.

    Returns:
        One reading per item, in the order the items were given.
    """
    return [
        StockReading(
            item_name=item_name,
            stock_on_hand=read_stock(item_name, as_of_date),
            as_of=date.fromisoformat(as_of_date),
        )
        for item_name in item_names
    ]


def read_stock(item_name: str, as_of_date: str) -> int:
    """The stock reading itself, for code that must not take it from the model.

    `check_stock` is what the model calls and what the trail records; this is
    what the envelope is built from. A count the customer's order depends on is
    never read back out of a model's own report of it.

    Args:
        item_name: The exact carried-catalogue name.
        as_of_date: The date to read stock as of, as `YYYY-MM-DD`.

    Returns:
        The units on hand.
    """
    frame = starter.get_stock_level(item_name, as_of_date)
    if frame.empty:
        return 0
    return int(frame["current_stock"].iloc[0])
