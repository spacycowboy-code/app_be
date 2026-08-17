"""How a typed login name is resolved to one directory entry.

The confirmed cause of "some people get 401" was people typing their e-mail
address: the filter only matched ``sAMAccountName``, and a UPN is usually
unrelated to the signum (signum ``ezhgmio`` vs UPN
``tracy.m.zhang@ericsson.com``), so the search found nothing and the login was
refused as if the password were wrong. Both forms name the same account, so both
are accepted — these tests pin that down, plus the ``DOMAIN\\name`` form Windows
shows people, and which entry wins when the widened filter matches several.

Resolution must not change the resulting identity: the EID comes from the
directory entry, so whichever form was typed, the same account yields the same
EID — everything that keys off ownership depends on that.
"""
import pytest

from app.config import get_settings
from app.services import ldap_auth

from tests.test_login_failure_reasons import _Entry, _ServiceConn, _UserConn


class _BoundConn(_UserConn):
    """A user connection whose bind succeeds."""

    def bind(self):
        return True


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_ldap(monkeypatch):
    """Install a fake ldap3 layer and record the filter the search received."""
    monkeypatch.setattr(ldap_auth, "_server", lambda: object())
    seen: dict = {}

    def install(entries):
        calls = {"n": 0}

        class _RecordingServiceConn(_ServiceConn):
            def search(self, base, flt, **kwargs):
                seen["base"] = base
                seen["filter"] = flt
                return super().search(base, flt, **kwargs)

        def _connection(*_args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _RecordingServiceConn(entries)
            seen["bind_dn"] = kwargs.get("user")
            return _BoundConn()

        monkeypatch.setattr(ldap_auth, "Connection", _connection)
        return seen

    return install


def _entry(dn: str, sam: str, uid: str, upn: str | None = None) -> _Entry:
    attrs = {"uid": uid, "displayName": "Some One", "department": "RP AI", "sAMAccountName": sam}
    if upn:
        attrs["userPrincipalName"] = upn
    return _Entry(dn, attrs)


# --- Filter construction ----------------------------------------------------


def test_a_plain_signum_searches_only_samaccountname():
    flt = ldap_auth.build_search_filter("ezhgmio")

    assert flt == "(sAMAccountName=ezhgmio)"


def test_an_address_also_searches_the_mail_attributes():
    flt = ldap_auth.build_search_filter("tracy.m.zhang@ericsson.com")

    assert flt.startswith("(|")
    assert "(sAMAccountName=tracy.m.zhang@ericsson.com)" in flt
    assert "(userPrincipalName=tracy.m.zhang@ericsson.com)" in flt
    assert "(mail=tracy.m.zhang@ericsson.com)" in flt


def test_the_mail_attributes_are_configurable(monkeypatch):
    monkeypatch.setenv("LDAP_EMAIL_LOGIN_ATTRS", "userPrincipalName")
    get_settings.cache_clear()

    flt = ldap_auth.build_search_filter("a@b.com")

    assert "(mail=" not in flt
    assert "(userPrincipalName=a@b.com)" in flt


def test_filter_input_is_escaped():
    """A login name is untrusted input going into an LDAP filter."""
    flt = ldap_auth.build_search_filter("ez(|)*hgmio")

    # The metacharacters that could break out of the filter component are
    # hex-escaped, so the value can't inject clauses or a wildcard.
    assert flt == r"(sAMAccountName=ez\28|\29\2ahgmio)"
    assert "(|" not in flt


def test_an_address_with_metacharacters_is_escaped_in_every_clause():
    flt = ldap_auth.build_search_filter("a*b@ericsson.com")

    assert r"\2a" in flt
    assert flt.count(r"a\2ab@ericsson.com") == len(flt.split("(")) - 2


# --- Name normalisation -----------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("ezhgmio", "ezhgmio"),
        ("  ezhgmio  ", "ezhgmio"),
        ("ERICSSON\\ezhgmio", "ezhgmio"),
        ("ericsson/ezhgmio", "ezhgmio"),
        ("tracy.m.zhang@ericsson.com", "tracy.m.zhang@ericsson.com"),
    ],
)
def test_normalize_login_name(typed, expected):
    assert ldap_auth.normalize_login_name(typed) == expected


def test_domain_prefix_does_not_reach_the_filter(fake_ldap):
    seen = fake_ldap([_entry("CN=ezhgmio,DC=ericsson,DC=se", "ezhgmio", "EZHGMIO")])

    ldap_auth.authenticate("ERICSSON\\ezhgmio", "pw")

    assert seen["filter"] == "(sAMAccountName=ezhgmio)"


# --- End to end through authenticate() --------------------------------------


def test_login_by_email_resolves_to_the_same_account(fake_ldap):
    """The whole point: an e-mail address is a valid way to name the account."""
    dn = "CN=ezhgmio,OU=User,DC=ericsson,DC=se"
    seen = fake_ldap([_entry(dn, "ezhgmio", "EZHGMIO", "tracy.m.zhang@ericsson.com")])

    user = ldap_auth.authenticate("tracy.m.zhang@ericsson.com", "pw")

    # Bound as the entry's DN, and the EID comes from the entry — identical to
    # what logging in as "ezhgmio" produces.
    assert seen["bind_dn"] == dn
    assert user.eid == "EZHGMIO"
    assert user.dn == dn


def test_login_by_signum_still_works(fake_ldap):
    dn = "CN=ezhgmio,OU=User,DC=ericsson,DC=se"
    fake_ldap([_entry(dn, "ezhgmio", "EZHGMIO", "tracy.m.zhang@ericsson.com")])

    user = ldap_auth.authenticate("ezhgmio", "pw")

    assert user.eid == "EZHGMIO"


def test_an_exact_signum_match_wins_over_other_hits(fake_ldap):
    """A widened filter can match several entries; the choice must be stable."""
    alias = _entry("CN=alias,OU=User,DC=ericsson,DC=se", "someoneelse", "SOMEONEELSE")
    exact = _entry("CN=ezhgmio,OU=User,DC=ericsson,DC=se", "ezhgmio", "EZHGMIO")
    seen = fake_ldap([alias, exact])

    user = ldap_auth.authenticate("ezhgmio", "pw")

    assert user.eid == "EZHGMIO"
    assert seen["bind_dn"] == "CN=ezhgmio,OU=User,DC=ericsson,DC=se"


def test_without_an_exact_signum_match_the_first_hit_is_used(fake_ldap):
    """E-mail logins have no signum to match on — keep the previous behaviour."""
    first = _entry("CN=first,OU=User,DC=ericsson,DC=se", "first", "FIRST")
    second = _entry("CN=second,OU=User,DC=ericsson,DC=se", "second", "SECOND")
    fake_ldap([first, second])

    user = ldap_auth.authenticate("shared.alias@ericsson.com", "pw")

    assert user.eid == "FIRST"
