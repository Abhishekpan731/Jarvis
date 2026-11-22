#!/usr/bin/env bash
# Create a custom-format dump from this repository's local Compose Postgres only.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: %s /absolute/path/to/backup.dump\n' "$0" >&2
    exit 2
fi

output_file=$1
output_dir=${output_file%/*}
[[ "$output_file" == /* && "$output_file" == *.dump && -d "$output_dir" ]] || {
    printf 'Output must be a new .dump file in an existing directory, using an absolute path.\n' >&2
    exit 2
}
[[ ! -e "$output_file" && ! -L "$output_file" ]] || {
    printf 'Refusing to overwrite an existing output path.\n' >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/local-postgres-guard.sh"
assert_local_postgres || exit 1

umask 077
temporary_file=$(mktemp "${output_file}.part.XXXXXX")
trap 'rm -f "$temporary_file"' EXIT
local_compose exec -T postgres pg_dump -U jarvis -d jarvis --format=custom > "$temporary_file"
[[ -s "$temporary_file" ]] || {
    printf 'Postgres produced an empty backup; refusing to publish it.\n' >&2
    exit 1
}
ln "$temporary_file" "$output_file"
rm "$temporary_file"
trap - EXIT
printf 'Local development PostgreSQL backup created: %s\n' "$output_file"