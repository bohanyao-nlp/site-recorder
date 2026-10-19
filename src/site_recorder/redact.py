"""Structural redaction applied by the host before anything reaches disk.

The extension already scrubs every value the participant TYPED into a
sensitive field (value matching, see extension/redact.js). This module covers
what value matching cannot: secrets the participant never typed —

* credential HEADERS: ``Authorization`` (bearer tokens, Basic user:password),
  ``Cookie`` / ``Set-Cookie`` values, API keys, CSRF tokens;
* request-body FIELDS whose name says secret (``password``, ``token``,
  ``cvv`` …) in JSON, form-urlencoded and multipart bodies — this also catches
  short secrets (a 4-digit PIN) that value matching skips;
* URL query parameters that carry credentials (``access_token``,
  ``client_secret``, ``password`` …). Deliberately narrow: URLs are the
  replay key, and signed asset URLs (``token=``, ``X-Amz-Signature``) must
  stay byte-identical for the pipeline to find those assets again.

Redacted values become ``REDACTED:<12 hex>``, an HMAC under a per-installation
key kept in the app dir (never exported). Equal values map to equal tokens, so
"the same session cookie on every request" stays visible to the pipeline,
while the token reveals nothing — and a guessable value (a weak password in a
Basic header) cannot be brute-forced from an exported session without the key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREFIX = "REDACTED:"
KEY_FILE = "redaction.key"

# Header names whose VALUES are credentials.
_SECRET_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token",
    "x-csrf-token", "x-xsrf-token", "x-csrftoken", "x-amz-security-token",
}
_SECRET_HEADER_RE = re.compile(r"(auth|token|secret|api[-_]?key|session|csrf|xsrf|password|signature)", re.I)
# Headers that match the pattern above but carry no secret.
_PUBLIC_HEADERS = {"x-goog-authuser", "access-control-allow-headers", "access-control-expose-headers",
                   "access-control-request-headers"}

# Field / parameter names (lowercased, non-alphanumerics removed). Exact
# matches for short names that would be too broad as substrings ("pin" is in
# "shipping", "ssn" in "classname"), substrings for the distinctive ones.
_EXACT_FIELDS = {"pass", "pwd", "pin", "otp", "ssn", "cvv", "cvc", "csc", "cvn", "cvv2", "cvc2",
                 "ccv", "cc", "code_verifier", "codeverifier", "sig", "signature"}
_FIELD_PARTS = ("password", "passwd", "passcode", "passphrase", "secret", "token", "apikey",
                "accesskey", "privatekey", "cardnumber", "ccnumber", "creditcard", "securitycode",
                "onetimecode", "otpcode", "routingnumber", "accountnumber", "iban", "socialsecurity",
                "authcode", "sessionid", "csrf", "xsrf")


# URL query parameters: only unambiguous credentials (see module docstring).
_URL_PARAMS = {"password", "passwd", "pwd", "pass", "accesstoken", "idtoken", "refreshtoken",
               "clientsecret", "apikey", "codeverifier"}


def _norm_field(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def is_secret_field(name: str) -> bool:
    n = _norm_field(name)
    if not n:
        return False
    return n in {_norm_field(x) for x in _EXACT_FIELDS} or any(p in n for p in _FIELD_PARTS)


def is_secret_header(name: str) -> bool:
    n = str(name).strip().lower()
    if n in _PUBLIC_HEADERS:
        return False
    return n in _SECRET_HEADERS or bool(_SECRET_HEADER_RE.search(n))


def load_or_create_key(app_dir: Path | None) -> bytes:
    """The per-installation HMAC key (created on first use). Without an app
    dir, a per-process key: tokens are then only consistent within it."""
    if app_dir is None:
        return secrets.token_bytes(32)
    p = Path(app_dir) / KEY_FILE
    try:
        data = p.read_bytes()
        if len(data) >= 32:
            return data[:32]
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_bytes(key)
        try:
            if p.exists():
                tmp.unlink()
            else:
                os.replace(tmp, p)
        except OSError:
            pass
        data = p.read_bytes()        # another host may have won the race
        return data[:32] if len(data) >= 32 else key
    except OSError:
        return key


class Redactor:
    def __init__(self, key: bytes):
        self._key = key

    def token(self, value) -> str:
        v = "" if value is None else str(value)
        if v.startswith(PREFIX):
            return v
        digest = hmac.new(self._key, v.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
        return PREFIX + digest[:12]

    # -- headers -----------------------------------------------------------------
    def _cookie_pairs(self, value: str) -> str:
        out = []
        for part in value.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out.append(f"{k.strip()}={self.token(v.strip())}")
            elif part.strip():
                out.append(part.strip())
        return "; ".join(out)

    def _set_cookie(self, value: str) -> str:
        # CDP joins several Set-Cookie headers with "\n"; only the first
        # name=value pair of each is the cookie, the rest are attributes.
        lines = []
        for line in str(value).split("\n"):
            head, sep, attrs = line.partition(";")
            if "=" in head:
                k, v = head.split("=", 1)
                head = f"{k.strip()}={self.token(v.strip())}"
            lines.append(head + sep + attrs)
        return "\n".join(lines)

    def header_value(self, name: str, value) -> str:
        n = str(name).strip().lower()
        v = "" if value is None else str(value)
        if n == "cookie":
            return self._cookie_pairs(v)
        if n == "set-cookie":
            return self._set_cookie(v)
        if n in ("authorization", "proxy-authorization"):
            scheme, sp, cred = v.partition(" ")
            return f"{scheme} {self.token(cred)}" if sp else self.token(v)
        return self.token(v)

    def headers(self, headers) -> dict:
        if not isinstance(headers, dict):
            return {}
        out = {}
        for k, v in headers.items():
            if is_secret_header(k):
                out[k] = self.header_value(k, v)
            elif str(k).lower() in ("location", "referer", "content-location"):
                out[k] = self.url(v)
            else:
                out[k] = v
        return out

    # -- URLs ----------------------------------------------------------------------
    def url(self, url):
        if not isinstance(url, str) or "?" not in url:
            return url
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        if not parts.query:
            return url
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        secret = [_norm_field(k) in _URL_PARAMS for k, _ in pairs]
        if not any(secret):
            return url
        q = urlencode([(k, self.token(v) if s else v) for (k, v), s in zip(pairs, secret)], safe=":/@")
        return urlunsplit(parts._replace(query=q))

    # -- request bodies --------------------------------------------------------------
    def _json_value(self, obj):
        changed = False
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if is_secret_field(k) and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    out[k] = self.token(v)
                    changed = True
                else:
                    nv, c = self._json_value(v)
                    out[k] = nv
                    changed = changed or c
            return out, changed
        if isinstance(obj, list):
            out, changed = [], False
            for v in obj:
                nv, c = self._json_value(v)
                out.append(nv)
                changed = changed or c
            return out, changed
        return obj, False

    _MULTIPART_PART = re.compile(
        r'(Content-Disposition:[^\r\n]*?\bname="([^"]*)"[^\r\n]*\r\n(?:[^\r\n]+\r\n)*\r\n)(.*?)(?=\r\n--)',
        re.S | re.I)

    def body(self, text: str, content_type: str = "") -> str:
        """Field-name redaction of a text request body. Unparseable bodies are
        returned unchanged (the extension's value matching still applied)."""
        if not isinstance(text, str) or not text:
            return text
        ct = (content_type or "").lower()
        stripped = text.lstrip()
        if "json" in ct or stripped[:1] in ("{", "["):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if obj is not None:
                new, changed = self._json_value(obj)
                return json.dumps(new, ensure_ascii=False, separators=(",", ":")) if changed else text
        if "multipart/form-data" in ct:
            def _sub(m):
                return m.group(1) + (self.token(m.group(3)) if is_secret_field(m.group(2)) else m.group(3))
            return self._MULTIPART_PART.sub(_sub, text)
        if "x-www-form-urlencoded" in ct or (not ct and re.fullmatch(r"[^=&\s]+=[^&\s]*(&[^=&\s]+=[^&\s]*)*", text)):
            pairs = parse_qsl(text, keep_blank_values=True)
            if pairs and any(is_secret_field(k) for k, _ in pairs):
                return urlencode([(k, self.token(v) if is_secret_field(k) else v) for k, v in pairs])
        return text
