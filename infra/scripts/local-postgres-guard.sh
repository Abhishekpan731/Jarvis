#!/usr/bin/env bash
# Fail closed: these helpers support only the repository's local Compose Postgres.
set -euo pipefail

GUARD_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly GUARD_SCRIPT_DIR
REPO_ROOT="$(cd "$GUARD_SCRIPT_DIR/../.." && pwd)"
readonly REPO_ROOT
COMPOSE_DIR="$REPO_ROOT/infra/compose"
readonly COMPOSE_DIR
LOCAL_COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
readonly LOCAL_COMPOSE_FILE

# Ignore ambient Compose overrides so scripts cannot be redirected to another stack.
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR

local_compose() {
    docker compose --project-directory "$REPO_ROOT" -f "$LOCAL_COMPOSE_FILE" "$@"
}

refuse() {
    printf 'Refusing local database operation: %s\n' "$1" >&2
    return 1
}

assert_local_postgres() {
    local context endpoint container_id image service config_files database_config user_config mounts
    [[ -z "${DOCKER_HOST:-}" ]] || refuse 'DOCKER_HOST is set' || return 1
    context=$(docker context show 2>/dev/null) || refuse 'cannot identify Docker context' || return 1
    endpoint=$(docker context inspect "$context" --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null) || refuse 'cannot inspect Docker endpoint' || return 1
    [[ "$endpoint" == unix://* ]] || refuse 'Docker endpoint is not a local Unix socket' || return 1
    container_id=$(local_compose ps --status running -q postgres 2>/dev/null) || refuse 'Compose Postgres service is unavailable' || return 1
    [[ -n "$container_id" ]] || refuse 'local Compose Postgres is not running' || return 1
    image=$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null) || refuse 'cannot verify Postgres image' || return 1
    service=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$container_id" 2>/dev/null) || refuse 'cannot verify Compose service' || return 1
    config_files=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$container_id" 2>/dev/null) || refuse 'cannot verify Compose file' || return 1
    database_config=$(docker inspect --format '{{range .Config.Env}}{{if eq . "POSTGRES_DB=jarvis"}}yes{{end}}{{end}}' "$container_id" 2>/dev/null) || refuse 'cannot verify local database name' || return 1
    user_config=$(docker inspect --format '{{range .Config.Env}}{{if eq . "POSTGRES_USER=jarvis"}}yes{{end}}{{end}}' "$container_id" 2>/dev/null) || refuse 'cannot verify local database user' || return 1
    mounts=$(docker inspect --format '{{range .Mounts}}{{println .Name}}{{end}}' "$container_id" 2>/dev/null) || refuse 'cannot verify local data volume' || return 1
    [[ "$image" == 'pgvector/pgvector:pg16' && "$service" == 'postgres' ]] || refuse 'container does not match the repository local Postgres service' || return 1
    [[ "$config_files" == *"$LOCAL_COMPOSE_FILE"* ]] || refuse 'container was not created from the repository local Compose file' || return 1
    [[ "$database_config" == yes && "$user_config" == yes && "$mounts" == *jarvis-postgres* ]] || refuse 'container database or persistent volume does not match local development settings' || return 1
    local_compose exec -T postgres pg_isready -U jarvis -d jarvis >/dev/null 2>&1 || refuse 'local development database is not ready' || return 1
}