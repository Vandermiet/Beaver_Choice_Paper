"""The four resolution rules, exercised without a model.

Resolution is the decision that determines whether this business can trade, and
every rule that decides it is ordinary function code — so these tests spend no
model call and cover the rules themselves rather than a transcript of them.

Seed 137 carries 18 of the 46 universe items, and what it does *not* carry is
precisely what the sample keeps asking for. That asymmetry is the subject.
"""

import pytest

from beaver.contract import BlockerCode, RequestedLine
from beaver.inventory.models import ProductCategory, ResolutionDecision
from beaver.inventory.tools import resolve_name, resolve_requested_line


def a_line(item: str, quantity: float | None = 100, unit: str | None = "sheets"):
    return RequestedLine(
        line_id="L1",
        item_as_stated=item,
        quantity_as_stated=quantity,
        unit_as_stated=unit,
    )


class TestTheDefaultPlainPaperItem:
    """A paper business that cannot sell printer paper has failed at the only
    thing it does. The phrasing appears in 9 of the 20 sample requests."""

    @pytest.mark.parametrize(
        "stated",
        [
            "printer paper",
            "printing paper",
            "copy paper",
            "white paper",
            "standard copy paper",
            "plain paper",
            "paper",
        ],
    )
    def test_generic_paper_resolves_to_a4_paper(self, seeded_db, stated):
        shortlist = resolve_name(stated)
        assert shortlist.decision is ResolutionDecision.RESOLVED
        assert shortlist.resolved_item == "A4 paper"

    def test_generic_is_not_ambiguous(self, seeded_db):
        """There is nothing to be ambiguous between — it would ask a question
        with one possible answer."""
        assert resolve_name("copy paper").decision is not ResolutionDecision.AMBIGUOUS

    def test_a_finish_word_beats_the_default(self, seeded_db):
        assert resolve_name("glossy printer paper").resolved_item == "Glossy paper"


class TestRuleOneFinishSelectsAndAdjectivesDrop:
    """Generosity is spent on adjectives, never on identity. The catalogue
    decides which words are which: a word it never uses is a sales adjective."""

    @pytest.mark.parametrize(
        "stated, expected",
        [
            ("high-quality white cardstock", "Cardstock"),
            ("heavy cardstock (white)", "Cardstock"),
            ("premium glossy paper", "Glossy paper"),
            ("sturdy kraft paper", "Kraft paper"),
            ("bright colored paper", "Colored paper"),
            ("card stock", "Cardstock"),
            ("100 lb cover stock", "100 lb cover stock"),
            ("photo paper", "Photo paper"),
        ],
    )
    def test_the_adjective_drops_and_the_material_selects(self, seeded_db, stated, expected):
        shortlist = resolve_name(stated)
        assert shortlist.decision is ResolutionDecision.RESOLVED
        assert shortlist.resolved_item == expected

    def test_a_colour_the_catalogue_names_is_not_an_adjective(self, seeded_db):
        """`Colored paper` is an item we sell, so "colored" selects. "White" is
        not, so it drops. The catalogue decides, not a word list."""
        assert resolve_name("colored paper (assorted colors)").resolved_item == (
            "Colored paper"
        )
        assert resolve_name("white paper").resolved_item == "A4 paper"


