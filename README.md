# student-ekt — аренда рядом с кампусом УрФУ «Новокольцовский» (Екатеринбург)

Личный инструмент: собирает объявления об аренде по всему Екатеринбургу (Яндекс Недвижимость и Яндекс Аренда, Авито,
Домклик, ВК/Telegram), склеивает одну квартиру с разных сайтов, считает реальную стоимость на человека
(аренда + КУ + залог и комиссия), время в пути до кампуса, надёжность и цену к рынку — и показывает всё на карте.

**Карта:** https://rapulse.github.io/student-ekt/ (обновляется при каждом сборе; телефоны скрыты).

## Запуск (macOS)
```bash
./rent-parser/run.sh                 # каждый день: только новое (~10–15 мин, откроется Chrome)
./rent-parser/run.sh --full          # раз в неделю: все страницы, снятые объявления, изменения цен (~1 ч)
./rent-parser/run.sh --fast          # без браузера: Яндекс + ВК/TG
open map_all.html                    # карта локально (OpenStreetMap); map_yandex.html — на Яндекс Картах
```
Нужны: python3 с pandas/pyarrow, Node + `npm install` в `rent-parser/` (puppeteer-core), Google Chrome.
Ключ Яндекс Карт JS API — в `rent-parser/local_keys.json` (`{"yandex_maps_js": "…"}`, не в git).

Подробно: [`rent-parser/descr.md`](rent-parser/descr.md) (шпаргалка), [`rent-parser/TASK.md`](rent-parser/TASK.md) (решения),
[`CLAUDE.md`](CLAUDE.md) (цели и правила проекта), [`rent-parser/AGENT_TOP10.md`](rent-parser/AGENT_TOP10.md) (задание ИИ-агенту «10 лучших»).

Не в git: база `data/listings.parquet`, собранные карты, профиль Chrome, личные заметки (`_tmp/`, `CLAUDE.local.md`).
