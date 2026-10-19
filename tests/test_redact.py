"""Host-side structural redaction (headers, body fields, URL credentials)."""
import json

from site_recorder.redact import PREFIX, Redactor, is_secret_field, load_or_create_key

R = Redactor(b"k" * 32)


def test_field_names():
    for name in ("password", "user_password", "newPassword", "pass", "pwd", "PIN", "otp", "cvv",
                 "card_number", "cardNumber", "access_token", "client_secret", "apiKey", "csrfToken"):
        assert is_secret_field(name), name
    for name in ("username", "email", "q", "shipping", "classname", "spinner", "passenger_count",
                 "pinned", "compass", "description"):
        assert not is_secret_field(name), name


def test_tokens_are_stable_keyed_and_idempotent():
    t = R.token("abc")
    assert t.startswith(PREFIX) and t == R.token("abc") and t != R.token("abd")
    assert Redactor(b"x" * 32).token("abc") != t       # depends on the installation key
    assert R.token(t) == t


def test_headers():
    h = R.headers({"Authorization": "Bearer eyJsecret", "Cookie": "sid=s3cr3t; theme=dark",
                   "Set-Cookie": "sid=new; Path=/; HttpOnly\nlang=en; Path=/",
                   "X-CSRF-Token": "tok", "Accept": "text/html", "X-Goog-AuthUser": "0",
                   "Location": "https://a.example/cb?access_token=zzz&x=1"})
    assert h["Authorization"].startswith("Bearer " + PREFIX) and "eyJ" not in h["Authorization"]
    assert h["Cookie"].startswith("sid=" + PREFIX) and "theme=" + PREFIX in h["Cookie"]
    assert "s3cr3t" not in h["Cookie"]
    lines = h["Set-Cookie"].split("\n")
    assert lines[0].startswith("sid=" + PREFIX) and lines[0].endswith("; Path=/; HttpOnly")
    assert lines[1].startswith("lang=" + PREFIX)
    assert h["X-CSRF-Token"].startswith(PREFIX)
    assert h["Accept"] == "text/html" and h["X-Goog-AuthUser"] == "0"
    assert "zzz" not in h["Location"] and "x=1" in h["Location"]


def test_url_redaction_is_narrow():
    assert R.url("https://a.example/s?q=shoes") == "https://a.example/s?q=shoes"
    u = R.url("https://a.example/cb?code=1&access_token=SECRET&state=s")
    assert "SECRET" not in u and "code=1" in u and "state=s" in u
    # signed asset URLs are replay keys: left byte-identical
    signed = "https://cdn.example/i.png?token=abc&X-Amz-Signature=def"
    assert R.url(signed) == signed


def test_json_body():
    body = json.dumps({"user": "ann", "password": "hunter2",
                       "variables": {"input": {"cardNumber": 4242424242424242, "pin": "12"}}})
    out = json.loads(R.body(body, "application/json"))
    assert out["user"] == "ann"
    assert out["password"].startswith(PREFIX)
    assert out["variables"]["input"]["cardNumber"].startswith(PREFIX)
    assert out["variables"]["input"]["pin"].startswith(PREFIX)
    untouched = '{"q": "shoes"}'
    assert R.body(untouched, "application/json") == untouched


def test_form_body():
    out = R.body("username=ann&password=hunter2&remember=1", "application/x-www-form-urlencoded")
    assert "hunter2" not in out and "username=ann" in out and "remember=1" in out
    # no content type, but clearly form-encoded
    assert "hunter2" not in R.body("pwd=hunter2&x=1", "")


def test_multipart_body():
    b = ("--XX\r\nContent-Disposition: form-data; name=\"user\"\r\n\r\nann\r\n"
         "--XX\r\nContent-Disposition: form-data; name=\"password\"\r\n\r\nhunter2\r\n--XX--\r\n")
    out = R.body(b, "multipart/form-data; boundary=XX")
    assert "hunter2" not in out and "\r\n\r\nann\r\n" in out


def test_key_file_persists(tmp_path):
    k1 = load_or_create_key(tmp_path)
    assert (tmp_path / "redaction.key").exists() and load_or_create_key(tmp_path) == k1
    assert len(load_or_create_key(None)) == 32
