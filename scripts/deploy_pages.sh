#!/usr/bin/env bash
# Deploy the /site/ folder to Cloudflare Pages via direct-upload REST calls
# (no wrangler CLI required). Needs CLOUDFLARE_API_TOKEN and
# CLOUDFLARE_ACCOUNT_ID in the environment.
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?missing}"
: "${CLOUDFLARE_ACCOUNT_ID:?missing}"

SITE_DIR="$(cd "$(dirname "$0")/../site" && pwd)"
PROJECT="versewell"
API="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/pages/projects/${PROJECT}"

# 1. Hash every file and request the upload-token for missing ones.
declare -A HASH_PATH
HASH_LIST="["
first=1
while IFS= read -r -d '' f; do
  rel="${f#"$SITE_DIR"/}"
  h=$(sha1sum "$f" | awk '{print $1}')
  HASH_PATH["$h"]="$rel"
  if [ $first -eq 1 ]; then first=0; else HASH_LIST+=","; fi
  HASH_LIST+="\"$h\""
done < <(find "$SITE_DIR" -type f -print0)
HASH_LIST+="]"

echo "checking ${#HASH_PATH[@]} files against Pages…"
MISSING=$(curl -s -X POST "$API/upload-token" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"hashes\": $HASH_LIST}" | python3 -c "import json,sys;print(' '.join(json.load(sys.stdin).get('result',[])))")

# 2. Upload missing files (base64 payloads).
for h in $MISSING; do
  rel="${HASH_PATH[$h]}"
  echo "  uploading $rel"
  b64=$(base64 -w0 "$SITE_DIR/$rel")
  printf -- '------vw\r\nContent-Disposition: form-data; name="%s"\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n\r\n%s\r\n' "$h" "$b64" > /tmp/vw_part
  printf -- '------vw--\r\n' >> /tmp/vw_part
  curl -s -X POST "$API/upload" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H "Content-Type: multipart/form-data; boundary=----vw" \
    --data-binary @/tmp/vw_part > /dev/null
done

# 3. Build the manifest (hash -> /path) and create the deployment.
MANIFEST=$(python3 - "$SITE_DIR" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
out = {}
for dirpath, _, files in os.walk(root):
    for fn in files:
        p = os.path.join(dirpath, fn)
        rel = "/" + os.path.relpath(p, root).replace(os.sep, "/")
        out[rel] = hashlib.sha1(open(p, "rb").read()).hexdigest()
print(json.dumps(out))
PY
)

python3 - "$MANIFEST" > /tmp/vw_deploy_body <<'PY'
import json, sys
manifest = json.loads(sys.argv[1])
parts = []
boundary = "----vwdeploy"
def field(name, value, ctype=None, filename=None):
    h = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
    if filename:
        h += f"; filename=\"{filename}\""
    h += "\r\n"
    if ctype:
        h += f"Content-Type: {ctype}\r\n"
    parts.append(h + "\r\n" + value + "\r\n")
field("manifest", json.dumps(manifest), "application/json")
field("branch", "main")
open("/tmp/vw_deploy_body", "w").write("".join(parts) + f"--{boundary}--\r\n")
PY

echo "creating deployment…"
curl -s -X POST "$API/deployments" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: multipart/form-data; boundary=----vwdeploy" \
  --data-binary @/tmp/vw_deploy_body | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('result') or {};print('success:',d.get('success'),'| url:',r.get('url'),'| errors:',d.get('errors'))"
