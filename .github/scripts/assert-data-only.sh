#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --staged | <base-ref>" >&2
  exit 2
}

if [[ $# -ne 1 ]]; then
  usage
fi

declare -a command
if [[ "$1" == "--staged" ]]; then
  command=(git diff --cached --name-only --no-renames --diff-filter=ACDMRTUXB -z)
elif [[ "$1" == -* ]]; then
  usage
else
  command=(git diff --name-only --no-renames --diff-filter=ACDMRTUXB -z "$1...HEAD")
fi

declare -a rejected=()
while IFS= read -r -d '' path; do
  if [[ "$path" != data/* ]]; then
    rejected+=("$path")
  fi
done < <("${command[@]}")

if (( ${#rejected[@]} > 0 )); then
  echo "Refusing automated push: changes outside data/ were found:" >&2
  printf '  %q\n' "${rejected[@]}" >&2
  exit 1
fi
