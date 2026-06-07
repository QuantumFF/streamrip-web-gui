"""Classification of a finished `rip search` run to a user-facing error (pure fn)."""
from app import classify_search_error


def test_success_returns_no_error():
    assert classify_search_error(0, "some results output") is None


def test_invalid_app_secret():
    out = "blah InvalidAppSecretError blah"
    assert classify_search_error(1, out) == (
        "Invalid Qobuz app secrets. Update your config with valid secrets or "
        "run 'rip config --update' in the container."
    )


def test_traceback():
    out = "Traceback (most recent call last):\n  File ..."
    assert classify_search_error(1, out) == (
        "Streamrip encountered an error (check logs for full traceback)"
    )


def test_authentication_failure():
    out = "Qobuz authentication error"
    assert classify_search_error(1, out) == (
        "Authentication failed - check your Qobuz credentials in config"
    )


def test_bad_credentials():
    out = "bad credentials supplied"
    assert classify_search_error(1, out) == (
        "Invalid credentials - check your Qobuz configuration"
    )


def test_generic_fallback_nonzero_unrecognised_stdout():
    assert classify_search_error(1, "something unexpected") == "Streamrip search failed"


def test_generic_fallback_empty_stdout():
    assert classify_search_error(1, "") == "Streamrip search failed"