class TestRuleTwoTheSizeVeto:
    """A named size can disqualify a resolution but never select one."""

    def test_a_size_the_universe_sells_vetoes_nothing(self, seeded_db):
        assert resolve_name("A4 glossy paper").resolved_item == "Glossy paper"

    def test_a_size_absent_from_the_universe_refuses_the_line(self, seeded_db):
        shortlist = resolve_name("A3 glossy paper")
        assert shortlist.decision is ResolutionDecision.SIZE_VETO
        assert shortlist.unrecognised_size == "a3"
        assert shortlist.resolved_item is None

    @pytest.mark.parametrize("stated", ["A3 matte paper", "A5 invitation cards", "11x17 paper"])
    def test_the_veto_fires_whatever_the_line_would_otherwise_have_done(self, seeded_db, stated):
        assert resolve_name(stated).decision is ResolutionDecision.SIZE_VETO

    def test_the_veto_never_selects_the_size_that_survived_it(self, seeded_db):
        """The rejected alternative was selling them plain `A4 paper` — the
        worst outcome available, since it drops the only requirement that
        mattered."""
        assert resolve_name("A4 glossy paper").resolved_item != "A4 paper"

    def test_the_veto_disqualifies_a_resolution_and_needs_one_to_disqualify(self, seeded_db):
        """"A3 matte paper" is vetoed: matte paper is a product the universe
        names, and the honest answer is that we do not sell it in that size.
        "A3 balloons" is not — we sell balloons in no size, and asking "would
        A4 do?" about them would suspend a request to ask an unanswerable
        question, and file the refusal under the wrong code."""
        assert resolve_name("A3 matte paper").decision is ResolutionDecision.SIZE_VETO
        assert resolve_name("A3 balloons").decision is ResolutionDecision.NO_CANDIDATE
        assert resolve_name("balloons in A3").decision is ResolutionDecision.NO_CANDIDATE

    def test_a_generic_paper_line_in_a_vetoed_size_is_still_vetoed(self, seeded_db):
        """It would have resolved to the default plain-paper item, and that is
        the resolution the size disqualifies."""
        assert resolve_name("A3 paper").decision is ResolutionDecision.SIZE_VETO

    def test_eight_and_a_half_by_eleven_is_letter_which_the_universe_names(self, seeded_db):
        assert resolve_name('8.5" x 11" colored paper').resolved_item == "Colored paper"


class TestRuleThreeTheCategoryGuard:
    """What arrives on a pallet as large-format is not what was asked for as
    paper. `Poster paper` costs $0.25; the large-format roll costs $1.00."""

    def test_poster_paper_does_not_become_large_format(self, seeded_db):
        shortlist = resolve_name("colorful poster paper")
        assert shortlist.decision is ResolutionDecision.CATEGORY_GUARD
        assert shortlist.carried_candidates == ["Large poster paper (24x36 inches)"]
        assert shortlist.universe_best_match == "Poster paper"
        assert shortlist.universe_best_category is ProductCategory.PAPER

    def test_the_guard_raises_item_not_carried(self, seeded_db):
        assert (
            ResolutionDecision.CATEGORY_GUARD.blocker_code is BlockerCode.ITEM_NOT_CARRIED
        )

    def test_the_head_noun_says_what_the_thing_is(self, seeded_db):
        """"Recycled kraft paper envelopes" are envelopes. Matching two words
        of "kraft paper" does not make them sheets, so the guard refuses the
        substitution rather than selling paper to someone who wanted post."""
        shortlist = resolve_name("100% recycled kraft paper envelopes")
        assert shortlist.decision is ResolutionDecision.CATEGORY_GUARD
        assert shortlist.universe_best_match == "Envelopes"
        assert resolve_name("kraft paper").resolved_item == "Kraft paper"

    def test_a_size_that_names_the_large_format_item_clears_the_guard(self, seeded_db):
        """A size never selects, but it does say which universe item was meant
        — and 24x36 is large-format by name."""
        assert resolve_name('poster boards (24" x 36")').resolved_item == (
            "Large poster paper (24x36 inches)"
        )


class TestRuleFourAlwaysAsk:
    """Ambiguity is a property of the candidate set, not of how far apart the
    candidates are priced."""

    def test_two_survivors_raise_item_ambiguous(self, seeded_db):
        shortlist = resolve_name("banner paper")
        assert shortlist.decision is ResolutionDecision.AMBIGUOUS
        assert sorted(shortlist.carried_candidates) == [
            "Banner paper",
            "Rolls of banner paper (36-inch width)",
        ]

    def test_a_worse_match_is_not_a_survivor(self, seeded_db):
        """"Card stock" is `Cardstock`, not a choice between it and `Invitation
        cards`. Accounting for more of the customer's words is rule 1 doing its
        work — the narrowing that decides which candidates survive at all — and
        not a tie-break between candidates that already have."""
        shortlist = resolve_name("card stock")
        assert shortlist.decision is ResolutionDecision.RESOLVED
        assert shortlist.carried_candidates == ["Cardstock"]

    def test_the_price_spread_is_never_consulted(self, seeded_db):
        """$0.30 against $2.50 is 8.3x, and the rule does not care: no price
        band, no cheaper-by-default tie-break."""
        assert resolve_name("banner paper").resolved_item is None


