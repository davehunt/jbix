"""Tests for jbix/people.py — PeopleClient."""

from unittest.mock import MagicMock, patch

import requests

from jbix.people import PeopleClient


def _make_response(dinos: list) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"total": len(dinos), "dinos": dinos}
    resp.raise_for_status.return_value = None
    return resp


def _dino(primary: str, secondary: str = "") -> dict:
    return {"primaryEmail": primary, "secondaryEmail": secondary, "username": primary.split("@")[0]}


class TestPeopleClientLookup:
    def setup_method(self):
        self.client = PeopleClient(token="test-token")

    def test_returns_primary_email_on_exact_primary_match(self):
        dino = _dino("dev@example.com")
        with patch("jbix.people.requests.get", return_value=_make_response([dino])):
            result = self.client.lookup("dev@example.com")
        assert result == "dev@example.com"

    def test_returns_primary_email_on_secondary_match(self):
        dino = _dino("dev@example.com", secondary="dev.personal@example.net")
        with patch("jbix.people.requests.get", return_value=_make_response([dino])):
            result = self.client.lookup("dev.personal@example.net")
        assert result == "dev@example.com"

    def test_returns_none_when_no_matching_dino(self):
        dino = _dino("other@example.com")
        with patch("jbix.people.requests.get", return_value=_make_response([dino])):
            result = self.client.lookup("notfound@example.com")
        assert result is None

    def test_returns_none_on_http_error(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
        with patch("jbix.people.requests.get", return_value=resp):
            result = self.client.lookup("user@example.com")
        assert result is None

    def test_returns_none_on_request_exception(self):
        with patch("jbix.people.requests.get", side_effect=requests.ConnectionError("timeout")):
            result = self.client.lookup("user@example.com")
        assert result is None

    def test_caches_result_on_second_call(self):
        dino = _dino("dev@example.com", secondary="dev.personal@example.net")
        mock_get = MagicMock(return_value=_make_response([dino]))
        with patch("jbix.people.requests.get", mock_get):
            first = self.client.lookup("dev.personal@example.net")
            second = self.client.lookup("dev.personal@example.net")
        assert first == "dev@example.com"
        assert second == "dev@example.com"
        mock_get.assert_called_once()

    def test_caches_none_result(self):
        mock_get = MagicMock(return_value=_make_response([]))
        with patch("jbix.people.requests.get", mock_get):
            first = self.client.lookup("nobody@example.com")
            second = self.client.lookup("nobody@example.com")
        assert first is None
        assert second is None
        mock_get.assert_called_once()

    def test_passes_correct_auth_header(self):
        client = PeopleClient(token="my-secret-token")
        mock_get = MagicMock(return_value=_make_response([]))
        with patch("jbix.people.requests.get", mock_get):
            client.lookup("user@example.com")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer my-secret-token"

    def test_passes_email_as_query_param(self):
        mock_get = MagicMock(return_value=_make_response([]))
        with patch("jbix.people.requests.get", mock_get):
            self.client.lookup("user@example.com")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["q"] == "user@example.com"
        assert kwargs["params"]["w"] == "all"
