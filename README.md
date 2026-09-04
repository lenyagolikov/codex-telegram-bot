# Scooters Codex Telegram Bot

Приватный Telegram-интерфейс для Codex. Бот принимает текстовые и голосовые
сообщения, передаёт их в `codex app-server` и возвращает в Telegram финальный
ответ. Поддерживаются macOS, Linux и Windows; Docker не требуется.

> Проект запускает Codex на том же компьютере, где работает бот. Он не является
> удалённым прокси к уже открытому Codex Desktop.

## Возможности

- отдельный постоянный Codex-диалог для каждого Telegram-чата;
- префикс `[Telegram]` в названии задач Codex;
- только финальный ответ без промежуточных рассуждений;
- преобразование Markdown в безопасный Telegram HTML;
- повторная доставка финальных ответов через SQLite outbox;
- команды `/new`, `/status`, `/cancel`, `/diff` и `/help`;
- вопросы Codex, подтверждения и MCP-формы через текст и inline-кнопки;
- опциональное локальное распознавание голосовых через `faster-whisper`;
- опциональное авто-подтверждение строго ограниченных операций чтения;
- allowlist пользователей и работа только в личных чатах.

## Требования

- для готового приложения Python не нужен;
- установленная команда `codex`;
- выполненный `codex login`;
- Telegram-бот, созданный через [@BotFather](https://t.me/BotFather).

Codex CLI должен самостоятельно запускаться на выбранной машине. Актуальные
сведения по App Server находятся в
[официальной документации Codex](https://developers.openai.com/codex/app-server/).

## Установка готового приложения

Скачай архив для своей ОС со страницы
[GitHub Releases](https://github.com/lenyagolikov/scooters-codex-telegram-bot/releases):

- `CodexTelegramBot-macos-arm64.zip` для Mac с Apple Silicon;
- `CodexTelegramBot-linux-x86_64.tar.gz` для Linux x86_64;
- `CodexTelegramBot-windows-x86_64.zip` для Windows x86_64.

Распакуй архив и запусти `CodexTelegramBot`. Откроется окно настройки: укажи
токен Telegram-бота, разрешённые Telegram user ID, рабочую папку и путь к
Codex CLI. Кнопка «Сохранить и запустить в фоне» установит системный сервис и
запустит бота. После установки сервиса не перемещай исполняемый файл — сначала
останови сервис, перенеси файл и установи сервис заново.

Пока релизы не подписаны сертификатом разработчика, macOS или Windows могут
показать системное предупреждение при первом запуске. Скачивай приложение
только со страницы релизов этого репозитория.

## Установка из исходного кода

Для этого варианта нужен Python 3.10–3.13.

Склонируй репозиторий и перейди в него:

```bash
git clone https://github.com/lenyagolikov/scooters-codex-telegram-bot.git
cd scooters-codex-telegram-bot
```

### macOS и Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

С голосовыми сообщениями:

```bash
python -m pip install ".[voice]"
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

С голосовыми сообщениями:

```powershell
python -m pip install ".[voice]"
```

Открыть графическое окно настройки после установки из исходного кода:

```bash
scooters-codex-telegram-bot-gui
```

## Настройка

В готовом приложении все параметры задаются через графическое окно. Ручная
настройка `.env` остаётся для серверов, автоматизации и установки из исходников.

Скопируй пример рядом с проектом:

```bash
cp .env.example .env
```

На Windows:

```powershell
Copy-Item .env.example .env
```

Заполни как минимум:

```dotenv
TELEGRAM_BOT_TOKEN=token-from-botfather
TELEGRAM_ALLOWED_USER_IDS=123456789
CODEX_CWD=/absolute/path/to/project
```

Не отправляй токен в чат и не добавляй `.env` в Git. Для первого запуска можно
оставить allowlist пустым: команда `/start` покажет Telegram user ID, но бот не
будет выполнять запросы. После этого добавь ID и перезапусти процесс.

Проверка конфигурации без запуска бота:

```bash
scooters-codex-telegram-bot --check
```

Запуск:

```bash
scooters-codex-telegram-bot
```

Совместимый старый способ также оставлен:

```bash
python run_bot.py
```

## Где хранятся настройки

CLI ищет конфигурацию в следующем порядке:

1. путь из `--config`;
2. путь из `BOT_CONFIG_PATH`;
3. `.env` в текущей директории;
4. системная директория конфигурации.

Системные пути по умолчанию:

| ОС | Конфигурация | SQLite-состояние |
|---|---|---|
| macOS | `~/Library/Application Support/scooters-codex-telegram-bot/.env` | там же, `state.sqlite3` |
| Linux | `~/.config/scooters-codex-telegram-bot/.env` | `~/.local/state/scooters-codex-telegram-bot/state.sqlite3` |
| Windows | `%APPDATA%\scooters-codex-telegram-bot\.env` | `%LOCALAPPDATA%\scooters-codex-telegram-bot\state.sqlite3` |

Графическое приложение пытается сохранить токен Telegram в системном хранилище
секретов: Keychain на macOS, Credential Manager на Windows или Secret Service
на Linux. Если хранилище недоступно, токен записывается в `.env`, созданный
атомарно с правами только для текущего пользователя. Остальные настройки всегда
хранятся в `.env`, чтобы фоновый процесс мог прочитать их без открытого окна.

Фактически выбранный путь можно вывести командой:

```bash
scooters-codex-telegram-bot --show-config-path
```

## Переменные окружения

| Переменная | Назначение | Значение по умолчанию |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | токен BotFather | обязательна |
| `TELEGRAM_ALLOWED_USER_IDS` | Telegram user ID через запятую | пустой allowlist |
| `CODEX_CWD` | рабочая директория Codex | текущая директория |
| `CODEX_BIN` | путь или имя Codex CLI | `codex` |
| `CODEX_MODEL` | модель Codex | настройка CLI |
| `CODEX_REASONING_EFFORT` | уровень рассуждения | настройка CLI |
| `BOT_STATE_PATH` | путь к SQLite | системная директория данных |
| `TELEGRAM_IP_FAMILY` | `auto`, `ipv4` или `ipv6` | `auto` |
| `POLL_TIMEOUT_SECONDS` | таймаут long polling | `30` |
| `VOICE_TRANSCRIPTION_ENABLED` | голосовая расшифровка | `false` |
| `WHISPER_MODEL` | модель faster-whisper | `small` |
| `WHISPER_LANGUAGE` | язык или пусто для autodetect | `ru` |
| `VOICE_MAX_DURATION_SECONDS` | предел длительности | `600` |
| `VOICE_MAX_FILE_BYTES` | предел размера | `20971520` |
| `AUTO_APPROVE_SAFE_READ_ONLY` | узкое автоподтверждение чтения | `false` |
| `AUTO_APPROVE_READ_ROOTS` | разрешённые корни через запятую | `CODEX_CWD` |

Настоящие переменные окружения имеют приоритет над значениями из `.env`.

## Голосовые сообщения

Установи voice-extra и включи функцию:

```dotenv
VOICE_TRANSCRIPTION_ENABLED=true
WHISPER_MODEL=small
WHISPER_LANGUAGE=ru
```

Модель загружается при первом голосовом сообщении и работает локально на CPU.
Временный аудиофайл создаётся с ограниченными правами и удаляется после
обработки. Аудио и текст расшифровки не записываются в лог.

## Автоматическое подтверждение чтения

Функция выключена по умолчанию. Для включения:

```dotenv
AUTO_APPROVE_SAFE_READ_ONLY=true
AUTO_APPROVE_READ_ROOTS=/path/to/project,/path/to/read-only-docs
```

Автоматически подтверждается только запрос, для которого Codex App Server
передал непустой список действий и классифицировал каждое как `read`,
`listFiles` или `search`. Все пути должны оставаться внутри разрешённых корней.

Неизвестные действия, запись, сеть, дополнительные права на запись и чтение
чувствительных файлов (`.env`, SSH/Codex-конфигурация, shell history и ключи)
автоматически не подтверждаются и показываются пользователю в Telegram.

## Команды Telegram

- `/new` — создать новый Codex-диалог со следующим сообщением;
- `/status` — показать текущий диалог, задачу и состояние App Server;
- `/cancel` — остановить текущую задачу;
- `/diff` — показать последний полученный diff;
- `/help` — открыть справку.

Во время вопроса MCP команда `/skip` пропускает необязательное поле, а `/cancel`
отменяет только текущую форму. Подтверждения и вопросы ожидают ответа до 24 часов.

## Фоновый запуск

Проще всего открыть `CodexTelegramBot` и нажать «Сохранить и запустить в фоне».
Приложение само создаст службу текущего пользователя: systemd user service в
Linux, LaunchAgent в macOS или задачу Task Scheduler в Windows. Статус и
управление доступны в том же окне.

Те же операции можно выполнить из терминала:

```bash
scooters-codex-telegram-bot --install-service
scooters-codex-telegram-bot --service-status
scooters-codex-telegram-bot --restart-service
scooters-codex-telegram-bot --stop-service
scooters-codex-telegram-bot --uninstall-service
```

Для готового приложения вместо имени команды укажи путь к исполняемому файлу.
Опция `--config /path/to/.env` позволяет выбрать явный файл настроек.

### Ручная установка

Готовые шаблоны лежат в `deploy/`. В них нет секретов: замени значения
`__BOT_EXECUTABLE__`, `__CONFIG_FILE__` и `__WORKING_DIRECTORY__` абсолютными
путями своей установки.

Узнать путь CLI после активации virtualenv:

```bash
command -v scooters-codex-telegram-bot
```

На Windows:

```powershell
(Get-Command scooters-codex-telegram-bot).Source
```

### Linux: systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/linux/scooters-codex-telegram-bot.service.example \
  ~/.config/systemd/user/scooters-codex-telegram-bot.service
# Отредактируй три __PLACEHOLDER__ в скопированном файле.
systemctl --user daemon-reload
systemctl --user enable --now scooters-codex-telegram-bot
systemctl --user status scooters-codex-telegram-bot
```

Для работы без открытой SSH-сессии администратор может включить systemd linger:

```bash
loginctl enable-linger "$USER"
```

### macOS: LaunchAgent

```bash
cp deploy/macos/com.scooters.codex-telegram-bot.plist.example \
  ~/Library/LaunchAgents/com.scooters.codex-telegram-bot.plist
# Отредактируй __PLACEHOLDER__ и создай директорию для логов.
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.scooters.codex-telegram-bot.plist
launchctl print "gui/$(id -u)/com.scooters.codex-telegram-bot"
```

LaunchAgent не выполняется, пока Mac спит.

### Windows: Task Scheduler

Запусти PowerShell от своего пользователя после активации virtualenv:

```powershell
.\deploy\windows\install-task.ps1 `
  -BotExecutable (Get-Command scooters-codex-telegram-bot).Source `
  -ConfigFile "$env:APPDATA\scooters-codex-telegram-bot\.env"
```

Проверка и удаление задачи:

```powershell
Get-ScheduledTask -TaskName "Scooters Codex Telegram Bot"
Unregister-ScheduledTask -TaskName "Scooters Codex Telegram Bot"
```

Одновременно должна работать только одна копия бота с одним Telegram-токеном.

## Разработка

```bash
python -m pip install -e ".[dev]"
ruff check .
python -m unittest discover -s tests -v
python -m build
```

Локальная сборка готового приложения для текущей ОС:

```bash
python -m pip install -e ".[desktop]"
python scripts/build_executable.py
```

Результат появится в `dist/`. PyInstaller не выполняет кросс-компиляцию, поэтому
GitHub Actions собирает отдельный артефакт на Linux, macOS и Windows. Для
публикации релиза создай и отправь тег:

```bash
git tag v0.3.0
git push origin v0.3.0
```

Workflow проверит код, соберёт три архива и прикрепит их к GitHub Release.
Обычные проверки pull request продолжают выполняться на Python 3.10 и 3.13.

## Безопасность

- бот не открывает входящий порт и использует Telegram long polling;
- токен не передаётся Codex, не попадает в логи и по возможности хранится в
  системном хранилище секретов;
- `.env` с резервной копией токена создаётся только с правами владельца и не
  должен попадать в Git;
- сообщения принимаются только в личных чатах и только от allowlist;
- запросы секретных значений в MCP-формах отклоняются;
- политика Codex остаётся `workspace-write` и `on-request`;
- авто-подтверждение чтения выключено до явной настройки.

## Ограничения

- нужен отдельно установленный и авторизованный Codex CLI;
- вывод команд не стримится построчно — пользователь получает финальный ответ;
- задачи, созданные через App Server, могут отображаться в Codex Desktop вне
  конкретного Desktop-проекта;
- доступность MCP зависит от конфигурации и окружения процесса бота;
- фоновый процесс на ноутбуке не работает во время сна устройства.

Интеграция использует официальный
[Telegram Bot API](https://core.telegram.org/bots/api) и
[Codex App Server](https://developers.openai.com/codex/app-server/).