class TestItemsWeSimplyDoNotSell:
    def test_a_universe_item_the_seed_did_not_carry_is_not_carried(self, seeded_db):
        shortlist = resolve_name("matte paper")
        assert shortlist.decision is ResolutionDecision.NO_CANDIDATE
        assert shortlist.universe_best_match == "Matte paper"

    @pytest.mark.parametrize("stated", ["balloons", "party streamers", "washi tape", "envelopes"])
    def test_things_outside_the_carried_catalogue_raise_item_not_carried(self, seeded_db, stated):
        decision = resolve_name(stated).decision
        assert decision.blocker_code is BlockerCode.ITEM_NOT_CARRIED

    def test_a_non_paper_product_never_falls_through_to_the_default(self, seeded_db):
        assert resolve_name("200 balloons").resolved_item is None


class TestQuantityAndUnits:
    """Refuse, do not convert. A ream-to-sheet conversion asserts an accounting
    fact the data never contains, on a line that then bills 500x what the
    customer read."""

    def test_a_ream_is_refused_rather_than_converted(self, seeded_db):
        verdict = resolve_requested_line(a_line("printer paper", 500, "reams"))
        assert verdict.decision is ResolutionDecision.UNIT_NOT_UNDERSTOOD
        assert verdict.quantity is None

    @pytest.mark.parametrize("unit", ["reams", "ream", "packs", "boxes", "cartons", "bundles"])
    def test_every_multiplier_unit_is_refused(self, seeded_db, unit):
        verdict = resolve_requested_line(a_line("A4 paper", 10, unit))
        assert verdict.decision is ResolutionDecision.UNIT_NOT_UNDERSTOOD

    @pytest.mark.parametrize("unit", ["sheets", "units", None, "poster boards", "pieces"])
    def test_a_unit_we_can_price_passes(self, seeded_db, unit):
        verdict = resolve_requested_line(a_line("glossy paper", 100, unit))
        assert verdict.decision is ResolutionDecision.RESOLVED

    def test_a_missing_quantity_asks_for_one(self, seeded_db):
        verdict = resolve_requested_line(a_line("printer paper", None, "sheets"))
        assert verdict.decision is ResolutionDecision.QUANTITY_MISSING

    def test_a_fractional_quantity_rounds_up(self, seeded_db):
        """Half a sheet is a sheet we hand over."""
        assert resolve_requested_line(a_line("printer paper", 100.4)).quantity == 101


class TestOneBlockerPerLine:
    """Resolution is judged before units, so a line that fails both fails once."""

    def test_an_uncarried_item_in_an_unpriceable_unit_fails_on_the_name(self, seeded_db):
        verdict = resolve_requested_line(a_line("300 rolls of party streamers", 300, "rolls"))
        assert verdict.decision.blocker_code is BlockerCode.ITEM_NOT_CARRIED

    def test_a_vetoed_size_in_an_unpriceable_unit_fails_on_the_size(self, seeded_db):
        verdict = resolve_requested_line(a_line("A3 paper", 5, "reams"))
        assert verdict.decision is ResolutionDecision.SIZE_VETO

    def test_a_line_with_neither_quantity_nor_a_carried_item_fails_on_the_item(self, seeded_db):
        verdict = resolve_requested_line(a_line("balloons", None, None))
        assert verdict.decision.blocker_code is BlockerCode.ITEM_NOT_CARRIED

    def test_every_decision_but_resolved_names_exactly_one_code(self, seeded_db):
        codes = [d.blocker_code for d in ResolutionDecision]
        assert codes.count(None) == 1
        assert ResolutionDecision.RESOLVED.blocker_code is None


class TestTheResolvedVerdict:
    def test_a_resolved_line_carries_its_exact_catalogue_name_and_category(self, seeded_db):
        verdict = resolve_requested_line(a_line("A4 glossy paper", 200, "sheets"))
        assert verdict.resolved_item == "Glossy paper"
        assert verdict.category is ProductCategory.PAPER
        assert verdict.quantity == 200

    def test_the_detail_says_why_and_is_never_empty(self, seeded_db):
        """It is the blocker's internal `detail`, and an audit that reads
        "refused" without a reason has audited nothing."""
        for stated in ["A3 paper", "colorful poster paper", "banner paper", "balloons"]:
            assert resolve_requested_line(a_line(stated)).detail.strip()
