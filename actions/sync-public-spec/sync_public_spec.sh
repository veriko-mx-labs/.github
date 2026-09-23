#!/usr/bin/env bash
set -euo pipefail

readonly SPEC_URL='https://docs.veriko.mx/openapi.yaml'
readonly BASE_BRANCH
readonly KEEPALIVE_BRANCH='automation/keep-schedules-active'
readonly KEEPALIVE_DAYS=45
readonly PR_TITLE='Sincronizar el spec público'

temporary_directory="$(mktemp -d)"
trap 'rm -rf "$temporary_directory"' EXIT
downloaded_spec="$temporary_directory/openapi.yaml"
body_file="$temporary_directory/pr-body.md"
checks_file="$temporary_directory/checks.json"
failure_file="$temporary_directory/failures.log"

git config user.name 'Veriko'
git config user.email 'soporte@veriko.mx'

curl --fail --location --silent --show-error \
  --retry 3 --retry-all-errors \
  --output "$downloaded_spec" \
  "$SPEC_URL"

python "$VALIDATOR" "$downloaded_spec"

maintain_schedule() {
  local now last_commit age_days
  now="$(date +%s)"

  if git fetch --quiet origin "$KEEPALIVE_BRANCH"; then
    last_commit="$(git show -s --format=%ct FETCH_HEAD)"
    age_days="$(( (now - last_commit) / 86400 ))"
    if (( age_days < KEEPALIVE_DAYS )); then
      echo "La actividad preventiva tiene ${age_days} día(s); no hace falta renovarla."
      return
    fi
  fi

  git switch --force-create "$KEEPALIVE_BRANCH" "origin/$BASE_BRANCH"
  git commit --allow-empty --message 'Mantener activa la sincronización programada'
  git push --force origin "HEAD:$KEEPALIVE_BRANCH"
  echo "Actividad preventiva renovada en $KEEPALIVE_BRANCH."
}

if cmp --silent "$downloaded_spec" "$SPEC_PATH"; then
  echo "$SPEC_PATH ya coincide con $SPEC_URL."
  maintain_schedule
  exit 0
fi

pr_number="$(gh pr list \
  --repo "$GITHUB_REPOSITORY" \
  --head "$SYNC_BRANCH" \
  --base "$BASE_BRANCH" \
  --state open \
  --json number \
  --jq '.[0].number // empty')"

if [[ -n "$pr_number" ]]; then
  git fetch --quiet origin "$SYNC_BRANCH"
  git switch --force-create "$SYNC_BRANCH" "origin/$SYNC_BRANCH"
else
  git fetch --quiet origin "$SYNC_BRANCH" || true
  git switch --force-create "$SYNC_BRANCH" "origin/$BASE_BRANCH"
fi

mkdir -p "$(dirname "$SPEC_PATH")"
cp "$downloaded_spec" "$SPEC_PATH"
bash -euo pipefail -c "$REGENERATE_COMMAND"
git add --all

if ! git diff --cached --quiet; then
  git commit --message 'Sincronizar el spec público'
  if [[ -n "$pr_number" ]]; then
    git push origin "HEAD:$SYNC_BRANCH"
  else
    git push --force-with-lease origin "HEAD:$SYNC_BRANCH"
  fi
fi

cat > "$body_file" <<EOF
## Resultado

**Pendiente:** el CI está comprobando el cambio publicado.

