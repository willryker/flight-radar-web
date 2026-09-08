"""
The one HTTP helper.

There is exactly one of these because of a bug worth remembering: SondeHub
answers with `Content-Encoding: gzip` whether or not you asked for it, and
urllib does not decompress on your behalf -- it hands back the raw deflate
stream and json.loads reports "Expecting value: line 1 column 1". The request
looked fine from every command-line tool, because curl and PowerShell both
decompress transparently.

So: always decompress when the header says to, and do it in one place rather
than in each caller that happens to remember.
"""

import gzip
import json
import urllib.request
import zlib

USER_AGENT = "esp32-flight-radar-web/1.0"


def fetch(url, timeout=15, headers=None, accept="application/json"):
    """Raw bytes from url, decompressed if the server compressed them."""
    head = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if accept:
        head["Accept"] = accept
    if headers:
        head.update(headers)

    req = urllib.request.Request(url, headers=head)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise IOError("HTTP %s" % resp.status)
        body = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()

    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            body = zlib.decompress(body, -zlib.MAX_WBITS)   # raw, no header
    return body


def fetch_json(url, timeout=15, headers=None):
    return json.loads(fetch(url, timeout, headers).decode("utf-8", "replace"))
