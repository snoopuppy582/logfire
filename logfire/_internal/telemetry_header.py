"""SDK <-> server out-of-band metadata exchanged via custom HTTP headers.

* `X-Logfire-Telemetry` (request): non-sensitive information about the SDK and how
  it is configured. Used by the backend to answer questions like which SDK
  versions are still in active use, which Python versions we can drop, and which
  configuration options users actually enable. Secrets (`token`, `api_key`,
  `service_name`, etc.) are never included.
* `X-Logfire-Warning` (response): an out-of-band warning the server wants the
  user to see. Surfaced via `warnings.warn(...)`; the standard "default" filter
  deduplicates identical messages so a chatty server only warns once.
* `X-Logfire-Error` (response): an out-of-band error the server wants the SDK
  to raise. Always raised — callers that want to keep working past it (the OTLP
  pipeline, the variables provider) already swallow exceptions from their HTTP
  calls.
"""

from __future__ import annotations

import platform
import sys
import warnings
from typing import TYPE_CHECKING, Any

import requests

from logfire.exceptions import LogfireServerError, LogfireServerWarning
from logfire.version import VERSION

if TYPE_CHECKING:
    from .config import _LogfireConfigData  # pyright: ignore[reportPrivateUsage]


TELEMETRY_HEADER_NAME = 'X-Logfire-Telemetry'
WARNING_HEADER_NAME = 'X-Logfire-Warning'
ERROR_HEADER_NAME = 'X-Logfire-Error'


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return 'none'
    return str(value)


def _base_telemetry_pairs() -> dict[str, str]:
    return {
        'sdk_version': VERSION,
        'sdk_language': 'python',
        'python_version': platform.python_version(),
        'runtime': sys.implementation.name,
        'os': sys.platform,
    }


def _config_telemetry_pairs(config: _LogfireConfigData) -> dict[str, str]:
    """Pick fields of `_LogfireConfigData` that are useful for product analytics.

    Only non-sensitive booleans / counts / numeric values are included; never the token,
    api_key, service_name, environment, or anything else that could identify a user
    or their deployment.
    """
    pairs: dict[str, str] = {}
    pairs['send_to_logfire'] = _format_value(config.send_to_logfire)
    pairs['inspect_arguments'] = _format_value(config.inspect_arguments)
    pairs['distributed_tracing'] = _format_value(config.distributed_tracing)
    pairs['add_baggage_to_attributes'] = _format_value(config.add_baggage_to_attributes)
    pairs['min_level'] = _format_value(config.min_level)
    pairs['console_enabled'] = _format_value(config.console is not False)
    pairs['scrubbing_enabled'] = _format_value(config.scrubbing is not False)
    pairs['code_source_set'] = _format_value(config.code_source is not None)
    pairs['variables_set'] = _format_value(config.variables is not None)
    pairs['service_version_set'] = _format_value(config.service_version is not None)
    pairs['environment_set'] = _format_value(config.environment is not None)
    pairs['additional_span_processors'] = _format_value(len(config.additional_span_processors or ()))

    token = config.token
    if isinstance(token, list):
        token_count = len(token)
    elif token:
        token_count = 1
    else:
        token_count = 0
    pairs['token_count'] = _format_value(token_count)

    sampling = getattr(config, 'sampling', None)
    if sampling is not None:
        head = sampling.head
        if isinstance(head, (int, float)):
            pairs['sampling_head'] = _format_value(head)
        else:
            pairs['sampling_head'] = 'custom'
        pairs['sampling_tail'] = _format_value(sampling.tail is not None)

    return pairs


def build_telemetry_header(config: _LogfireConfigData | None = None) -> str:
    """Return the `key=val,key2=val` value for the `X-Logfire-Telemetry` header."""
    pairs = _base_telemetry_pairs()
    if config is not None:
        pairs.update(_config_telemetry_pairs(config))
    return ','.join(f'{key}={value}' for key, value in pairs.items())


def process_logfire_response_headers(response: requests.Response, *_args: Any, **_kwargs: Any) -> requests.Response:
    """Handle `X-Logfire-Warning` / `X-Logfire-Error` headers on a Logfire API response.

    Designed to be installed as a `requests` response hook
    (`session.hooks['response'].append(...)`).
    """
    warning_message = response.headers.get(WARNING_HEADER_NAME)
    if warning_message:
        warnings.warn(warning_message, LogfireServerWarning, stacklevel=2)
    error_message = response.headers.get(ERROR_HEADER_NAME)
    if error_message:
        raise LogfireServerError(error_message)
    return response


def install_logfire_response_hook(session: requests.Session) -> None:
    """Install `process_logfire_response_headers` as a response hook on `session`."""
    existing: Any = session.hooks.setdefault('response', [])
    hooks: list[Any] = list(existing) if isinstance(existing, list) else [existing]  # pyright: ignore[reportUnknownArgumentType]
    if process_logfire_response_headers not in hooks:
        hooks.append(process_logfire_response_headers)
    session.hooks['response'] = hooks
