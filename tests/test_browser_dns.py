"""The captcha browser gets DoH answers for the site hosts, not the ISP's."""

import pytest

from aniworld.playwright import dns


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(dns, "_cache", {})
    monkeypatch.delenv("ANIWORLD_BROWSER_DOH", raising=False)
    monkeypatch.delenv("ANIWORLD_BROWSER_DOH_HOSTS", raising=False)


def _answers(monkeypatch, table):
    calls = []

    def query(session, host):
        calls.append(host)
        return table.get(host)

    monkeypatch.setattr(dns, "_query", query)
    monkeypatch.setattr(dns, "SITE_HOSTS", tuple(table))
    return calls


def test_hosts_are_pinned_with_and_without_www(monkeypatch):
    _answers(monkeypatch, {"aniworld.to": "190.115.16.17"})
    rules = "MAP aniworld.to 190.115.16.17, MAP www.aniworld.to 190.115.16.17"
    assert dns.browser_dns_args() == [f"--host-resolver-rules={rules}"]


def test_unresolved_serienstream_falls_back_to_the_known_ip(monkeypatch):
    _answers(monkeypatch, {"serienstream.to": None, "filmpalast.to": None})
    rules = dns.host_resolver_rules()
    assert f"MAP serienstream.to {dns.STO_IP}" in rules
    assert "filmpalast.to" not in rules


def test_nothing_resolved_leaves_dns_alone(monkeypatch):
    _answers(monkeypatch, {"filmpalast.to": None})
    assert dns.browser_dns_args() == []


def test_answers_are_cached(monkeypatch):
    calls = _answers(monkeypatch, {"aniworld.to": "190.115.16.17"})
    dns.host_resolver_rules()
    dns.host_resolver_rules()
    assert calls == ["aniworld.to"]


def test_can_be_switched_off(monkeypatch):
    calls = _answers(monkeypatch, {"aniworld.to": "190.115.16.17"})
    monkeypatch.setenv("ANIWORLD_BROWSER_DOH", "0")
    assert dns.browser_dns_args() == []
    assert calls == []


def test_extra_hosts_from_the_environment(monkeypatch):
    _answers(monkeypatch, {"aniworld.to": "190.115.16.17"})
    monkeypatch.setenv("ANIWORLD_BROWSER_DOH_HOSTS", " Example.org ,,")
    assert dns._hosts() == ["aniworld.to", "example.org"]


def test_only_ipv4_answers_are_used(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "Answer": [
                    {"type": 5, "data": "alias.example."},
                    {"type": 1, "data": "not-an-ip"},
                    {"type": 1, "data": "203.0.113.7"},
                ]
            }

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    assert dns._query(Session(), "example.org") == "203.0.113.7"
