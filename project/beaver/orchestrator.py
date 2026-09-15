"""The orchestrator: one customer request in, one customer-facing reply out.

It never acts on a system of record. It plans and drives the sequence of
delegations, reads the signals that come back, and is the only component that
writes customer-facing prose.

Ticket 101 stubs the seam so the harness runs end to end. The sequence, the
bounded retry and the outcome derivation land in tickets 103–108.
"""


async def handle_request(
    request_with_date: str,
    request_date: str,
    request_id: int,
) -> str:
    """Handle one customer request and return the reply the customer reads.

    This is the locked seam `handle_request(CustomerRequest) -> RequestResolution`
    in its stub form: ticket 102 introduces the kernel models and the arguments
    below become a `CustomerRequest`, the return becomes a `RequestResolution`,
    and the harness renders `resolution.customer_message`. The name and the
    call site do not move.

    Args:
        request_with_date: The customer's prose with the request date appended,
            exactly as the harness composes it. The `job` and `event` columns
            are deliberately not passed — they sharpen tone but carry no
            decision value.
        request_date: The ISO date the request arrived, which is also the date
            any resulting transaction is booked on.
        request_id: The harness's 1-based index for the request.

    Returns:
        The customer-facing reply.
    """
    return (
        f"Thank you for your enquiry of {request_date}. "
        "Our team is reviewing your request and will respond shortly. "
        f"(Placeholder reply for request {request_id}; the agent system is not yet wired.)"
    )
