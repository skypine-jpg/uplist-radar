#!/usr/bin/env python3
"""Create the first admin user. Usage: scripts/create_admin.py USERNAME PASSWORD"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uplist_radar.db import DB
from uplist_radar.auth import create_user

if len(sys.argv) != 3:
    print("usage: create_admin.py USERNAME PASSWORD", file=sys.stderr)
    sys.exit(1)
db = DB()
db.init()
uid = create_user(db, sys.argv[1], sys.argv[2], is_admin=True)
print(f"admin user created: id={uid} username={sys.argv[1]}")
