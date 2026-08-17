"""LDAP (Active Directory) authentication.

Flow (validated against ericsson.se / EGAD):

1. Bind as the service account (``ldap_bind_user`` / ``ldap_bind_password``).
2. Search under ``ldap_base_dn`` for the entry whose ``sAMAccountName`` matches
   the login name, reading the EID / display / department attributes.
3. Re-bind with that entry's DN + the password the user typed. A successful
   bind proves the password; anything else is an invalid credential.

Only the backend ever holds the service password or performs binds — the
browser only sends the user's typed username + password over TLS.

Every failure carries a ``reason`` so the caller can tell apart causes that all
used to surface as one indistinguishable 401: the login name not resolving at
all (very often someone typing their e-mail instead of their signum), an
expired password, a locked or disabled account, and an LDAP outage during the
user re-bind. Failures are logged server-side with that reason; passwords are
never logged.
"""
from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.config import get_settings

logger = logging.getLogger(__name__)

#: What the user is told when the login name resolves to nothing, or resolves
#: but the password is simply wrong. Deliberately identical for both so the
#: endpoint can't be used to enumerate accounts — the distinction lives in the
#: server log. The signum hint is here because the single most common cause is
#: someone typing their e-mail address (which is NOT the sAMAccountName).
BAD_CREDENTIALS_MSG = "用户名或密码错误（用户名请填公司 signum，例如 ezhgmio，不是邮箱）"

#: AD packs the real reason for an invalidCredentials result into the
#: diagnostic message as "data <hex>". Each maps to (reason, user-facing text).
#: 525/52e stay generic on purpose (see BAD_CREDENTIALS_MSG); the rest describe
#: an account state the user must act on, so saying so is worth more than the
#: marginal enumeration risk.
AD_SUBCODES: dict[str, tuple[str, str]] = {
    "525": ("user_not_found", BAD_CREDENTIALS_MSG),
    "52e": ("bad_password", BAD_CREDENTIALS_MSG),
    "530": ("time_restricted", "当前时间不允许登录，请联系 IT"),
    "531": ("workstation_restricted", "当前设备不允许登录，请联系 IT"),
    "532": ("password_expired", "公司密码已过期，请先修改密码后再登录"),
    "533": ("account_disabled", "账号已被禁用，请联系 IT"),
    "701": ("account_expired", "账号已过期，请联系 IT"),
    "773": ("must_change_password", "需要先修改公司密码（首次登录或密码被重置），修改后再登录"),
    "775": ("account_locked", "账号已被锁定（连续多次密码错误），请稍后重试或联系 IT 解锁"),
}

_SUBCODE_RE = re.compile(r"\bdata\s+([0-9a-fA-F]+)\b")


class LdapAuthError(Exception):
    """Authentication failed (bad credentials) or LDAP was unreachable.

    ``retryable`` distinguishes an operational failure (server down / timeout),
    which should surface as 503, from a plain bad-credential 401. ``reason`` is
    a stable machine-readable tag for logs and metrics — never shown to the
    user, who only sees the message.
    """

    def __init__(
        self, message: str, *, retryable: bool = False, reason: str = "unknown"
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


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
        raise LdapAuthError("用户名和密码不能为空", reason="empty_input")
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
        # Affects EVERY login, not just this user: bad service password, DNS,
        # firewall, or the whole directory being down.
        logger.error("LDAP service bind failed (affects all logins): %s", exc)
        raise LdapAuthError(
            f"LDAP 服务不可用: {exc}", retryable=True, reason="service_bind_failed"
        ) from exc

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
            # NOT a wrong password: the login name matched no entry. Usually an
            # e-mail address / "DOMAIN\name" typed instead of the signum, or an
            # account outside ldap_base_dn.
            logger.warning(
                "LDAP login failed: user %r not found (base=%s filter=%s referrals=%s)",
                username,
                settings.ldap_base_dn,
                flt,
                svc.result.get("referrals") if svc.result else None,
            )
            raise LdapAuthError(BAD_CREDENTIALS_MSG, reason="user_not_found")
        if len(svc.entries) > 1:
            # sAMAccountName is unique per domain, so this means a stale or
            # cross-domain duplicate. We bind the first hit, which may be the
            # wrong object — worth knowing about rather than silently guessing.
            logger.warning(
                "LDAP login: %d entries matched user %r, using the first (%s)",
                len(svc.entries),
                username,
                svc.entries[0].entry_dn,
            )
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
            logger.warning(
                "LDAP login failed: no %s / sAMAccountName on %s",
                settings.ldap_eid_attr,
                user_dn,
            )
            raise LdapAuthError("无法确定该账号的 EID", reason="no_eid")
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
        bound = user_conn.bind()
    except LDAPException as exc:
        # An operational failure (timeout, TLS handshake, connection reset) —
        # NOT a wrong password. `ldaps://ericsson.se` round-robins across DCs,
        # so this connection can land on a different (slow/unreachable) DC than
        # the service bind did. Reporting it as 401 sent people off chasing
        # their password; it is a 503.
        logger.error(
            "LDAP user re-bind errored for %r (dn=%s): %s: %s",
            username,
            user_dn,
            type(exc).__name__,
            exc,
        )
        raise LdapAuthError(
            "LDAP 服务暂时不可用，请稍后重试", retryable=True, reason="rebind_error"
        ) from exc

    if not bound:
        result = user_conn.result or {}
        description = str(result.get("description") or "")
        message = str(result.get("message") or "")
        match = _SUBCODE_RE.search(message)
        subcode = match.group(1).lower() if match else ""
        reason, user_message = AD_SUBCODES.get(
            subcode, ("bad_password", BAD_CREDENTIALS_MSG)
        )
        logger.warning(
            "LDAP login failed for %r (dn=%s): %s data=%s reason=%s",
            username,
            user_dn,
            description or "bind refused",
            subcode or "?",
            reason,
        )
        raise LdapAuthError(user_message, reason=reason)

    try:
        user_conn.unbind()
    except Exception:
        pass

    logger.info("LDAP login OK: %r -> eid=%s", username, eid)
    return LdapUser(eid=eid, display_name=display_name, department=department, dn=user_dn)
