"""Diagnose per-user LDAP login failures ("some users get 401").

The login path collapses several distinct causes into one 401
("用户名或密码错误"): user not found, user found but bind refused, password
expired, account locked, smartcard-required, and even an LDAP timeout during
the user re-bind. This script separates them.

Usage (from the backend repo root, so .env is picked up):

    .venv/bin/python tools/ldap_diag.py ezhgmio someone-else ...

Search-only: needs just the service account, NOT the user's password. Reports
whether the account is findable and what AD says about its state.

Optional password check (only for an account whose password you legitimately
hold, e.g. your own) — prints AD's raw sub-code instead of a generic message:

    .venv/bin/python tools/ldap_diag.py --check-bind ezhgmio

The password is read from a TTY prompt, never echoed, never logged.
"""
from __future__ import annotations

import getpass
import ssl
import sys
from datetime import datetime, timedelta, timezone

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.config import get_settings

# userAccountControl flags that explain a refused simple bind.
UAC_FLAGS = {
    0x0002: "ACCOUNTDISABLE (账号已禁用)",
    0x0010: "LOCKOUT (账号被锁定)",
    0x0020: "PASSWD_NOTREQD",
    0x0040: "PASSWD_CANT_CHANGE",
    0x0200: "NORMAL_ACCOUNT",
    0x10000: "DONT_EXPIRE_PASSWORD",
    0x40000: "SMARTCARD_REQUIRED (禁止用密码做简单绑定 -> 必然 401)",
    0x100000: "NOT_DELEGATED",
    0x800000: "PASSWORD_EXPIRED (密码已过期)",
}

# AD packs the real reason into the diagnostic message of an invalidCredentials
# result, as "data <hex>".
AD_SUBCODES = {
    "525": "用户不存在",
    "52e": "密码错误 (凭据本身不对)",
    "530": "不允许在此时间登录",
    "531": "不允许在此工作站登录",
    "532": "密码已过期 -> 用户需先改密码",
    "533": "账号已禁用",
    "701": "账号已过期",
    "773": "必须在下次登录时修改密码 -> 用户需先改密码",
    "775": "账号被锁定 (连续输错触发)",
}

