"""LLM provider selection.

Everything else in the app depends on a LangChain ``BaseChatModel``, never on a
specific vendor SDK, so switching providers is an environment-variable change.

Providers are imported lazily so that a missing optional dependency for one
provider cannot stop the app from starting with another.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import Settings, get_settings

SUPPORTED_PROVIDERS = ("gemini", "vertexai", "openai", "fake")


class LLMConfigurationError(RuntimeError):
    """Raised when the selected provider is missing credentials or a package."""


def build_chat_model(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    provider = settings.llm_provider.strip().lower()

    if provider == "fake":
        from app.llm.fake import FakeChatModel

        return FakeChatModel()

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise LLMConfigurationError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set in .env"
            )
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise LLMConfigurationError(
                "langchain-google-genai is not installed; run "
                "`pip install -r requirements.txt`"
            ) from exc

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
            # This client's own default retry count (6, with internal
            # backoff) compounds with the with_retry() wrapper applied in
            # get_chat_model() below -- 5 outer attempts x 6 inner attempts
            # each with growing backoff turns one transient 503 into a
            # multi-minute worst case. Disable the inner layer and let
            # with_retry be the single, bounded retry policy.
            retries=0,
        )

    if provider == "vertexai":
        if not settings.google_cloud_project:
            raise LLMConfigurationError(
                "LLM_PROVIDER=vertexai requires GOOGLE_CLOUD_PROJECT to be set, "
                "plus application default credentials "
                "(gcloud auth application-default login) or "
                "GOOGLE_APPLICATION_CREDENTIALS pointing at a service account key."
            )
        try:
            from langchain_google_vertexai import ChatVertexAI
        except ImportError as exc:
            raise LLMConfigurationError(
                "LLM_PROVIDER=vertexai requires the optional dependency "
                "langchain-google-vertexai: pip install langchain-google-vertexai"
            ) from exc

        return ChatVertexAI(
            model_name=settings.gemini_model,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            temperature=settings.llm_temperature,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMConfigurationError(
                "LLM_PROVIDER=openai requires OPENAI_API_KEY to be set in .env"
            )
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise LLMConfigurationError(
                "LLM_PROVIDER=openai requires the optional dependency "
                "langchain-openai: pip install langchain-openai"
            ) from exc

        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=settings.llm_temperature,
        )

    raise LLMConfigurationError(
        f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
        f"Supported values: {', '.join(SUPPORTED_PROVIDERS)}."
    )


@lru_cache
def get_chat_model() -> BaseChatModel:
    """Cached model instance used by the API at runtime.

    Wrapped with retry/backoff: hosted LLM APIs return transient 429/503
    "overloaded" errors under normal operation, and without this a single
    momentary spike turns into a failed request instead of a slightly slower
    one. The offline fake model never raises this kind of error, so wrapping
    it is harmless.
    """
    settings = get_settings()
    model = build_chat_model(settings)
    if settings.llm_provider.strip().lower() == "fake":
        return model
    # Bounded: worst case is roughly 1+2+4 = 7s of backoff plus 4 request
    # round trips, not the many-minute cascade a larger count would produce
    # once every node in the graph (classify/generate/answer) hits it.
    return model.with_retry(stop_after_attempt=4, wait_exponential_jitter=True)
