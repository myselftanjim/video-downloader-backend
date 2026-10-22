#!/bin/sh
set -eu

if [ -z "${CLIPNEST_COBALT_API_KEY:-}" ]; then
  echo "CLIPNEST_COBALT_API_KEY must be configured." >&2
  exit 1
fi

# Generate the private API-key file at startup so no secret is committed to Git.
node -e '
const fs = require("node:fs");
const key = process.env.CLIPNEST_COBALT_API_KEY || "";
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(key)) {
  console.error("CLIPNEST_COBALT_API_KEY must be a UUIDv4.");
  process.exit(1);
}
const keys = { [key]: { name: "ClipNest private backend", limit: 20, allowedServices: "all" } };
fs.writeFileSync("/tmp/clipnest-cobalt-keys.json", JSON.stringify(keys), { mode: 0o600 });
'

export API_KEY_URL="file:///tmp/clipnest-cobalt-keys.json"
export API_AUTH_REQUIRED="1"
export API_LISTEN_ADDRESS="0.0.0.0"
export API_PORT="${PORT:-10000}"
export CORS_WILDCARD="0"
export CORS_URL="${CLIPNEST_FRONTEND_URL:-https://clipnest-personal-video-tool.onrender.com}"

exec node src/cobalt
