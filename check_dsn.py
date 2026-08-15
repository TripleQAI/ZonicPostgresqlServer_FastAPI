"""Check PGF_TSDB_DSN in .env without printing the secret.

    python check_dsn.py

Answers "why won't it connect?" before you start a server and read a traceback.
Never prints the password — only whether one is present and whether it needs
percent-encoding.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

RESERVED = "@:/?#[]%"


def main() -> int:
    env = Path(__file__).parent / ".env"
    if not env.exists():
        print("  .env not found — copy .env.example to .env")
        return 1

    line = next(
        (
            ln
            for ln in env.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("PGF_TSDB_DSN=") and not ln.strip().startswith("#")
        ),
        None,
    )
    if line is None:
        print("  No active PGF_TSDB_DSN line in .env (every one is commented out)")
        return 1

    value = line.split("=", 1)[1].strip()

    if "#" in value:
        print("  PROBLEM: the value contains '#'. dotenv treats it as a comment")
        print("           start and truncates there. Percent-encode it as %23.")
        return 1

    if "PUT_PASSWORD_HERE" in value:
        print("  PROBLEM: the placeholder is still in .env.")
        print("           Replace PUT_PASSWORD_HERE with the real password.")
        return 1

    # libpq key=value form — no URI parsing involved.
    if not value.startswith(("postgres://", "postgresql://")):
        has_pw = "password=" in value
        print(f"  key=value DSN; password present: {has_pw}")
        return 0 if has_pw else 1

    parsed = urllib.parse.urlsplit(value)
    print(f"  host     {parsed.hostname}")
    print(f"  port     {parsed.port}")
    print(f"  database {parsed.path.lstrip('/')}")
    print(f"  user     {parsed.username}")

    if parsed.password is None:
        print("\n  PROBLEM: no password in the DSN.")
        print("           The console Service URL omits it: there is nothing")
        print("           between the username and '@'. libpq will fail with")
        print("           'fe_sendauth: no password supplied'.")
        print("           Expected shape: postgresql://user:PASSWORD@host:port/db")
        return 1

    print(f"  password present ({len(parsed.password)} chars)")

    unsafe = sorted({c for c in parsed.password if c in RESERVED})
    if unsafe:
        print(f"\n  PROBLEM: password contains URI-reserved characters: {unsafe}")
        print("           Percent-encode them, or use the key=value form:")
        for c in unsafe:
            print(f"             {c!r} -> {urllib.parse.quote(c, safe='')}")
        return 1

    query = urllib.parse.parse_qs(parsed.query)
    sslmode = query.get("sslmode", ["(unset)"])[0]
    print(f"  sslmode  {sslmode}")
    if sslmode in {"require", "prefer", "allow", "disable"}:
        print("           note: this encrypts but does NOT authenticate the server.")
        print("           verify-full is verified to work against this endpoint.")

    print("\n  DSN looks well-formed. Start the service to test credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
