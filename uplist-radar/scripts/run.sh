#!/usr/bin/env bash
# Dev/local run: init DB, ensure admin, start the app with the in-process scheduler.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
$PY - <<'EOF'
from uplist_radar.db import DB
DB().init()
print("db ready")
EOF
if [ ! -f data/.admin_created ]; then
  echo "No admin user yet. Create one:"
  read -p "username: " U
  read -s -p "password: " P; echo
  $PY scripts/create_admin.py "$U" "$P" && touch data/.admin_created
fi
export UPLIST_SEC_CONTACT="${UPLIST_SEC_CONTACT:-}"
exec $PY -m uplist_radar.app
