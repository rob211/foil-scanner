"""Failure taxonomy. Every error here is fatal by design (spec section 8):
no defaults, no silent fallbacks, non-zero exit."""


class ScannerError(Exception):
    """Base class; anything raised from this module fails the run."""


class ConfigError(ScannerError):
    pass


class FetchError(ScannerError):
    pass


class SchemaError(ScannerError):
    pass


class StaleDataError(ScannerError):
    pass


class CalendarError(ScannerError):
    pass
