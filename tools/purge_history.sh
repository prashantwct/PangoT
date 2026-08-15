#!/usr/bin/env bash
#
# Remove `.env` and `pangolin_data.db` from every commit in this repository's
# history, then force-push the rewritten history.
#
# Both files were committed while the repository was public. Deleting them in a
# later commit does not un-publish them: anyone can still fetch them from the
# commits that added them. This rewrites those commits so the blobs are no
# longer reachable from any branch or tag.
#
#   ./tools/purge_history.sh --dry-run   # rewrite locally, show the result, push nothing
#   ./tools/purge_history.sh             # rewrite and force-push
#
# READ THIS FIRST
#
#   1. Rotate the credentials before running this. A rewrite does not revoke
#      anything — anyone who already copied the file still holds working
#      credentials, and GitHub serves cached blobs for a while afterwards.
#      This script refuses to run until you confirm rotation is done.
#
#   2. Every commit gets a new SHA. Existing clones cannot pull afterwards and
#      must be re-cloned; pushing from a stale clone puts the old history back.
#
#   3. Close or merge open pull requests first. They pin the old commits, which
#      keeps the blobs reachable no matter what this does.
#
#   4. The force-push changes the commit main points at, so any host with
#      auto-deploy will redeploy. The tree content is identical, but the service
#      restarts.
#
# Requires git-filter-repo:  pip install git-filter-repo
#
set -euo pipefail

REPO_URL="${PANGOT_REPO_URL:-https://github.com/prashantwct/PangoT.git}"
PATHS=(.env pangolin_data.db)

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

git filter-repo --help >/dev/null 2>&1 \
  || die "git-filter-repo is not installed. Run: pip install git-filter-repo"

if [[ "${PANGOT_CREDENTIALS_ROTATED:-}" != "yes" ]]; then
  cat >&2 <<'EOF'
REFUSING: rotate the credentials first.

Everything in the committed .env is still valid until you change it, and this
rewrite does not change it. Rotate, in this order:

  1. DATABASE_URL     — the Postgres password. It reads and writes pangolin
                        locations, so it goes first.
  2. ADMIN_PASSWORD   — it was committed in plaintext. Replace it with a
                        ADMIN_PASSWORD_HASH (see README).
  3. SECRET_KEY       — anyone holding it can forge a coordinator session
                        cookie. Changing it signs everyone out, which is the
                        point.
  4. MAPBOX_TOKEN     — it is billable.

Update them in the host's environment, confirm the app still starts, then:

  PANGOT_CREDENTIALS_ROTATED=yes ./tools/purge_history.sh
EOF
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- clone -------------------------------------------------------------------
#
# A fresh mirror clone, never the working repository. filter-repo rewrites
# aggressively and strips the remote afterwards; doing that to someone's
# checkout with uncommitted work in it is not recoverable.

say "Cloning $REPO_URL"
git clone --mirror "$REPO_URL" "$WORKDIR/repo.git" --quiet
cd "$WORKDIR/repo.git"

# --- what is actually there --------------------------------------------------

say "Commits carrying each file"
found=0
for path in "${PATHS[@]}"; do
  count="$(git log --all --oneline -- "$path" | wc -l | tr -d ' ')"
  printf '  %-20s %s commit(s)\n' "$path" "$count"
  [[ "$count" != "0" ]] && found=1
done

if [[ "$found" == "0" ]]; then
  say "Nothing to purge — neither file appears anywhere in history."
  exit 0
fi

# --- confirm -----------------------------------------------------------------

if [[ "$DRY_RUN" == "0" ]]; then
  warn ""
  warn "This rewrites every commit and force-pushes over $REPO_URL."
  warn "Existing clones will have to be re-cloned."
  read -r -p 'Type PURGE to continue: ' reply
  [[ "$reply" == "PURGE" ]] || die "Not confirmed; nothing was changed."
fi

# --- rewrite -----------------------------------------------------------------

say "Rewriting history"
args=()
for path in "${PATHS[@]}"; do args+=(--path "$path"); done
git filter-repo --invert-paths "${args[@]}" --force

# --- verify locally ----------------------------------------------------------

say "Verifying"
for path in "${PATHS[@]}"; do
  if [[ -n "$(git log --all --oneline -- "$path")" ]]; then
    die "$path is still reachable after the rewrite. Nothing has been pushed."
  fi
  printf '  %-20s gone from all refs\n' "$path"
done

if [[ "$DRY_RUN" == "1" ]]; then
  say "Dry run — nothing pushed."
  echo "The rewritten history was in $WORKDIR/repo.git and is now being deleted."
  exit 0
fi

# --- push --------------------------------------------------------------------
#
# filter-repo removes the remote on purpose, to make an accidental push
# impossible. Re-adding it here is the deliberate step.

say "Force-pushing"
git remote add origin "$REPO_URL"
git push --force --mirror origin

# --- verify remotely ---------------------------------------------------------
#
# The blob URLs that were fetchable before must now 404. GitHub can serve a
# cached copy for a while, so a 200 here is not necessarily a failed rewrite —
# but it is the reason step 4 below exists.

say "Checking the old blob URLs"
raw="https://raw.githubusercontent.com/prashantwct/PangoT"
for spec in "b852b33/.env" "8ccdafc/.env" "07f1e88/.env" "b852b33/pangolin_data.db"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$raw/$spec" || echo '???')"
  if [[ "$code" == "404" ]]; then
    printf '  %-32s %s\n' "$spec" "404 — gone"
  else
    printf '  \033[33m%-32s %s — still served (cached)\033[0m\n' "$spec" "$code"
  fi
done

cat <<'EOF'

Done. Remaining steps, in order:

  1. Re-clone. Every existing clone now has the old history and will push it
     back if anyone runs `git push` from one. Delete them:

         rm -rf PangoT && git clone https://github.com/prashantwct/PangoT.git

  2. Confirm the deploy came back up. The force-push moved main, so any
     auto-deploy will have rebuilt. /healthz should report ok.

  3. Consider making the repository private. Field-site coordinates are worth
     protecting on their own, separately from any credential.

  4. Ask GitHub Support (https://support.github.com/contact) to drop cached
     views of the old commits. A force-push alone does not remove them, and
     forks keep their own copies — name any forks in the request.
EOF
