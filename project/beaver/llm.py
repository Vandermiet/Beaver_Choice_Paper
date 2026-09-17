"""Model construction.

One place, because the version trap here is easy to re-introduce: the bare
`'openai:'` prefix resolves to `OpenAIResponsesModel` and would reach
`/v1/responses`, while the Vocareum proxy serves Chat Completions. The model is
therefore constructed as `OpenAIChatModel` explicitly, never by prefix string.
"""

import functools
import os

import dotenv
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

VOCAREUM_BASE_URL = "https://openai.vocareum.com/v1"
DEFAULT_MODEL_NAME = "gpt-4o-mini"
API_KEY_VAR = "UDACITY_OPENAI_API_KEY"


class MissingCredentialsError(RuntimeError):
    """Raised when there is no credential to reach the proxy with.

    Its own type, because the orchestrator has to tell this apart from every
    other way a request can fail. A bug in one request is that request's
    problem and the run carries on; a missing credential is the run's problem
    and every remaining request would fail identically, so it is allowed past
    the orchestrator's catch-all rather than being answered with an apology.
    """


def _api_key() -> str:
    """Read the proxy credential, or say plainly that there isn't one.

    Returns:
        The key, stripped.

    Raises:
        MissingCredentialsError: If the variable is unset, empty, or blank.
            Empty counts as missing because the common way to get here is a
            `.env` line with nothing after the `=`, which `os.environ` reports
            as a perfectly good empty string.
    """
    dotenv.load_dotenv()
    key = os.environ.get(API_KEY_VAR, "").strip()
    if not key:
        raise MissingCredentialsError(
            f"{API_KEY_VAR} is not set, so there is no credential to reach the "
            f"Vocareum proxy at {VOCAREUM_BASE_URL} with. Put it in a .env "
            f"file at the repository root as `{API_KEY_VAR}=...`, or export it "
            "into the environment, and run again. The .env file is gitignored, "
            "so a fresh clone of this repository never has one."
        )
    return key


def build_model(model_name: str = DEFAULT_MODEL_NAME) -> OpenAIChatModel:
    """Build the chat model every agent runs on.

    Args:
        model_name: The proxy's model name. `/v1/models` is blocked on the
            proxy, so the catalogue cannot be enumerated; `gpt-4o-mini` is
            confirmed working, including strict tool schemas.

    Returns:
        An `OpenAIChatModel` pointed at the Vocareum proxy.

    Raises:
        MissingCredentialsError: If no API key is configured.
    """
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=VOCAREUM_BASE_URL,
            api_key=_api_key(),
        ),
    )


@functools.cache
def shared_model() -> OpenAIChatModel:
    """The one model instance every agent in the system runs on.

    Built on first use rather than at import, so that importing an agent module
    needs no API key — which is what lets the whole system be exercised under a
    scripted model with no network and no credentials.

    Returns:
        The shared `OpenAIChatModel`.

    Raises:
        MissingCredentialsError: If no API key is configured. Not cached:
            `functools.cache` stores return values only, so a run that fixes
            its environment and retries gets a fresh attempt.
    """
    return build_model()
