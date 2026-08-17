#!/bin/sh
set -eu

# 在 Gunicorn fork worker 前只初始化一次，避免多进程竞争建表和种子数据。
export APP_ENV=production
export AUTO_INIT_DB=0
export SEED_DEMO_DATA=0
python -c 'from app import init_db; init_db()'

exec "$@"
