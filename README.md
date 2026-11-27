# Codex CLI с моделями через Chat Completions

Маленький адаптер, который подключает к [Codex CLI](https://github.com/openai/codex)
модели провайдеров, у которых есть только Chat Completions API. Например, GigaChat 4
в Cloud.ru Foundation Models или GLM по подписке z.ai. Один файл на Python без зависимостей.

## Зачем он нужен

Codex CLI начиная с 0.171 говорит с моделью только через OpenAI Responses API
(`wire_api = "responses"`, режим `chat` удалён). Многие провайдеры отдают только
`/v1/chat/completions`. Напрямую они с Codex друг друга не понимают.

Адаптер встаёт между ними. Он принимает от Codex запрос `POST /v1/responses`, собирает
из него обычный диалог Chat Completions, вызывает провайдера в потоковом режиме
и переводит ответ обратно в события Responses: текст, вызовы инструментов
и рассуждения модели.

Что важно:

- Вызовы инструментов идут в стандартном формате OpenAI `tools`.
- Рассуждения модели возвращаются в Codex и попадают в историю диалога. Адаптер
  понимает оба распространённых поля: `reasoning_content` (так отдаёт z.ai) и `reasoning`
  (так отдаёт Cloud.ru). Если рассуждения потерять, модель на длинных задачах теряет
  нить и делает в разы больше шагов.

## Быстрый старт на примере GigaChat 4

Нужны Python 3.9+, Codex CLI 0.171+ и ключ API Cloud.ru Foundation Models.

1. Запустите адаптер. Ключ передаётся через переменную окружения, в командной строке
   указывается только её имя:

   ```bash
   export CLOUDRU_API_KEY="ваш ключ Cloud.ru"
   python3 responses_adapter.py 8094 https://foundation-models.api.cloud.ru/v1 CLOUDRU_API_KEY
   ```

2. Запустите Codex с отдельным чистым профилем из этого репозитория. Ваш основной
   `~/.codex` при этом не меняется:

   ```bash
   CODEX_HOME="$PWD/codex-home" codex
   ```

   Профиль `codex-home/config.toml`:

   ```toml
   model = "GigaChat/GigaChat-4-708B-A41"
   model_provider = "cloudru"

   [model_providers.cloudru]
   name = "GigaChat (Cloud.ru) через responses_adapter"
   base_url = "http://127.0.0.1:8094/v1"
   wire_api = "responses"
   requires_openai_auth = false
   supports_websockets = false
   ```

   Ключ в конфиг Codex не нужен: его хранит только адаптер.

3. Проверка без интерактива:

   ```bash
   CODEX_HOME="$PWD/codex-home" codex exec -s workspace-write "Создай hello.txt с текстом hi"
   ```

Предупреждение `Model metadata for GigaChat/GigaChat-4-708B-A41 not found` нормально:
Codex не знает параметров этой модели и берёт значения по умолчанию.

Не ставьте в профиле `model_reasoning_effort = "none"`: адаптер тогда выключает
рассуждения, и модель заметно хуже справляется с многошаговыми задачами.

## Другие провайдеры

Адаптер подойдёт для любого провайдера с OpenAI-совместимым `/v1/chat/completions`:
ему нужны только адрес API и переменная с ключом. Например, подписка z.ai GLM Coding Plan:

```bash
export ZAI_API_KEY="ваш ключ z.ai"
python3 responses_adapter.py 8093 https://api.z.ai/api/coding/paas/v4 ZAI_API_KEY zai/glm-5.3=glm-5.3
```

Последние аргументы `alias=модель` задают псевдонимы: Codex отправляет имя `zai/glm-5.3`,
а в апстрим уходит `glm-5.3`.

## Бенчмарки через Harbor

Адаптер подходит для прогона Terminal-Bench и других наборов через
[Harbor](https://github.com/harbor-framework/harbor) с агентом `codex`. Из контейнеров
адаптер доступен по адресу `host.docker.internal`:

```bash
export ADAPTER_KEY=local   # Harbor требует переменную с ключом, адаптер её не проверяет
harbor run -d terminal-bench@2.0 -i fix-git -a codex \
  -m GigaChat/GigaChat-4-708B-A41 --skip-model-parsing-and-provider-mapping \
  --api-key-name ADAPTER_KEY --base-url http://host.docker.internal:8094/v1 \
  --ak version=0.171.0 -n 2 -o jobs
```

Если контейнеры не достают до адаптера, запустите его с `ADAPTER_HOST=0.0.0.0`.
Учтите, что тогда любой, кто дотянется до порта, будет ходить в Cloud.ru с вашим ключом.

## Что поддерживается

- Потоковые ответы (`stream: true`), как их шлёт Codex.
- Текст, вызовы инструментов типа `function` (в том числе несколько за один ход),
  результаты инструментов, рассуждения модели.
- Учёт токенов: входные, кэшированные и выходные токены передаются в Codex.

Ограничения:

- Картинки из запроса не передаются, адаптер берёт только текст.
- Инструменты других типов (веб-поиск, `custom`) пропускаются.
- Авторизации на стороне адаптера нет, поэтому по умолчанию он слушает только 127.0.0.1.

Проверено на Codex CLI 0.171.0 и `GigaChat/GigaChat-4-708B-A41` в Cloud.ru, ноябрь 2026.

## Лицензия

MIT, см. [LICENSE](LICENSE).

## Об авторе

Вячеслав Старцев, эксперт по ИИ-агентам: как устроены, как работают, как внедрять. 30 лет в разработке. Автор ютуб-канала и статей на Хабре про ИИ-агентов: разборы агентных систем, практика внедрения, открытые инструменты.

Сайт: https://vyacheslav-startsev.ru
