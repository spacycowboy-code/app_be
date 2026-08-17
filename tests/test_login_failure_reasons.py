"""Login failures must be distinguishable.

Every rejected login used to look the same from the outside AND in the log: one
401 saying 「用户名或密码错误」. That made "some people can log in, some get
401" undiagnosable, so these tests pin down the distinctions that were added:

* the login name resolving to nothing is tagged ``user_not_found`` — separately
  from a wrong password — while still telling the user the same thing, so the
  endpoint can't be used to enumerate accounts;
* AD's ``data <subcode>`` diagnostic is decoded, so an expired password or a
  locked account says so instead of blaming the password;
* an LDAP outage during the *user* re-bind is retryable (503), not a 401 —
  ``ldaps://ericsson.se`` round-robins across DCs, and reporting a timeout as
  bad credentials sent people off resetting passwords that were fine.

No directory is contacted: ldap3's ``Connection`` is replaced with a fake.
"""
import pytest
from fastapi.testclient import TestClient
from ldap3.core.exceptions import LDAPException

from app.config import get_settings
from app.main import app
from app.services import ldap_auth

USER_DN = "CN=esomeone,OU=User,DC=ericsson,DC=se"


class _Attr:
    def __init__(self, value):
        self.value = value


class _Entry:
    """Minimal stand-in for an ldap3 entry (``entry_dn`` + ``entry[attr].value``)."""

    def __init__(self, dn: str, attrs: dict):
        self.entry_dn = dn
        self._attrs = attrs

    def __getitem__(self, name):
        if name not in self._attrs:
            raise KeyError(name)
        return _Attr(self._attrs[name])


class _ServiceConn:
    """The service-account connection: searches, then gets unbound."""

    def __init__(self, entries):
        self.entries = entries
        self.result = {"referrals": None}

    def search(self, *_args, **_kwargs):
        return bool(self.entries)

    def unbind(self):
        return True


class _UserConn:
    """The user re-bind: either refused with an AD result, or raising."""

    def __init__(self, *, result=None, raises=None):
        self.result = result or {}
        self._raises = raises

    def bind(self):
        if self._raises is not None:
            raise self._raises
        return False

    def unbind(self):
        return True


@pytest.fixture
def fake_ldap(monkeypatch):
    """Install a fake ldap3 layer; the test decides what the user re-bind does.

    Returns a setter taking the entries the search finds and the connection the
    user re-bind gets.
    """
    monkeypatch.setattr(ldap_auth, "_server", lambda: object())

    def install(entries, user_conn):
        calls = {"n": 0}

        def _connection(*_args, **_kwargs):
            calls["n"] += 1
            # First construction is the service bind, second is the user bind.
            return _ServiceConn(entries) if calls["n"] == 1 else user_conn

        monkeypatch.setattr(ldap_auth, "Connection", _connection)

    return install


def _found_entry() -> _Entry:
    return _Entry(
        USER_DN,
        {"uid": "ESOMEONE", "displayName": "Some One", "department": "RP AI", "sAMAccountName": "esomeone"},
    )


def _invalid_credentials(subcode: str) -> dict:
    """An AD invalidCredentials result carrying its diagnostic sub-code."""
    return {
        "description": "invalidCredentials",
        "message": (
            f"80090308: LdapErr: DSID-0C0904DC, comment: AcceptSecurityContext error, "
            f"data {subcode}, v4563"
        ),
    }


def test_unresolvable_login_name_is_tagged_but_stays_generic(fake_ldap):
    """No entry matched: a distinct reason, an identical message to the user."""
    fake_ldap([], _UserConn())

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("someone@ericsson.com", "pw")

    assert excinfo.value.reason == "user_not_found"
    assert excinfo.value.retryable is False
    # Same text as a wrong password (no account enumeration), and it names the
    # single most common cause: an e-mail address typed instead of the signum.
    assert str(excinfo.value) == ldap_auth.BAD_CREDENTIALS_MSG
    assert "signum" in str(excinfo.value)


def test_wrong_password_is_tagged_bad_password(fake_ldap):
    fake_ldap([_found_entry()], _UserConn(result=_invalid_credentials("52e")))

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "wrong")

    assert excinfo.value.reason == "bad_password"
    assert str(excinfo.value) == ldap_auth.BAD_CREDENTIALS_MSG


