#!/usr/bin/env bash
# Deploy the /site/ folder to Cloudflare Pages.
#
# This script previously implemented the direct-upload REST flow by hand
# (hash -> upload-token -> upload -> manifest). That flow silently broke
# (upload-token returned method_not_allowed, so no file ever uploaded and
# every deployment 500'd). It is replaced by wrangler, which implements the
# current direct-upload protocol correctly. Kept as a thin wrapper so docs
# and any existing references keep working.
#
# Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment.
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?missing}"
: "${CLOUDFLARE_ACCOUNT_ID:?missing}"

cd "$(dirname "$0")/.."
CI=true npx --yes wrangler@latest pages deploy site --project-name=versewell --branch=main