AD_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _filetime(value) -> str:
    """Render an AD FILETIME (100ns ticks since 1601) as a readable date.

    ldap3 already converts some of these attributes to ``datetime``; a value at
    (or near) the 1601 epoch means the attribute is 0, i.e. unset.
    """
    if isinstance(value, datetime):
        if value.year <= 1601:
            return "never/unset"
        return value.strftime("%Y-%m-%d %H:%M:%SZ")
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return str(value)
    if ticks in (0, 0x7FFFFFFFFFFFFFFF):
        return "never/unset"
    try:
        return (AD_EPOCH + timedelta(microseconds=ticks // 10)).strftime("%Y-%m-%d %H:%M:%SZ")
    except OverflowError:
        return "never/unset"


def _server(settings) -> Server:
    use_ssl = settings.ldap_url.lower().startswith("ldaps://")
    validate = ssl.CERT_REQUIRED if settings.ldap_verify_tls else ssl.CERT_NONE
    tls = Tls(validate=validate) if use_ssl else None
    return Server(settings.ldap_url, use_ssl=use_ssl, tls=tls, get_info=ALL)


def _decode_uac(raw) -> str:
    try:
        uac = int(raw)
    except (TypeError, ValueError):
        return f"?({raw})"
    names = [text for bit, text in UAC_FLAGS.items() if uac & bit]
    return f"0x{uac:X} " + (", ".join(names) if names else "(no known flags)")


def diagnose(username: str, check_bind: bool = False) -> None:
    settings = get_settings()
    server = _server(settings)

    print(f"\n{'=' * 72}\n用户: {username}\n{'=' * 72}")

    # 1) Service bind. If THIS fails, every user gets 503 (not 401).
    try:
        svc = Connection(
            server,
            user=settings.ldap_bind_user,
            password=settings.ldap_bind_password,
            auto_bind=True,
            receive_timeout=settings.ldap_timeout,
        )
    except LDAPException as exc:
        print(f"  [服务账号绑定失败] {exc}")
        print("  -> 这会让所有人拿到 503，不是 401。检查 LDAP_BIND_PASSWORD / 网络。")
        return
    print(f"  服务账号绑定 OK ({settings.ldap_bind_user}), 服务器={server.host}")

    # 2) The exact search the login path performs.
    flt = settings.ldap_user_filter.format(username=escape_filter_chars(username))
    attrs = [
        settings.ldap_eid_attr,
        settings.ldap_display_attr,
        settings.ldap_department_attr,
        "sAMAccountName",
        "userPrincipalName",
        "userAccountControl",
        "lockoutTime",
        "pwdLastSet",
        "accountExpires",
        "msDS-UserPasswordExpiryTimeComputed",
    ]
    ok = svc.search(settings.ldap_base_dn, flt, search_scope=SUBTREE, attributes=attrs)
    print(f"  搜索: base={settings.ldap_base_dn} filter={flt}")
    print(f"  结果: ok={ok} 命中={len(svc.entries)} refs={svc.result.get('referrals')}")

    if not ok or not svc.entries:
        print("  -> 搜到 0 条：登录会返回 401「用户名或密码错误」，但真实原因是**查不到这个账号**。")
        print("     常见：用户输的不是 sAMAccountName(signum)，而是邮箱/ERICSSON\\xxx；")
        print("     或账号在其它域，不在 DC=ericsson,DC=se 这个搜索基下。")
        svc.unbind()
        return

    if len(svc.entries) > 1:
        print("  -> 命中多条！登录代码只取 entries[0]，可能绑到一个陈旧/禁用对象上。")

    for entry in svc.entries:
        print(f"\n  DN: {entry.entry_dn}")
        for name in attrs:
            try:
                val = entry[name].value
            except Exception:
                continue
            if val in (None, [], ""):
                continue
            if name == "userAccountControl":
                print(f"    userAccountControl = {_decode_uac(val)}")
            elif name in ("lockoutTime", "pwdLastSet", "accountExpires",
                          "msDS-UserPasswordExpiryTimeComputed"):
                print(f"    {name} = {_filetime(val)}")
            else:
                print(f"    {name} = {val}")
        eid = None
        try:
            eid = entry[settings.ldap_eid_attr].value
        except Exception:
            pass
        if not eid:
            print(f"    -> 缺少 {settings.ldap_eid_attr}，EID 会回退到 sAMAccountName")

    user_dn = svc.entries[0].entry_dn
    svc.unbind()

    if not check_bind:
        print("\n  (未做密码绑定验证。加 --check-bind 可验证并看到 AD 的真实子码。)")
        return

    # 3) Optional: the user re-bind, with AD's raw diagnostic preserved.
    password = getpass.getpass(f"  输入 {username} 的密码 (不回显, 不记录): ")
    try:
        conn = Connection(server, user=user_dn, password=password,
                          receive_timeout=settings.ldap_timeout)
        bound = conn.bind()
    except LDAPException as exc:
        print(f"  [绑定抛异常] {type(exc).__name__}: {exc}")
        print("  -> 注意：当前 ldap_auth.py 把这个异常也变成 401「用户名或密码错误」，")
        print("     所以超时/TLS 故障会被误报成密码错误。")
        return

    if bound:
        print("  绑定成功 -> 这个账号用当前代码可以正常登录。")
        conn.unbind()
        return

    result = conn.result or {}
    message = str(result.get("message", ""))
    print(f"  绑定失败: {result.get('description')} / {message.strip()[:200]}")
    for code, text in AD_SUBCODES.items():
        if f"data {code}" in message:
            print(f"  -> AD 子码 {code}: {text}")
            break
    else:
        print("  -> 未识别到 data 子码，见上面原始信息。")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--check-bind"]
    check_bind = "--check-bind" in argv[1:]
    if not args:
        print(__doc__)
        return 2
    for username in args:
        diagnose(username, check_bind=check_bind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
