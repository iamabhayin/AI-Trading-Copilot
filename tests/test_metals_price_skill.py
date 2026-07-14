"""Tests for skills/metals_price_skill.py.

Network (requests) and Ollama calls are mocked so these run offline.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from skills.metals_price_skill import (
    _template_message,
    build_metals_digest,
    fetch_metal_rates,
    format_metals_digest,
    notify_metals_digest,
)

GOLD_HTML = """
<table class="gr-table table-conatiner">
  <thead><tr><th>Gram</th><th>24K</th><th>22K</th><th>18K</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>&#x20b9;14,328<span class="gr-delta-down">(-213)</span></td><td>&#x20b9;13,135</td><td>&#x20b9;10,750</td></tr>
    <tr><td>8</td><td>&#x20b9;1,14,624</td><td>&#x20b9;1,05,080</td><td>&#x20b9;86,000</td></tr>
    <tr><td>10</td><td>&#x20b9;1,43,280<span class="gr-delta-down">(-2,130)</span></td><td>&#x20b9;1,31,350</td><td>&#x20b9;1,07,500</td></tr>
  </tbody>
</table>
"""

SILVER_HTML = """
<table class="gr-table table-conatiner">
  <thead><tr><th>Gram</th><th>Today</th><th>Yesterday</th><th>Change</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>&#x20b9;245</td><td>&#x20b9;244.90</td><td>+&nbsp;&#x20b9;0.10</td></tr>
    <tr><td>10</td><td>&#x20b9;2,450</td><td>&#x20b9;2,449</td><td>+&nbsp;&#x20b9;1</td></tr>
  </tbody>
</table>
"""

RATES = {"city": "Jaipur", "gold_per_gram": 14328.0, "gold_per_10g": 143280.0, "silver_per_gram": 245.0}


def _mock_html_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


@patch("skills.metals_price_skill.requests.get")
def test_fetch_metal_rates_parses_gold_and_silver_tables(mock_get):
    mock_get.side_effect = [_mock_html_response(GOLD_HTML), _mock_html_response(SILVER_HTML)]

    rates = fetch_metal_rates("Jaipur")

    assert rates == RATES
    gold_url, silver_url = (call.args[0] for call in mock_get.call_args_list)
    assert "gold-rates/jaipur.html" in gold_url
    assert "silver-rates/jaipur.html" in silver_url


@patch("skills.metals_price_skill.requests.get")
def test_fetch_metal_rates_raises_on_missing_table(mock_get):
    mock_get.return_value = _mock_html_response("<html><body>no table here</body></html>")

    with pytest.raises(ValueError):
        fetch_metal_rates("Jaipur")


@patch("skills.metals_price_skill.requests.get")
def test_fetch_metal_rates_raises_on_network_error(mock_get):
    mock_get.side_effect = requests.RequestException("connection refused")

    with pytest.raises(requests.RequestException):
        fetch_metal_rates("Jaipur")


def test_template_message_includes_all_rates():
    message = _template_message(RATES, "Jaipur")
    assert "Jaipur" in message
    assert "14,328" in message
    assert "143,280" in message
    assert "245" in message


def test_template_message_reports_unavailable_when_rates_missing():
    message = _template_message(None, "Jaipur")
    assert "unavailable" in message.lower()


@patch("skills.metals_price_skill.Settings.load")
def test_format_metals_digest_without_ollama_returns_template(mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")
    message = format_metals_digest(RATES)
    assert message == _template_message(RATES, "Jaipur")


@patch("skills.metals_price_skill.requests.post")
@patch("skills.metals_price_skill.Settings.load")
def test_format_metals_digest_uses_ollama_when_configured(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(json=lambda: {"response": "Polished Jaipur digest"})

    message = format_metals_digest(RATES)

    assert message == "Polished Jaipur digest"


@patch("skills.metals_price_skill.requests.post")
@patch("skills.metals_price_skill.Settings.load")
def test_format_metals_digest_fails_open_on_ollama_error(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.side_effect = requests.RequestException("connection refused")

    message = format_metals_digest(RATES)

    assert message == _template_message(RATES, "Jaipur")


@patch("skills.metals_price_skill.Settings.load")
@patch("skills.metals_price_skill.fetch_metal_rates")
def test_build_metals_digest_reports_unavailable_on_fetch_failure(mock_fetch, mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")
    mock_fetch.side_effect = ValueError("rate table not found in page markup")

    digest = build_metals_digest("Jaipur")

    assert "unavailable" in digest.lower()


@patch("skills.metals_price_skill.Settings.load")
@patch("skills.metals_price_skill.fetch_metal_rates")
def test_build_metals_digest_includes_rates_on_success(mock_fetch, mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")
    mock_fetch.return_value = RATES

    digest = build_metals_digest("Jaipur")

    assert "14,328" in digest


@patch("skills.metals_price_skill.route_message")
@patch("skills.metals_price_skill.build_metals_digest")
def test_notify_metals_digest_routes_to_discord_only(mock_build_digest, mock_route_message):
    mock_build_digest.return_value = "digest text"

    notify_metals_digest("Jaipur")

    mock_route_message.assert_called_once_with("metals_price", "digest text", send_telegram=False)
