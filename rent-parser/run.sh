#!/usr/bin/env bash
# Единая точка запуска сбора аренды
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR/.."

python3 rent-parser/main.py "$@"

# Дополнительные шаги — отдельные скрипты: если какой-то сломается, база и карта уже готовы
extra=0
python3 rent-parser/transit/transit.py && extra=1 || echo "⚠️  время в пути не посчитано (на базу и карту не влияет)"
python3 rent-parser/smart/smart.py && extra=1 || echo "⚠️  умный фильтр не посчитан (на базу и карту не влияет)"
if [ "$extra" = 1 ]; then
  PYTHONPATH=rent-parser:rent-parser/pipeline python3 -c "from export_viewer import export_to_viewer; export_to_viewer()" \
    || echo "⚠️  карта не пересобрана с доп. данными — откроется прежняя"
  # Новое в ⭐ Топе → список здесь, в терминале
  node rent-parser/smart/top_alert.js || true
fi

# Карта на GitHub Pages (без чужих телефонов) — если не вышло, локальная карта всё равно готова
bash rent-parser/publish/publish.sh || echo "⚠️  сайт не обновлён (база и локальная карта готовы)"
