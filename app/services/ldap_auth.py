"""LDAP (Active Directory) authentication.

Flow (validated against ericsson.se / EGAD):

1. Bind as the service account (``ldap_bind_user`` / ``ldap_bind_password``).
2. Search under ``ldap_base_dn`` for the entry whose ``sAMAccountName`` matches
   the login name, reading the EID / display / department attributes.
3. Re-bind with that entry's DN + the password the user typed. A successful
   bind proves the password; anything else is an invalid credential.

Only the backend ever holds the service password or performs binds — the
browser only sends the user's typed username + password over TLS.
"""
from __future__ import annotations

import ssl
from dataclasses import dataclass

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.config import get_settings


class LdapAuthError(Exception):
    """Authentication failed (bad credentials) or LDAP was unreachable.

    ``retryable`` distinguishes an operational failure (server down / timeout),
    which should surface as 503, from a plain bad-credential 401.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class LdapUser:
    eid: str
    display_name: str | None
    department: str | None
    dn: str


def _server() -> Server:
    settings = get_settings()
    use_ssl = settings.ldap_url.lower().startswith("ldaps://")
    validate = ssl.CERT_REQUIRED if settings.ldap_verify_tls else ssl.CERT_NONE
    tls = Tls(validate=validate) if use_ssl else None
    # ldap3 parses host/port/ssl from the URL when passed as-is.
    return Server(settings.ldap_url, use_ssl=use_ssl, tls=tls, get_info=ALL)


def authenticate(username: str, password: str) -> LdapUser:
    """Verify ``username``/``password`` against AD and return the user's attrs.

    Raises ``LdapAuthError`` on bad credentials (retryable=False) or on an
    operational LDAP failure (retryable=True).
    """
    settings = get_settings()
    username = (username or "").strip()
    if not username or not password:
        raise LdapAuthError("用户名和密码不能为空")
    # An empty password can produce an "unauthenticated bind" that AD may accept
    # as anonymous — guarded above, but keep the invariant explicit.

    server = _server()

    # 1) Bind as the service account and look the user up.
    try:
        svc = Connection(
            server,
            user=settings.ldap_bind_user,
            password=settings.ldap_bind_password,
            auto_bind=True,
            receive_timeout=settings.ldap_timeout,
        )
    except LDAPException as exc:  # service account / server problem
        raise LdapAuthError(f"LDAP 服务不可用: {exc}", retryable=True) from exc

    try:
        flt = settings.ldap_user_filter.format(username=escape_filter_chars(username))
        attrs = [
            settings.ldap_eid_attr,
            settings.ldap_display_attr,
            settings.ldap_department_attr,
            "sAMAccountName",
        ]
        ok = svc.search(
            settings.ldap_base_dn, flt, search_scope=SUBTREE, attributes=attrs
        )
        if not ok or not svc.entries:
            raise LdapAuthError("用户名或密码错误")
        entry = svc.entries[0]
        user_dn = entry.entry_dn

        def _attr(name: str) -> str | None:
            try:
                val = entry[name].value
            except Exception:
                return None
            if isinstance(val, (list, tuple)):
                val = val[0] if val else None
            return str(val) if val else None

        eid = _attr(settings.ldap_eid_attr) or _attr("sAMAccountName")
        if not eid:
            raise LdapAuthError("无法确定该账号的 EID")
        eid = eid.strip().upper()
        display_name = _attr(settings.ldap_display_attr)
        department = _attr(settings.ldap_department_attr)
    finally:
        try:
            svc.unbind()
        except Exception:
            pass

    # 2) Re-bind as the user to verify the password.
    try:
        user_conn = Connection(
            server, user=user_dn, password=password, receive_timeout=settings.ldap_timeout
        )
        if not user_conn.bind():
            raise LdapAuthError("用户名或密码错误")
        try:
            user_conn.unbind()
        except Exception:
            pass
    except LDAPException as exc:
        # A bind failure for bad password raises here on some servers.
        raise LdapAuthError("用户名或密码错误") from exc

    return LdapUser(eid=eid, display_name=display_name, department=department, dn=user_dn)
