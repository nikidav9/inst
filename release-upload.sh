#!/usr/bin/env bash
# Загрузка файла в GitHub Release без gh (на исполнителях Selectel gh не установлен,
# 08.11.2026 деплой падал с «gh: command not found»). Нужны curl и jq.
# Использование: GH_TOKEN=… scripts/release-upload.sh <тег> <файл> [заголовок релиза] [описание]
# Релиз создаётся, если его нет (не «latest»); одноимённый файл заменяется.
set -euo pipefail
tag="${1:?тег}"; file="${2:?файл}"; title="${3:-$1}"; notes="${4:-Собирается автоматически.}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}"
api="https://api.github.com/repos/$repo"
auth=(-H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN не задан}" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")

rel=$(curl -sS "${auth[@]}" "$api/releases/tags/$tag" || true)
id=$(printf '%s' "$rel" | jq -r '.id // empty' 2>/dev/null || true)
if [ -z "$id" ]; then
  body=$(jq -n --arg t "$tag" --arg n "$title" --arg b "$notes" '{tag_name:$t,name:$n,body:$b,make_latest:"false"}')
  rel=$(curl -sS -X POST "${auth[@]}" -H "Content-Type: application/json" -d "$body" "$api/releases")
  id=$(printf '%s' "$rel" | jq -r '.id // empty')
  [ -n "$id" ] || { echo "Не удалось создать релиз $tag: $(printf '%s' "$rel" | head -c 300)"; exit 1; }
fi
name=$(basename "$file")
out=$(mktemp); trap 'rm -f "$out"' EXIT   # свой временный файл: исполнители Selectel идут под разными пользователями
old=$(curl -sS "${auth[@]}" "$api/releases/$id/assets?per_page=100" | jq -r --arg n "$name" '.[]|select(.name==$n)|.id')
for a in $old; do curl -sS -X DELETE "${auth[@]}" "$api/releases/assets/$a" -o /dev/null; done
code=$(curl -sS -o "$out" -w '%{http_code}' -X POST "${auth[@]}" -H "Content-Type: application/octet-stream" \
  --data-binary @"$file" "https://uploads.github.com/repos/$repo/releases/$id/assets?name=$name")
[ "$code" = 201 ] || { echo "Загрузка $name: HTTP $code $(head -c 300 "$out")"; exit 1; }
echo "Загружено: $name → релиз $tag"
