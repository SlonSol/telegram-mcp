# Как запустить эту сборку на бесплатной виртуалке Google Cloud

Это личная сборка [telegram-mcp](https://github.com/chigwell/telegram-mcp). Она **только читает** Telegram: не отправляет, не меняет, не удаляет сообщения и не отмечает их прочитанными. Пока программа запущена, она запоминает сообщения в файл на диске. Если сообщение потом удалят, текст останется в этом архиве.

Виртуалка `e2-micro` маленькая: около 1 ГБ памяти и 30 ГБ диска. Ниже хватает настроек по умолчанию: в архив пишется текст и только небольшие фотографии (до 1,5 МБ каждая, всего до 512 МБ). Старые фото стираются первыми, текст не стирается.

Команды ниже вводите в терминале виртуалки. Файл `.env` — это обычный текстовый файл с настройками. Его нельзя никому отправлять и нельзя коммитить.

## 1. Ключи API

1. Откройте в браузере <https://my.telegram.org> и войдите своим номером телефона.
2. Раздел **API development tools**.
3. Создайте приложение. Название может быть любым, например `telegram-mcp`.
4. Скопируйте два значения: **App api_id** (число) и **App api_hash** (строка).

Это не пароль от Telegram, но это секрет. Положите их только в `.env` на виртуалке.

## 2. Файл настроек

На виртуалке, в папке с этой программой:

```bash
cp .env.example .env
```

Откройте `.env` и впишите:

```env
TELEGRAM_API_ID=сюда_число
TELEGRAM_API_HASH=сюда_hash
TELEGRAM_HARD_READONLY=1
TELEGRAM_EXPOSED_TOOLS=read-only
TELEGRAM_ARCHIVE_MEDIA=photos
```

`TELEGRAM_HARD_READONLY=1` уже стоит в примере. Не меняйте его на `0`: тогда программа снова сможет отправлять сообщения.

Придумайте длинный пароль для HTTP и впишите его одной строкой, без кавычек и пробелов:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

```env
MCP_AUTH_TOKEN=сюда_вставить_пароль
```

Помощник (Cursor, Claude и другие) должен присылать заголовок `Authorization: Bearer сюда_вставить_пароль`. Без этого пароля программа, запущенная через Docker, не стартует: внутри контейнера она слушает все адреса, а не только localhost.

## 3. Войти в Telegram (QR)

Сессию удобнее сделать на самой виртуалке, один раз.

Если Python-зависимости ещё не стоят:

```bash
pip install -r requirements.txt
python session_string_generator.py --qr
```

На экране появится QR-код. В телефоне: Telegram → Настройки → Устройства → Подключить устройство, и отсканируйте код. Если программа попросит пароль двухфакторной защиты — введите его. В конце она напечатает длинную строку сессии.

Впишите её в `.env`:

```env
TELEGRAM_SESSION_STRING=длинная_строка
```

Строка сессии — это полный доступ к аккаунту. Храните её только в `.env`.

Чтобы архив не терял место в потоке обновлений после перезапуска, перенесите сессию в файл и уберите строку:

```bash
python migrate_session.py --target data/telegram_session
```

Потом в `.env` закомментируйте `TELEGRAM_SESSION_STRING` (поставьте `#` в начале строки) и оставьте:

```env
TELEGRAM_SESSION_NAME=data/telegram_session
```

Пока в файле есть и строка, и имя файла, используется строка. Файл сессии тогда не обновляется.

Тот же QR можно запустить из уже собранного образа:

```bash
docker compose run --rm -it telegram-mcp python session_string_generator.py --qr
```

## 4. Запуск

### Docker Compose (проще)

```bash
mkdir -p data
sudo chown -R 1000:1000 data
docker compose up --build -d
docker compose logs -f
```

Папка `data` на диске виртуалки хранит файл сессии, архив удалённых сообщений и кэш расшифровок. Без этой папки всё пропадёт при пересборке контейнера.

В логе должна быть строка, что `TELEGRAM_HARD_READONLY` включён. Остановка и обновление контейнера: `docker compose up --build -d` после `git pull`.

Программа слушает порт `127.0.0.1:8765` на виртуалке. Снаружи интернета порт не открыт. Помощник на этой же машине подключается так:

```json
{
  "mcpServers": {
    "telegram": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer сюда_вставить_пароль"
      }
    }
  }
}
```

### systemd (если без Docker)

Файл `/etc/systemd/system/telegram-mcp.service`:

```ini
[Unit]
Description=Telegram MCP (read-only)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/telegram-mcp
Environment=MCP_TRANSPORT=http
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT=8765
Environment=TELEGRAM_HARD_READONLY=1
EnvironmentFile=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/telegram-mcp/.env
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
User=ВАШ_ПОЛЬЗОВАТЕЛЬ

[Install]
WantedBy=multi-user.target
```

`MCP_HOST=127.0.0.1` — программа слушает только эту машину. Пароль `MCP_AUTH_TOKEN` тогда не обязателен, но лучше его всё равно задать в `.env`: если он задан, без заголовка `Authorization` запросы не проходят.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-mcp
sudo systemctl status telegram-mcp
```

## 5. Что делает архив

Пока процесс запущен, он запоминает новые и изменённые сообщения во всех чатах: личных, группах, супергруппах и каналах. В базе лежат номер чата, название, отправитель, номер сообщения, дата, текст, тип вложения, ответ и пересылка, а также старые варианты текста после правок.

Когда Telegram сообщает об удалении, строка помечается удалённой и получает время удаления. Строка не стирается.

У Telegram есть особенность: в личных чатах и обычных группах удаление приходит **без номера чата** (номера сообщений там уникальны на весь аккаунт). В каналах и супергруппах номер канала есть. Архив учитывает оба случая.

Помощник может спросить архив этими инструментами. Они читают только локальный файл и в Telegram ничего не отправляют:

- `list_deleted_messages` — недавно удалённые, с фильтром по чату, отправителю и времени
- `search_archive` — поиск по тексту, включая удалённые
- `get_message_edit_history` — старые версии одного сообщения
- `archive_stats` — сколько сообщений лежит в архиве

Сообщения, отправленные **до** запуска программы, в архив не попадают. Удаление такого сообщения восстановить нельзя: программа его не видела.

В режиме HTTP (`docker compose` или systemd выше) запись идёт, пока запущен сервис. В режиме stdio (когда помощник сам запускает программу на время разговора) запись идёт только пока этот разговор держит процесс. После отключения новые удаления не сохраняются, но уже записанное остаётся в `data/archive.sqlite`. Для постоянной записи нужен HTTP.

Чтение истории и приём обновлений не ставят галочки «прочитано». Программа не вызывает отметку прочтения сообщений, упоминаний, реакций и не открывает сторис. Подключённый клиент может выглядеть в сети так же, как любой просто включённый Telegram-клиент, который ничего не пишет. Отдельную команду «я онлайн» эта сборка не отправляет.

## 6. Обновление из upstream

Эта папка — ваш форк. Новые правки автора chigwell подтягиваются так, и запрос на слияние открывается только в **ваш** репозиторий `SlonSol/telegram-mcp`, не в `chigwell/telegram-mcp`.

```bash
git remote add upstream https://github.com/chigwell/telegram-mcp.git   # один раз
git fetch upstream
git checkout main
git merge upstream/main
```

Новый код этой сборки лежит в отдельных файлах (`telegram_mcp/hard_readonly.py`, `telegram_mcp/archive/`, `telegram_mcp/transport/auth.py`, `telegram_mcp/tools/archive.py`, `DEPLOY.md`). Их слияние обычно не трогает. Конфликты чаще бывают в `telegram_mcp/runtime.py`, `telegram_mcp/runner.py`, `telegram_mcp/transport/http.py`, `telegram_mcp/transcription.py`, `docker-compose.yml`, `Dockerfile`, `.env.example` и `README.md`. После слияния оставьте `TELEGRAM_HARD_READONLY` включённым и не коммитьте `.env` и файлы `*.session`.

Проверка:

```bash
python -m pytest
```
