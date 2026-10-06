"""Domain errors with safe, inspectable messages."""


class SearchEngineError(Exception):
    """Base class for project-owned errors."""


class ConfigurationError(SearchEngineError):
    """Raised when a configuration profile is invalid."""


class UrlCanonicalizationError(SearchEngineError):
    """Raised when an input cannot be represented as a supported canonical URL."""


class UrlCollisionError(SearchEngineError):
    """Raised when one UrlId resolves to different canonical URLs."""


class InvalidLifecycleTransition(SearchEngineError):
    """Raised when a URL lifecycle state transition is not permitted."""


class DatabaseSchemaError(SearchEngineError):
    """Raised when mutable crawl state is newer than this executable supports."""
