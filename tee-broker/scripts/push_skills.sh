# tee-broker-deploy/scripts/push_skills.sh
#!/usr/bin/env bash
# Walks a directory of skill folders and pushes each one to the skill library.
# Usage:
#   push_skills.sh --source-dir PATH [--library-url URL] [--api-key KEY] [--sync] [--build]
#
# Requires: curl, jq, sha256sum, optional: cargo + rustup target wasm32-wasip1 (for --build)

set -euo pipefail

SOURCE=""
LIB_URL="http://127.0.0.1:8091"
API_KEY="${SKILL_LIBRARY_API_KEY:-}"
SYNC=0
BUILD=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-dir) SOURCE="$2"; shift 2;;
        --library-url) LIB_URL="$2"; shift 2;;
        --api-key) API_KEY="${2}"; shift 2;;
        --sync) SYNC=1; shift;;
        --build) BUILD=1; shift;;
        -h|--help)
            grep '^# ' "$0" | sed 's/^# //'
            exit 0;;
        *) echo "unknown arg: $1"; exit 1;;
    esac
done

if [[ -z "$SOURCE" ]]; then
    echo "ERROR: --source-dir is required"
    exit 1
fi
if [[ -z "$API_KEY" ]]; then
    echo "ERROR: --api-key) or env SKILL_LIBRARY_API_KEY is required"
    exit 1
fi

if [[ ! -d "$SOURCE" ]]; then
    echo "ERROR: source dir $SOURCE does not exist"
    exit 1
fi

# Strip trailing slash.
LIB_URL="${LIB_URL%/}"

# Frontmatter parse: extract first --- block from SKILL.md.
parse_frontmatter() {
    local file="$1"
    local in_fm=0 name="" description="" license="Apache-2.0" version="0.1.0"
    while IFS= read -r line; do
        if [[ "$line" == "---" && $in_fm -eq 0 ]]; then in_fm=1; continue; fi
        if [[ "$line" == "---" && $in_fm -eq 1 ]]; then break; fi
        if [[ $in_fm -eq 1 ]]; then
            case "$line" in
                name:*)        name="${line#name: }";;
                description:*) description="${line#description: }";;
                version:*)     version="${line#version: }";;
                license:*)     license="${line#license: }";;
            esac
        fi
    done < "$file"
    echo "$name|$version|$description|$license"
}

pushed=0
failed=0

for skill_dir in "$SOURCE"/*/; do
    [[ -d "$skill_dir" ]] || continue
    name="$(basename "$skill_dir")"
    skill_md_path="$skill_dir/SKILL.md"
    if [[ ! -f "$skill_md_path" ]]; then
        echo "SKIP $name (no SKILL.md)"
        continue
    fi

    IFS='|' read -r fm_name fm_version fm_description fm_license < \
        <(parse_frontmatter "$skill_md_path")
    fm_name="${fm_name:-$name}"
    fm_license="${fm_license:-Apache-2.0}"
    fm_version="${fm_version:-0.1.0}"
    fm_description="${fm_description:-No description provided}"

    # Optional Rust build.
    if [[ -f "$skill_dir/Cargo.toml" && "$BUILD" -eq 1 ]]; then
        echo "BUILD  $name (cargo wasm32-wasip1 release)"
        (cd "$skill_dir" && cargo build --target wasm32-wasip1 --release)
    fi

    # Register card.
    register_body=$(jq -n \
        --arg name "$fm_name" \
        --arg version "$fm_version" \
        --arg desc "$fm_description" \
        --arg lic "$fm_license" \
        --arg summary "$(echo "$fm_description" | head -c 200)" \
        '{name:$name,version:$version,description:$desc,license:$lic,summary:$summary}')

    echo "POST $fm_name@$fm_version"
    reg_resp=$(curl -sS -X POST \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$register_body" \
        "$LIB_URL/v1/library/skills" || true)
    if ! echo "$reg_resp" | jq -e '.sha256_card' >/dev/null 2>&1; then
        # Likely 409 already-registered — continue, we still upload files (idempotent upsert).
        echo "  register: $reg_resp"
    fi
    ref="${fm_name}@${fm_version}"

    # Upload every file in the skill_dir (excluding target/, .git/, Cargo.lock).
    file_count=0
    total_bytes=0
    while IFS= read -r file_path; do
        rel="${file_path#${skill_dir%/}/}"
        # Skip build artefacts.
        case "$rel" in
            target/*|.git/*|Cargo.lock|node_modules/*) continue;;
        esac
        sha=$(sha256sum "$file_path" | awk '{print $1}')
        size=$(stat -c%s "$file_path")
        ctype="application/octet-stream"
        case "$rel" in
            SKILL.md|README.md|*.md) ctype="text/markdown";;
            *.wasm) ctype="application/wasm";;
            *.rs)   ctype="text/x-rust";;
            *.toml) ctype="application/toml";;
            *.json) ctype="application/json";;
            *.bmp)  ctype="image/bmp";;
            *.bin)  ctype="application/octet-stream";;
        esac
        encoded_rel=$(jq -nr --arg v "$rel" '$v|@uri')
        echo "  upload $rel ($size bytes, $ctype)"
        up_resp=$(curl -sS -X POST \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: $ctype" \
            -H "X-File-Sha256: $sha" \
            --data-binary @"$file_path" \
            "$LIB_URL/v1/library/skills/$ref/files/$encoded_rel" || true)
        file_count=$((file_count + 1))
        total_bytes=$((total_bytes + size))
    done < <(find "$skill_dir" -type f)

    # Optional sync to broker.
    if [[ $SYNC -eq 1 ]]; then
        sync_resp=$(curl -sS -X POST \
            -H "Authorization: Bearer $API_KEY" \
            "$LIB_URL/v1/library/skills/$ref/sync-to-broker" || true)
        echo "  sync:    $sync_resp"
    fi

    pushed=$((pushed + 1))
done

echo ""
echo "Done. pushed=$pushed failed=$failed"
