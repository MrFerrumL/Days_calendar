# Источники данных

## MET Norway

- Источник: Norwegian Meteorological Institute, [MET Norway](https://www.met.no/).
- API: [Locationforecast 2.0](https://api.met.no/weatherapi/locationforecast/2.0/documentation).
- Лицензия: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
- Обработка: перевод обозначений погоды на русский, округление значений и сводка доступных часов дня. MET Norway не является автором или стороной, поддерживающей этот бот.
- Условия: [Terms of Service](https://docs.api.met.no/doc/TermsOfService). User-Agent содержит название приложения и ссылку на репозиторий; HTTP Expires учитывается кэшем.
- `tests/fixtures/met.json` — сокращённый ответ API от 22.10.2026, используется только в тестах; та же лицензия CC BY 4.0.

## Open-Meteo

- Источник: [Open-Meteo](https://open-meteo.com/).
- Документация: [Weather Forecast API](https://open-meteo.com/en/docs).
- Данные: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Обработка: перевод кода погоды, округление и форматирование.

## Расписание

- Источник: [cube.nlabs.su](https://cube.nlabs.su/timetable?type=group).
- Сохранённая тестовая выборка содержит публичное расписание 19.10–01.11.2026. Технические идентификаторы автора изменения удалены из фикстуры.
- Дополнительное расписание перенесено со скриншотов пользователя; бот не принадлежит администрации вуза или сервису расписания.

## SOCKS-транспорт Telegram

- [PySocks](https://github.com/Anorov/PySocks), версия 1.7.1, лицензия BSD-3-Clause. Используется только при заданном `TELEGRAM_PROXY_URL`.
- Сервер использует клиент [Tor](https://www.torproject.org/) и [Snowflake](https://snowflake.torproject.org/); сервер не является выходным узлом или ретранслятором Tor. Эти службы устанавливаются отдельно от Python-приложения.