- Fuente: [$SPEC_URL]($SPEC_URL)
- Copia: \`$SPEC_PATH\`
- Rama del bot: \`$SYNC_BRANCH\`

El cuerpo se actualizará con el resultado.
EOF

if [[ -z "$pr_number" ]]; then
  pr_url="$(gh pr create \
    --repo "$GITHUB_REPOSITORY" \
    --base "$BASE_BRANCH" \
    --head "$SYNC_BRANCH" \
    --title "$PR_TITLE" \
    --body-file "$body_file")"
  pr_number="${pr_url##*/}"
else
  gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --body-file "$body_file"
fi

wait_for_checks() {
  local pending total
  for _ in $(seq 1 120); do
    if ! gh pr checks "$pr_number" \
      --repo "$GITHUB_REPOSITORY" \
      --json name,bucket,link,workflow > "$checks_file"; then
      if [[ ! -s "$checks_file" ]]; then
        echo '[]' > "$checks_file"
      fi
    fi

    total="$(jq 'length' "$checks_file")"
    pending="$(jq '[.[] | select(.bucket == "pending")] | length' "$checks_file")"
    if (( total > 0 && pending == 0 )); then
      return 0
    fi
    sleep 10
  done
  return 1
}

if ! wait_for_checks; then
  cat > "$body_file" <<EOF
## Resultado

**Rojo — el CI no terminó dentro de 20 minutos.**

- Fuente: [$SPEC_URL]($SPEC_URL)
- Copia: \`$SPEC_PATH\`

El PR queda abierto. Revisa si algún job no arrancó o quedó pendiente.
EOF
  gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --body-file "$body_file"
  exit 1
fi

failed_count="$(jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length' "$checks_file")"

if (( failed_count == 0 )); then
  cat > "$body_file" <<EOF
## Resultado

**Verde — cambio editorial o fuera de lo cubierto por los clientes actuales.**

- Fuente: [$SPEC_URL]($SPEC_URL)
- Copia: \`$SPEC_PATH\`
- CI: todos los jobs terminaron correctamente.
EOF
  gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --body-file "$body_file"

  if [[ "$AUTO_MERGE" == 'true' ]]; then
    gh pr merge "$pr_number" \
      --repo "$GITHUB_REPOSITORY" \
      --auto \
      --squash \
      --delete-branch
  fi
  exit 0
fi

: > "$failure_file"
{
  echo 'Resumen de checks:'
  jq -r '.[] | "- \(.workflow // "CI") / \(.name): \(.bucket)"' "$checks_file"
  echo
} >> "$failure_file"
while read -r run_id; do
  gh run view "$run_id" --repo "$GITHUB_REPOSITORY" --log-failed >> "$failure_file" 2>&1 || true
done < <(
  jq -r '.[] | select(.bucket == "fail" or .bucket == "cancel") | .link' "$checks_file" \
    | sed -nE 's#.*?/actions/runs/([0-9]+).*#\1#p' \
    | sort -u
)

if grep --quiet --extended-regexp \
  'familias del spec no traen operaciones|operaciones de máquina a máquina sin método' \
  "$failure_file"; then
  classification='Rojo — hay una operación de máquina a máquina nueva sin método.'
else
  classification='Rojo — cambió una operación que el cliente ya cubre.'
fi

python - "$failure_file" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = re.sub(r"\x1b\[[0-9;]*m", "", path.read_text(encoding="utf-8", errors="replace"))
text = text.replace("```", "` ` `")
lines = text.splitlines()
pattern = re.compile(
    r"not ok|failureType|error:|Expected values|ERR_ASSERTION|"
    r"operaciones de máquina a máquina|familias del spec|# fail|##\[error\]",
    re.IGNORECASE,
)
ranges = [(0, min(12, len(lines)))]
for index, line in enumerate(lines):
    if pattern.search(line):
        ranges.append((max(0, index - 4), min(len(lines), index + 21)))

merged = []
for start, end in sorted(ranges):
    if merged and start <= merged[-1][1]:
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    else:
        merged.append((start, end))

excerpt = "\n\n[…]\n\n".join("\n".join(lines[start:end]) for start, end in merged)
path.write_text(excerpt[-30000:], encoding="utf-8")
PY

cat > "$body_file" <<EOF
## Resultado

**$classification**

- Fuente: [$SPEC_URL]($SPEC_URL)
- Copia: \`$SPEC_PATH\`
- Acción: el PR queda abierto hasta que el cliente refleje el contrato publicado.

<details>
<summary>Fallo del CI</summary>

\`\`\`text
$(cat "$failure_file")
\`\`\`

</details>
EOF

gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --body-file "$body_file"
exit 1
