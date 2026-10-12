#!/usr/bin/env bash
# Карта → GitHub Pages: ветка gh-pages репозитория origin перезаписывается одним коммитом (без истории,
# чтобы 16 МБ карты не копились). Вход в GitHub — как у git обычно (связка ключей macOS).
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
ROOT="$DIR/../.."
SITE="$DIR/../tmp/site"

[ -f "$ROOT/map_yandex.html" ] || { echo "⚠️  нет map_yandex.html (нужен ключ в rent-parser/local_keys.json) — не публикую"; exit 1; }
REMOTE="$(git -C "$ROOT" remote get-url origin 2>/dev/null)" || { echo "⚠️  у проекта нет origin — не публикую"; exit 1; }

rm -rf "$SITE"
python3 "$DIR/build_web.py" "$SITE"
git -C "$SITE" init -q -b gh-pages
git -C "$SITE" add -A
git -C "$SITE" commit -q -m "Карта $(date '+%Y-%m-%d %H:%M')"
# карта ~3 МБ в сжатом виде: без большого буфера git шлёт её кусками, и часть прокси отвечает HTTP 400
git -C "$SITE" -c http.postBuffer=157286400 push -q --force "$REMOTE" gh-pages
URL="$(echo "$REMOTE" | sed -E 's#(git@github.com:|https://github.com/)([^/]+)/([^/.]+)(\.git)?#https://\2.github.io/\3/#')"
echo "🌐 опубликовано: $URL (обновится за 1–2 мин)"
