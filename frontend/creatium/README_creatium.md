# Creatium frontend (Memory Forever)

- Это фронтенд для виджета Memory Forever на платформе Creatium.
- Виджет — одна HTML-секция, в которой есть три части:
  - HTML-код (вкладка HTML),
  - CSS-код (вкладка CSS),
  - JS-код к публикации (вкладка JS).

Файлы в этом каталоге:

- `memoryforever.html` — разметка секции.
- `memoryforever.css` — стили секции.
- `memoryforever.js` — JS-логика (работает в браузере, без сборки).
- Код из этих файлов копируется вручную в соответствующие вкладки Creatium.

Ограничения:

- Это обычная страница в браузере: никакого Node.js, `require`, `import` и сборщиков.
- Можно использовать только нативный JS (ES5/ES6) и `fetch` для HTTP-запросов.
- Бэкенд доступен по URL: `https://memoryforever.onrender.com` (API_BASE).
- API:
  - `GET /v1/catalog` — получить список сцен/форматов/фонов/музыки.
  - `POST /v1/upload` (multipart/form-data, поле `files[]`) — загрузка фото.
  - `POST /v1/render/start` — запуск рендеринга (тело см. модель RenderRequest).
  - `GET /v1/render/status/{job_id}` — статус рендера.

Задача фронтенда:

- Позволить пользователю:
  1. выбрать сцену/формат/фон/музыку из `GET /v1/catalog`,
  2. загрузить 1–2 фото,
  3. запустить рендер (`/v1/render/start`),
  4. показывать прогресс и ссылку на готовое видео из `/v1/render/status`.