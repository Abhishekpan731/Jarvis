#!/usr/bin/env bash
# Restore is destructive and is restricted to this repository's local Compose Postgres.
set -euo pipefail

if [[ $# -ne 2 || $1 != --confirm-local-dev ]]; then
    printf 'Usage: %s --confirm-local-dev /absolute/path/to/backup.dump\n' "$0" >&2
    exit 2
fi

backup_file=$2
[[ "$backup_file" == /* && "$backup_file" == *.dump && -f "$backup_file" && ! -L "$backup_file" ]] || {
    printf 'Backup must be an existing, non-symlink custom-format .dump file with an absolute path.\n' >&2
    exit 2
}
[[ -t 0 ]] || {
    printf 'Restore requires an interactive terminal and a typed confirmation.\n' >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/local-postgres-guard.sh"
assert_local_postgres || exit 1
local_compose exec -T postgres pg_restore --list - < "$backup_file" >/dev/null

printf 'This replaces objects in the repository local development database jarvis.\n'
printf 'Type RESTORE JARVIS LOCAL DEV to continue: '
IFS= read -r confirmation
[[ "$confirmation" == 'RESTORE JARVIS LOCAL DEV' ]] || {
    printf 'Confirmation did not match; no restore was run.\n' >&2
    exit 1
}

local_compose exec -T postgres pg_restore --single-transaction --clean --if-exists --no-owner --no-privileges --exit-on-error -U jarvis -d jarvis - < "$backup_file"
printf 'Local development PostgreSQL restore completed from: %s\n' "$backup_file"