#!/usr/bin/env bash
# Единая точка запуска сбора аренды
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR/.."

python3 rent-parser/main.py "$@"

# Время в пути до НВК (оценка) — отдельный шаг: если сломается, база и карта уже готовы
if python3 rent-parser/transit/transit.py; then
  PYTHONPATH=rent-parser:rent-parser/pipeline python3 -c "from export_viewer import export_to_viewer; export_to_viewer()" \
    || echo "⚠️  карта не пересобрана со временем в пути — откроется прежняя"
else
  echo "⚠️  время в пути не посчитано (на базу и карту не влияет)"
fi
