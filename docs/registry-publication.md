# Подготовка официального MCP Registry

Статус на 14.11.2026: GitHub и Release публичны. `server.json` — подготовленный
проект записи, не подтверждение регистрации. PyPI и MCP Registry не опубликованы.
Проверка Google не требуется для демо, CSV/XLSX или PostgreSQL.

## Выбранный путь

- Имя MCP: `io.github.finuchetai/finagent-mcp`.
- Python-пакет: `open-finance-mcp`, версия `0.7.0rc2`.
- Транспорт: локальный stdio; запуск по умолчанию использует вымышленные данные.
- Публикация пакета: официальный PyPI, затем метаданные в MCP Registry.

Обычный ZIP исходников и наш Windows ZIP не являются форматом MCPB, поэтому
их нельзя объявлять MCPB-пакетами без отдельной упаковки и проверки. Готового
публичного HTTPS MCP endpoint у этого проекта нет; фиктивный remote не указываем.

Каталог хранит метаданные, а не сам пакет. PyPI проверяется до регистрации:
на 14.11.2026 запрос `https://pypi.org/pypi/open-finance-mcp/json` вернул 404.
Это не резервирование имени и не доказательство прав на него; доступность
нужно проверить заново непосредственно перед публикацией.

## Что подготовлено

- Русский и английский README с назначением, установкой, примерами и ограничениями.
- Описание будущего PyPI-пакета берётся из английского README; ссылки ведут
  на публичную документацию GitHub.
- В README добавлен маркер `mcp-name: io.github.finuchetai/finagent-mcp`:
  Registry сверяет его с описанием пакета в PyPI.
- `server.json` содержит фиксированную версию, репозиторий и путь PyPI/stdio.
  Базовая запись запускает демо. Реальные источники подключаются отдельно
  в постоянной venv с extras и согласованной картой, как описано в README.
- Пакет не заявляет автоматическую установку Google-входа из PyPI.
  OAuth-приложение и пользовательские grants не входят в публичный Python-пакет.
- `MANIFEST.in` включает инструкции, примеры и установочные скрипты в sdist,
  чтобы исходный пакет можно было проверить по README.

## Проверка перед публикацией

Работать в чистой копии зафиксированного коммита, в отдельной venv.

```sh
python3 preflight.py environment
python3 -m venv .venv
.venv/bin/python -m pip install build twine
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*.whl dist/*.tar.gz
```

Проверить состав обоих архивов, отсутствие конфигурации Google, grants, ключей,
профилей, финансовых экспортов и локальных путей владельца. Sdist должен
содержать AGENTS.md, preflight.py, docs/, examples/, scripts/ и тесты; wheel —
модуль, встроенные инструкции и entry points. Проверить маркер `mcp-name`
в METADATA wheel и PKG-INFO sdist.

Установить wheel в новую постоянную venv, отдельно проверить stdio и демо,
затем extras CSV/XLSX/PostgreSQL. Не считать пропуск PostgreSQL runtime-тестов
проверкой живой БД. Повторить CI на точном коммите подготовки.

`server.json` проверяется по указанной в нём официальной JSON Schema.
Структурная проверка не подтверждает наличие пакета или права публикации.
После появления пакета использовать проверку `mcp-publisher validate`.

## Что нужно для реальной публикации

1. Доступ владельца к аккаунту PyPI и настройка доверенного издателя GitHub
   Actions либо иной согласованный способ загрузки. Не передавать токены в чат.
   Подготовка файлов сама по себе не создаёт аккаунт и не публикует пакет.
2. Опубликовать проверенные wheel/sdist. Убедиться, что версия, описание,
   маркер владения и установка из PyPI корректны. Версия в `server.json`
   должна соответствовать реально опубликованной версии.
3. Авторизовать namespace организации `finuchetai` в MCP Registry.
   По актуальной инструкции интерактивный GitHub device login не подтверждает
   namespace организации. Предпочтителен GitHub Actions OIDC; альтернативный
   PAT должен позволять прочитать роль Owner организации. Не заменять это
   фиктивным endpoint или публикацией под чужим namespace.
4. Опубликовать проверенный `server.json`, затем прочитать запись из Registry API
   и проверить настоящий запуск через выбранный MCP-клиент.

Эти шаги публикации не запускаются автоматически при обычном push. Уже
опубликованные тег и файлы GitHub Release 0.7.0rc2 сохраняются без замены.
Если пакет или версия меняются до PyPI, обновить связанные метаданные и
проверить новый комплект отдельно.

## Официальные требования

- [Порядок публикации](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx).
- [PyPI и проверка владения](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx).
- [Поддержанные реестры](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/official-registry-requirements.md).
- [Namespace организации и авторизация](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/authentication.mdx).
- [GitHub Actions OIDC](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx).
- [JSON Schema](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json).
