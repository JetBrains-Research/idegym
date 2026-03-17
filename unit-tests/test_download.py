from pytest import mark, param, raises

from scripts.download import http_url


@mark.parametrize(
    "expected",
    [
        param("http://example.com", id="http-url"),
        param("http://example.com/", id="http-url-trailing-slash"),
        param("http://example.com/example", id="http-url-path"),
        param("http://example.com/example.html", id="http-url-file"),
        param("https://example.com", id="https-url"),
        param("https://example.com/", id="https-url-trailing-slash"),
        param("https://example.com/example", id="https-url-path"),
        param("https://example.com/example.html", id="https-url-file"),
    ],
)
def test_http_url_valid(expected: str):
    actual = http_url(expected)
    assert actual == expected


@mark.parametrize(
    "expected",
    [
        param(None, id="none"),
        param("", id="empty"),
        param(" ", id="blank"),
        param("https://", id="no-domain"),
        param("example", id="no-tld"),
        param("example.com", id="netloc-only"),
        param("example.com/", id="trailing-slash"),
        param("example.com/example", id="netloc-and-path"),
    ],
)
def test_http_url_invalid(expected: str):
    with raises(Exception):
        http_url(expected)
