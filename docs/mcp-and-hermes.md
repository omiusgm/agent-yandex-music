# MCP и Hermes

Сначала установите зависимости и проверьте свой аккаунт по [README](../README.md). Копирование конфигурации не переносит авторизацию на другой компьютер.

## Обычный stdio MCP

Клиент запускает процесс и общается с ним через stdin/stdout. Для клиента, использующего блок `mcpServers`, пример выглядит так:

```json
{
  "mcpServers": {
    "yandex_music": {
      "command": "/absolute/path/agent-yandex-music/.venv/bin/python",
      "args": ["-m", "yandex_music_mcp.server"],
      "env": {
        "YANDEX_MUSIC_TOKEN_FILE": "/absolute/private/path/yandex-music-token",
        "YANDEX_MUSIC_EXPORT_DIR": "/absolute/path/agent-yandex-music/exports"
      }
    }
  }
}
```

Замените все пути абсолютными путями своей машины. Токен передаётся через путь к файлу, а не значением в JSON. Не заменяйте целиком существующую конфигурацию клиента: добавьте или обновите только `yandex_music`.

Схема внешнего блока и способы ограничения инструментов зависят от клиента. Приведённый JSON сам по себе не фильтрует инструменты upstream-сервера. Не считайте режим чтения включённым, пока не проверены настройки и фактический список инструментов конкретного клиента.

## Существующий профиль Hermes

У каждого профиля Hermes свои настройки. Обычно default использует `~/.hermes/config.yaml`, а именованный профиль — `~/.hermes/profiles/NAME/config.yaml`. При пользовательском `HERMES_HOME`, контейнере или отдельной службе путь может отличаться.

1. Проверьте установленную версию и доступные команды: `hermes --version`, `hermes --help`, `hermes profile list`, `hermes mcp --help`.
2. Определите, какой **уже существующий** профиль обслуживает нужного агента. Не создавайте и не переключайте профиль только ради подключения музыки.
3. Сделайте локальную резервную копию именно его `config.yaml`, сохранив права. Считайте копию приватной: в исходном конфиге могут быть секреты других интеграций.
4. Получите фрагмент и объедините только `mcp_servers.yandex_music` с существующим YAML. Не создавайте второй одноимённый корневой блок.

```bash
.venv/bin/python scripts/make_hermes_config.py \
  --token-file "$HOME/.yandex-music-mcp/token" --mode read
```

Генератор ничего не записывает в Hermes и использует абсолютный путь к Python этого проекта. `HERMES_HOME` определяет профиль Hermes; это не обязательно домашний каталог, в котором MCP ищет свою авторизацию. Поэтому путь к токену передаётся явно.

## Режимы инструментов

`make_hermes_config.py` создаёт `tools.include` из исходных имён MCP:

| Режим | Что входит |
| --- | --- |
| `read` | `account_info`, `search`, `get_liked_tracks`, `get_listening_history`, `list_my_playlists`, `get_playlist_tracks`, `get_album_tracks`, `get_playlist_by_url`, `get_artist_tracks`, `get_similar_tracks`, `get_my_wave`, `get_chart`, `export_tracks_to_file` |
| `manage` | Всё из `read`, плюс `create_playlist`, `add_tracks_to_playlist` |

Это список разрешённых вызовов, а не обещание полноты каждого ответа. Для полного снимка используйте `account_export.py`: обычный MCP-вызов может иметь лимит числа возвращаемых треков. `export_tracks_to_file` сохраняет метаданные на диск; он не скачивает аудио.

В актуальном Hermes:

- `include` содержит исходные имена сервера, без префикса `mcp_yandex_music_`.
- `include: []` скрывает все обычные инструменты. Отсутствие `include` и `exclude` разрешает все инструменты сервера.
- `include` имеет приоритет над `exclude`. Для чёрного списка удалите `include`, а не оставляйте его пустым.
- Поддерживаются точные имена и регистрозависимые glob-шаблоны; точные списки удобнее переносить между версиями.
- `resources: false` и `prompts: false` отдельно отключают служебные обёртки.
- `supports_parallel_tool_calls: false` оставляет операции этого сервера последовательными.

Фильтр Hermes ограничивает видимые агенту MCP-инструменты. Он не делает OAuth-токен read-only и не ограничивает доступ агента к другим его инструментам.

## Проверка после подключения

После проверки поддержки команд в вашей версии:

```bash
hermes -p NAME mcp test yandex_music
```

Замените `NAME` существующим профилем. В его работающем чате выполните `/reload-mcp`, если команда доступна, либо откройте новую сессию того же профиля. Не используйте `hermes mcp reload`: в проверенном парсере такой подкоманды нет. Не перезапускайте все профили и ботов ради одного MCP.

Затем поручите агенту прочитать аккаунт и перечислить ваши плейлисты. Сверьте владельца и несколько знакомых названий. `mcp test` проверяет подключение и обнаружение инструментов, а не успешное обращение к музыкальному аккаунту. Для режима `read` проверьте отсутствие инструментов изменения в доступном агенту наборе.

## Первичные источники

- [Hermes: MCP](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).
- [Hermes: профили](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/profiles.md).
- [Hermes: фильтрация инструментов](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool_registration.py).
- [Hermes: парсер команд MCP](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/subcommands/mcp.py).
- [Upstream MCP Яндекс Музыки](https://github.com/kenzolika/yandex-music-mcp).

Документация `main` может опережать установленную сборку. Наличие конкретной команды проверяется локальным `--help`; установка интеграции не требует безусловного обновления Hermes.
