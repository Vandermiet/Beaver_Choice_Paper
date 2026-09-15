"""The shared kernel: the derived blocker properties, the seam's narrowing, and
the one validator the "never a product we do not sell" guarantee rests on."""

import pytest
from pydantic import BaseModel, ValidationError

from beaver.contract import (
    AgentName,
    AgentResponse,
    AgentView,
    BlockerCode,
    BlockerSignal,
    CarriedItemName,
    InternalPayload,
    carried_catalogue,
    forget_carried_catalogue,
)


class Customer(BaseModel):
    note: str


class Internal(InternalPayload):
    cash_before: float


def a_response(signals: list[BlockerSignal] | None = None) -> AgentResponse:
    return AgentResponse[Customer, Internal](
        agent=AgentName.SALES,
        step_id="20260915T000000Z:1:001",
        request_id="1",
        customer=Customer(note="two reams on their way"),
        internal=Internal(cash_before=45059.70, signals=signals or []),
    )


class TestDerivedProperties:
    """Revisable, pausing and internal-only are read off the code, never set by
    the agent raising it — so two agents cannot disagree about one."""

    def test_a_pausing_code_is_also_revisable(self):
        code = BlockerCode.ITEM_AMBIGUOUS
        assert code.pausing
        assert code.revisable
        assert not code.internal_only

    def test_an_unmeetable_deadline_is_revisable_but_never_pauses(self):
        """It is raised at buy time, after sibling lines may already have been
        written, so it is spoken as prose rather than suspending the flow."""
        assert BlockerCode.DEADLINE_UNMEETABLE.revisable
        assert not BlockerCode.DEADLINE_UNMEETABLE.pausing

    def test_insufficient_stock_is_neither_revisable_nor_pausing(self):
        assert not BlockerCode.INSUFFICIENT_STOCK.revisable
        assert not BlockerCode.INSUFFICIENT_STOCK.pausing

    def test_only_cash_insufficient_is_internal_only(self):
        assert [c for c in BlockerCode if c.internal_only] == [BlockerCode.CASH_INSUFFICIENT]

    def test_the_signal_and_its_customer_blocker_cannot_disagree(self):
        """Because neither of them holds the answer — the code does."""
        signal = BlockerSignal(line_id="L1", code=BlockerCode.ITEM_AMBIGUOUS, detail="two fit")
        assert signal.to_customer().code is signal.code


class TestTheSeamNarrows:
    def test_the_view_carries_the_customer_payload_and_no_internal_half(self):
        view = a_response().to_view()
        assert isinstance(view, AgentView)
        assert view.customer.note == "two reams on their way"
        assert "cash_before" not in view.model_dump_json()
        assert not hasattr(view, "internal")

    def test_blockers_cross_as_code_and_line_only(self):
        view = a_response(
            [BlockerSignal(line_id="L1", code=BlockerCode.INSUFFICIENT_STOCK, detail="stock 120")]
        ).to_view()
        assert [(b.line_id, b.code) for b in view.blockers] == [
            ("L1", BlockerCode.INSUFFICIENT_STOCK)
        ]
        assert "stock 120" not in view.model_dump_json()


class TestCarriedItemName:
    """The guarantee that no transaction is written against a product we do not
    sell rests on this annotation, not on any agent's instructions."""

    class Line(BaseModel):
        item_name: CarriedItemName

    def test_a_catalogue_string_passes(self, seeded_db):
        assert self.Line(item_name="A4 paper").item_name == "A4 paper"

    def test_a_non_catalogue_string_raises(self, seeded_db):
        with pytest.raises(ValidationError, match="not in the carried catalogue"):
            self.Line(item_name="A3 glossy paper")

    def test_a_universe_item_the_seed_did_not_carry_raises(self, seeded_db):
        """`Letter-sized paper` is in `paper_supplies` and not in the seed-137
        inventory — the catalogue is what we stock, not what exists."""
        with pytest.raises(ValidationError):
            self.Line(item_name="Letter-sized paper")

    def test_the_catalogue_is_the_eighteen_seeded_items(self, seeded_db):
        assert len(carried_catalogue()) == 18

    def test_forgetting_the_cache_re_reads_the_database(self, seeded_db):
        carried_catalogue()
        forget_carried_catalogue()
        assert "A4 paper" in carried_catalogue()
