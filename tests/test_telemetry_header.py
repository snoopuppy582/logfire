from __future__ import annotations

import warnings
from unittest.mock import patch

import pytest
import requests
import requests_mock
from inline_snapshot import snapshot

import logfire
from logfire._internal.config import GLOBAL_CONFIG, LogfireCredentials
from logfire._internal.telemetry_header import (
    ERROR_HEADER_NAME,
    TELEMETRY_HEADER_NAME,
    WARNING_HEADER_NAME,
    build_telemetry_header,
    process_logfire_response_headers,
)
from logfire.exceptions import LogfireServerError, LogfireServerWarning
from logfire.version import VERSION


def _parse_header(value: str) -> dict[str, str]:
    return dict(part.split('=', 1) for part in value.split(','))


def test_build_telemetry_header_without_config():
    pairs = _parse_header(build_telemetry_header())
    assert pairs['sdk_version'] == VERSION
    assert pairs['sdk_language'] == 'python'
    assert pairs['python_version']
    assert pairs['runtime']
    assert pairs['os']


def test_build_telemetry_header_with_config():
    pairs = _parse_header(build_telemetry_header(GLOBAL_CONFIG))
    assert pairs['sdk_version'] == VERSION
    for key in ('code_source_set', 'variables_set', 'token_count'):
        assert key in pairs


def test_telemetry_header_excludes_secrets():
    """The header must never carry the token, api key, environment or service name."""
    secrets = ['shhh-secret-token', 'secret-api-key', 'top-secret-env', 'secret-service-name']
    with patch.dict('os.environ', {}, clear=False):
        logfire.configure(
            send_to_logfire=False,
            token=secrets[0],
            api_key=secrets[1],
            environment=secrets[2],
            service_name=secrets[3],
            console=False,
        )
    try:
        header = build_telemetry_header(GLOBAL_CONFIG)
        for secret in secrets:
            assert secret not in header
    finally:
        # Reset to the default test config.
        logfire.configure(send_to_logfire=False, console=False)


def test_otlp_export_sends_telemetry_header():
    captured: list[dict[str, str]] = []

    with requests_mock.Mocker() as m:
        m.get(
            'https://logfire-us.pydantic.dev/v1/info',
            json={'project_name': 'myproject', 'project_url': 'fake_project_url'},
        )

        def _capture(request: requests.PreparedRequest, _context: object) -> str:
            captured.append(dict(request.headers))
            return ''

        m.post('https://logfire-us.pydantic.dev/v1/traces', text=_capture, status_code=200)

        logfire.configure(send_to_logfire=True, token='abc1', console=False)
        for thread in __import__('threading').enumerate():
            if thread.name == 'check_logfire_token':  # pragma: no cover
                thread.join()

        with logfire.span('a span'):
            pass
        logfire.force_flush()

    assert any(TELEMETRY_HEADER_NAME in headers for headers in captured)
    [headers] = [headers for headers in captured if TELEMETRY_HEADER_NAME in headers]
    pairs = _parse_header(headers[TELEMETRY_HEADER_NAME])
    assert pairs['sdk_version'] == VERSION
    assert pairs['token_count'] == '1'
    assert 'abc1' not in headers[TELEMETRY_HEADER_NAME]
    # The header must advertise the same `service.instance.id` carried by OTLP
    # resource attributes so the backend can correlate the two.
    resource = GLOBAL_CONFIG.get_tracer_provider().resource
    assert pairs['service_instance_id'] == resource.attributes['service.instance.id']


def test_from_token_sends_telemetry_header():
    with requests_mock.Mocker() as m:
        m.get(
            'https://logfire-us.pydantic.dev/v1/info',
            json={'project_name': 'myproject', 'project_url': 'fake_project_url'},
        )
        session = requests.Session()
        LogfireCredentials.from_token(
            'pylf_v1_us_xxx', session, 'https://logfire-us.pydantic.dev', telemetry_header='sdk_version=1.2.3'
        )
        [history] = m.request_history
        assert history.headers[TELEMETRY_HEADER_NAME] == 'sdk_version=1.2.3'


def test_process_response_warning_header_emits_warning():
    response = requests.Response()
    response.headers[WARNING_HEADER_NAME] = 'The /foo/bar endpoint is deprecated, please use /bar/baz'
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        process_logfire_response_headers(response)
    assert [(w.category, str(w.message)) for w in caught] == snapshot(
        [(LogfireServerWarning, 'The /foo/bar endpoint is deprecated, please use /bar/baz')]
    )


def test_process_response_warning_header_dedupes():
    """Python's default `warnings` filter should fold repeats of the same message into one entry."""
    response = requests.Response()
    response.headers[WARNING_HEADER_NAME] = 'a duplicated warning'
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('default')
        for _ in range(5):
            process_logfire_response_headers(response)
    messages = [str(w.message) for w in caught]
    assert messages == ['a duplicated warning']


def test_process_response_error_header_raises():
    response = requests.Response()
    response.headers[ERROR_HEADER_NAME] = 'something is wrong'
    with pytest.raises(LogfireServerError, match='something is wrong'):
        process_logfire_response_headers(response)


def test_response_hook_installed_on_logfire_client():
    from logfire._internal.auth import UserToken
    from logfire._internal.client import LogfireClient

    token = UserToken(
        token='pylf_v1_us_xxx',
        base_url='https://logfire-us.pydantic.dev',
        expiration='2099-12-31T23:59:59',
    )
    client = LogfireClient(user_token=token)

    with requests_mock.Mocker() as m:
        m.get(
            'https://logfire-us.pydantic.dev/v1/account/me',
            json={'name': 'me'},
            headers={WARNING_HEADER_NAME: 'deprecated endpoint'},
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            client.get_user_information()

    assert any(isinstance(w.message, LogfireServerWarning) for w in caught)

    with requests_mock.Mocker() as m:
        m.get(
            'https://logfire-us.pydantic.dev/v1/account/me',
            json={'name': 'me'},
            headers={ERROR_HEADER_NAME: 'no longer supported'},
        )
        with pytest.raises(LogfireServerError, match='no longer supported'):
            client.get_user_information()

    [history, *_] = m.request_history
    assert TELEMETRY_HEADER_NAME in history.headers
    pairs = _parse_header(history.headers[TELEMETRY_HEADER_NAME])
    assert pairs['sdk_version'] == VERSION
