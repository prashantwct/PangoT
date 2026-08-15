"""Refuse to let secrets or field data into the repository.

This exists because both got committed once: a `.env` with a live Postgres URL,
admin password, Flask secret key and Mapbox token, and `pangolin_data.db` with
pangolin coordinates. The repository is public, so they were world-readable
until they were noticed.

`.gitignore` alone does not prevent this — it has no effect on a file that is
already tracked, which is exactly how it happened. This runs in CI and as a
pre-commit hook, where it can actually stop the commit.

    python tools/check_secrets.py            # scan tracked files
    python tools/check_secrets.py --staged   # scan what is about to be committed
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

# Paths that must never be tracked, whatever they contain.
FORBIDDEN_PATHS = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),   # .env.local, .env.production…
    re.compile(r"\.db$"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"\.pem$"),
)

# Content patterns are deliberately few. The path rules above are the real
# defence — a tracked `.env` is exactly how this went wrong — and a content
# check that fires on ordinary code just teaches people to skip the output.
# Everything here is something that has no innocent explanation.
CONTENT_PATTERNS = (
    ("database URL with an inline password",
     re.compile(r"\b(?:postgres|postgresql|mysql|mongodb)(?:\+\w+)?://[^\s:/@]+:[^\s@'\"]+@[^\s/'\"]+")),
    ("Mapbox secret token",
     re.compile(r"\bsk\.eyJ[A-Za-z0-9_\-]{20,}")),
    ("AWS access key id",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("GitHub token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
)

# Files whose whole job is to show the shape of a credential without being one.
ALLOWLIST = {
    ".env.example",
    "tools/check_secrets.py",   # necessarily contains the patterns it looks for
}

# A connection string pointing at one of these is a documented example or a
# throwaway test fixture, not a live credential.
PLACEHOLDER = re.compile(
    r"@(?:localhost|127\.0\.0\.1|\[::1\]|host|db|postgres)\b"
    r"|@[\w.-]*example\.(?:com|org|net)\b"
    r"|//(?:user|USER|username|postgres|nobody|test):",
    re.IGNORECASE,
)

SELF = "tools/check_secrets.py"

# Text files only; a scan of binary assets is noise.
SCANNABLE_SUFFIXES = {
    ".py", ".js", ".json", ".yml", ".yaml", ".md", ".html", ".css",
    ".txt", ".cfg", ".ini", ".toml", ".sh", ".env", ".example", "",
}


def tracked_files(staged: bool):
    command = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
        if staged
        else ["git", "ls-files"]
    )
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return [line for line in output.splitlines() if line.strip()]


def scan(paths):
    problems = []

    for path in paths:
        if path in ALLOWLIST:
            continue

        for pattern in FORBIDDEN_PATHS:
            if pattern.search(path):
                problems.append((path, "this file must never be committed"))
                break

    for path in paths:
        if path in ALLOWLIST:
            continue
        suffix = Path(path).suffix
        if suffix and suffix not in SCANNABLE_SUFFIXES:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except (OSError, IsADirectoryError):
            continue

        for label, pattern in CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                if PLACEHOLDER.search(match.group(0)):
                    continue        # documented example or test fixture
                line = text[: match.start()].count("\n") + 1
                problems.append((f"{path}:{line}", label))
                break

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true",
                        help="scan staged changes instead of all tracked files")
    args = parser.parse_args()

    problems = scan(tracked_files(args.staged))

    if not problems:
        print("No secrets or field data found in tracked files.")
        return 0

    print("REFUSING: secrets or field data would be committed.\n", file=sys.stderr)
    for where, why in problems:
        print(f"  {where}\n      {why}", file=sys.stderr)
    print(
        "\nRemove them with `git rm --cached <file>` and keep them out via .gitignore.\n"
        "If a secret has already been pushed, rotate it — deleting the file does not\n"
        "un-publish it. See README 'If a secret is committed'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