@pytest.mark.parametrize(
    ("subcode", "reason", "expected_in_message"),
    [
        ("532", "password_expired", "密码已过期"),
        ("773", "must_change_password", "修改公司密码"),
        ("775", "account_locked", "锁定"),
        ("533", "account_disabled", "禁用"),
        ("701", "account_expired", "过期"),
    ],
)
def test_account_state_is_reported_instead_of_blaming_the_password(
    fake_ldap, subcode, reason, expected_in_message
):
    """These users' passwords may be perfectly correct — say what's wrong."""
    fake_ldap([_found_entry()], _UserConn(result=_invalid_credentials(subcode)))

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "pw")

    assert excinfo.value.reason == reason
    assert expected_in_message in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_unknown_subcode_falls_back_to_bad_password(fake_ldap):
    """An unrecognised diagnostic must not crash or leak the raw AD string."""
    fake_ldap([_found_entry()], _UserConn(result=_invalid_credentials("abcd")))

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "pw")

    assert excinfo.value.reason == "bad_password"
    assert "80090308" not in str(excinfo.value)


def test_missing_result_message_falls_back_to_bad_password(fake_ldap):
    """Some servers refuse a bind with no diagnostic at all."""
    fake_ldap([_found_entry()], _UserConn(result={}))

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "pw")

    assert excinfo.value.reason == "bad_password"


def test_rebind_outage_is_retryable_not_a_credential_error(fake_ldap):
    """A timeout on the user re-bind is an outage: 503, never 401."""
    fake_ldap([_found_entry()], _UserConn(raises=LDAPException("socket timeout")))

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "pw")

    assert excinfo.value.reason == "rebind_error"
    assert excinfo.value.retryable is True
    assert "密码" not in str(excinfo.value)


def test_empty_input_is_rejected_before_any_bind(fake_ldap):
    fake_ldap([_found_entry()], _UserConn())

    with pytest.raises(ldap_auth.LdapAuthError) as excinfo:
        ldap_auth.authenticate("esomeone", "")

    assert excinfo.value.reason == "empty_input"


# --- The endpoint's mapping of those reasons onto status codes ---------------


@pytest.fixture
def login_client(monkeypatch):
    """A client that can actually reach the login handler (auth switched on)."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "login-reason-test-secret")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _raise(error):
    def _authenticate(_username, _password):
        raise error

    return _authenticate


def test_credential_failure_is_401_carrying_the_message(login_client, monkeypatch):
    monkeypatch.setattr(
        ldap_auth,
        "authenticate",
        _raise(ldap_auth.LdapAuthError("公司密码已过期，请先修改密码后再登录",
                                      reason="password_expired")),
    )

    resp = login_client.post(
        "/api/auth/login", json={"username": "esomeone", "password": "pw"}
    )

    assert resp.status_code == 401
    # The frontend renders this detail, so the actionable text must survive.
    assert resp.json()["detail"] == "公司密码已过期，请先修改密码后再登录"


def test_ldap_outage_is_503(login_client, monkeypatch):
    monkeypatch.setattr(
        ldap_auth,
        "authenticate",
        _raise(ldap_auth.LdapAuthError("LDAP 服务暂时不可用，请稍后重试",
                                      retryable=True, reason="rebind_error")),
    )

    resp = login_client.post(
        "/api/auth/login", json={"username": "esomeone", "password": "pw"}
    )

    assert resp.status_code == 503


def test_the_password_never_reaches_the_log_or_the_response(login_client, monkeypatch, caplog):
    secret_password = "Sup3rSecret!Value"
    monkeypatch.setattr(
        ldap_auth,
        "authenticate",
        _raise(ldap_auth.LdapAuthError(ldap_auth.BAD_CREDENTIALS_MSG, reason="bad_password")),
    )

    with caplog.at_level("DEBUG"):
        resp = login_client.post(
            "/api/auth/login", json={"username": "esomeone", "password": secret_password}
        )

    assert resp.status_code == 401
    assert secret_password not in resp.text
    assert secret_password not in caplog.text
    # The reason IS logged — that is the whole point of the change.
    assert "reason=bad_password" in caplog.text
