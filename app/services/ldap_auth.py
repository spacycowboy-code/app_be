"""LDAP (Active Directory) authentication.

Flow (validated against ericsson.se / EGAD):

1. Bind as the service account (``ldap_bind_user`` / ``ldap_bind_password``).
2. Search under ``ldap_base_dn`` for the user's entry, reading the EID /
   display / department attributes. A signum matches ``sAMAccountName``; an
   address also matches ``userPrincipalName`` / ``mail`` (see
   ``build_search_filter``), so both ways of naming an account work.
3. Re-bind with that entry's DN + the password the user typed. A successful
   bind proves the password; anything else is an invalid credential.

Only the backend ever holds the service password or performs binds — the
browser only sends the user's typed username + password over TLS.

Every failure carries a ``reason`` so the caller can tell apart causes that all
used to surface as one indistinguishable 401: the login name not resolving at
all, an expired password, a locked or disabled account, and an LDAP outage
during the user re-bind. Failures are logged server-side with that reason;
passwords are never logged.
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
#: server log.
BAD_CREDENTIALS_MSG = "Incorrect username or password."

#: AD packs the real reason for an invalidCredentials result into the
#: diagnostic message as "data <hex>". Each maps to (reason, user-facing text).
#: 525/52e stay generic on purpose (see BAD_CREDENTIALS_MSG); the rest describe
#: an account state the user must act on, so saying so is worth more than the
#: marginal enumeration risk.
AD_SUBCODES: dict[str, tuple[str, str]] = {
    "525": ("user_not_found", BAD_CREDENTIALS_MSG),
    "52e": ("bad_password", BAD_CREDENTIALS_MSG),
    "530": ("time_restricted", "Sign-in is not allowed at this time. Please contact IT."),
    "531": ("workstation_restricted",
            "Sign-in is not allowed from this device. Please contact IT."),
    "532": ("password_expired",
            "Your corporate password has expired. Change it, then sign in again."),
    "533": ("account_disabled", "This account is disabled. Please contact IT."),
    "701": ("account_expired", "This account has expired. Please contact IT."),
    "773": ("must_change_password",
            "You must change your corporate password first (new account or password "
            "reset), then sign in again."),
    "775": ("account_locked",
            "This account is locked after repeated failed sign-ins. Wait and retry, "
            "or contact IT to unlock it."),
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


def normalize_login_name(raw: str) -> str:
    """Reduce what a person typed to the identifier we search for.

    Browsers and habit produce ``DOMAIN\\name`` (what Windows shows) and
    ``domain/name``; neither is a directory attribute value, so strip the
    prefix. The remainder is used as typed — AD matching is case-insensitive.
    """
    name = (raw or "").strip()
    for separator in ("\\", "/"):
        if separator in name:
            name = name.rsplit(separator, 1)[-1].strip()
    return name


def build_search_filter(username: str) -> str:
    """LDAP filter resolving a typed login name to one user entry.

    A signum matches ``sAMAccountName``. An address (anything containing "@")
    additionally matches the mail-ish attributes, because the most common way to
    fail this login was typing one's e-mail — which is a perfectly valid way to
    name the same account, and no reason to refuse it. The UPN and the mail
    address are frequently unrelated to the signum (e.g. signum ``ezhgmio`` vs
    UPN ``tracy.m.zhang@ericsson.com``), so this cannot be derived locally; it
    has to be a directory lookup.
    """
    settings = get_settings()
    escaped = escape_filter_chars(username)
    primary = settings.ldap_user_filter.format(username=escaped)
    if "@" not in username:
        return primary
    clauses = [primary] + [
        f"({attr}={escaped})" for attr in settings.ldap_email_login_attr_list
    ]
    return "(|" + "".join(clauses) + ")"


def _pick_entry(entries: list, username: str) -> object:
    """Choose deterministically when several entries match.

    Widening the filter to mail attributes makes multiple hits plausible (a
    shared alias, a stale object). An exact ``sAMAccountName`` match is the
    strongest claim to the name, so it wins; otherwise keep the first hit, which
    is the previous behaviour.
    """
    wanted = username.lower()
    for entry in entries:
        try:
            candidate = entry["sAMAccountName"].value
        except Exception:
            continue
        if candidate and str(candidate).lower() == wanted:
            return entry
    return entries[0]


def authenticate(username: str, password: str) -> LdapUser:
    """Verify ``username``/``password`` against AD and return the user's attrs.

    Raises ``LdapAuthError`` on bad credentials (retryable=False) or on an
    operational LDAP failure (retryable=True).
    """
    settings = get_settings()
    username = normalize_login_name(username)
    if not username or not password:
        raise LdapAuthError("Username and password are required.", reason="empty_input")
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
            f"The directory service is unavailable: {exc}", retryable=True, reason="service_bind_failed"
        ) from exc

    try:
        flt = build_search_filter(username)
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
            # NOT a wrong password: the login name matched no entry — neither as
            # a signum nor (for an address) as a UPN / mail attribute. Either a
            # typo or an account outside ldap_base_dn.
            logger.warning(
                "LDAP login failed: user %r not found (base=%s filter=%s referrals=%s)",
                username,
                settings.ldap_base_dn,
                flt,
                svc.result.get("referrals") if svc.result else None,
            )
            raise LdapAuthError(BAD_CREDENTIALS_MSG, reason="user_not_found")
        entry = _pick_entry(list(svc.entries), username)
        if len(svc.entries) > 1:
            # Plausible now that an address also matches mail attributes: a
            # shared alias, or a stale duplicate object.
            logger.warning(
                "LDAP login: %d entries matched user %r, using %s",
                len(svc.entries),
                username,
                entry.entry_dn,
            )
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
            raise LdapAuthError("Could not determine this account's EID.", reason="no_eid")
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
            "The directory service is temporarily unavailable. Please try again.", retryable=True, reason="rebind_error"
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
