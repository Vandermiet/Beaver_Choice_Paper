"""Model construction.

One place, because the version trap here is easy to re-introduce: the bare
`'openai:'` prefix resolves to `OpenAIResponsesModel` and would reach
`/v1/responses`, while the Vocareum proxy serves Chat Completions. The model is
therefore constructed as `OpenAIChatModel` explicitly, never by prefix string.
"""

import os

import dotenv
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

VOCAREUM_BASE_URL = "https://openai.vocareum.com/v1"
DEFAULT_MODEL_NAME = "gpt-4o-mini"


def build_model(model_name: str = DEFAULT_MODEL_NAME) -> OpenAIChatModel:
    """Build the chat model every agent runs on.

    Args:
        model_name: The proxy's model name. `/v1/models` is blocked on the
            proxy, so the catalogue cannot be enumerated; `gpt-4o-mini` is
            confirmed working, including strict tool schemas.

    Returns:
        An `OpenAIChatModel` pointed at the Vocareum proxy.
    """
    dotenv.load_dotenv()
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=VOCAREUM_BASE_URL,
            api_key=os.environ["UDACITY_OPENAI_API_KEY"],
        ),
    )
