#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный сервер для ARG-квеста «СЕКРЕТНЫЙ ТЕРМИНАЛ».

Зачем он нужен
--------------
Сайт — один файл index.html, но аудио и картинки лежат рядом папками
(audio/, clue_*.png). Если открыть index.html двойным щелчком (протокол
file://), браузер заблокирует часть загрузок как «cross-origin».
Поэтому проект раздаётся по HTTP.

Чем лучше `python3 -m http.server`
---------------------------------
1. HTTP/1.1 + keep-alive. Стандартный SimpleHTTPRequestHandler работает по
   HTTP/1.0 и закрывает TCP-соединение после каждого ответа. Через
   обратный прокси (в т.ч. превью Arena) это даёт ошибки вида
   ERR_CONNECTION_RESET / «соединение сброшено», потому что прокси
   пытается переиспользовать уже закрытое соединение.
2. Потоки (ThreadingHTTPServer) — несколько запросов одновременно:
   аудио + картинки + сам html не блокируют друг друга.
3. Корректные MIME-типы, включая .m4a -> audio/mp4.
4. Поддержка Range-запросов (206 Partial Content) — нужна, чтобы
   перемотка <audio> работала, а трек не грузился целиком.
5. Заголовки кэша: html не кэшируем (правки видны сразу), аудио/картинки
   кэшируем — повторное открывание мгновенное.

Запуск
------
    python3 server.py            # порт 8080 (по умолчанию)
    python3 server.py 3000       # свой порт
    PORT=3000 python3 server.py  # то же через переменную окружения
"""

import os
import posixpath
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ── Настройки ────────────────────────────────────────────────────────────
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
ROOT = os.path.dirname(os.path.abspath(__file__))

# Расширения, которым стандартная библиотека может не знать правильный тип.
# .m4a критичен: без audio/mp4 часть браузеров откажется играть запись.
EXTRA_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/x-wav",
    ".webm": "audio/webm",
    ".json": "application/json",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

# Что кэшировать и насколько (в секундах).
CACHE_LONG = 604800   # 7 суток — аудио и картинки (они не меняются)
CACHE_NONE = 0        # html — всегда свежий


class Handler(SimpleHTTPRequestHandler):
    """Раздаёт файлы проекта с keep-alive, Range и нормальными типами."""

    server_version = "ARGServer/1.0"
    protocol_version = "HTTP/1.1"   # <-- ключевая правка против RESET

    # ── Логи ────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        sys.stderr.write(
            "  %s %s\n" % (self.address_string(), fmt % args)
        )

    def handle_one_request(self):
        """Разрыв соединения клиентом — не повод сыпать трейсбеками в лог."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ── MIME-типы ───────────────────────────────────────────────────────
    def guess_type(self, path):
        base, ext = posixpath.splitext(path)
        if ext.lower() in EXTRA_TYPES:
            return EXTRA_TYPES[ext.lower()]
        return super().guess_type(path)

    # ── Кэш ─────────────────────────────────────────────────────────────
    def end_headers(self):
        path = urllib.parse.urlsplit(self.path).path
        ext = posixpath.splitext(path)[1].lower()
        if ext in ("", ".html", ".htm"):
            # html не кэшируем — иначе правки не будут видны после Ctrl+F5
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        else:
            self.send_header(
                "Cache-Control", "public, max-age=%d" % CACHE_LONG
            )
        super().end_headers()

    # ── Range (перемотка аудио) ─────────────────────────────────────────
    def send_head(self):
        """Отдаём заголовки; при Range отдаём 206 вместо 200."""
        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()

        size = os.path.getsize(path)
        ctype = self.guess_type(path)

        # Разбираем только простой вид: bytes=start-end / bytes=start-
        try:
            units, _, rng = range_header.partition("=")
            if units.strip() != "bytes":
                raise ValueError
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
        except ValueError:
            self.send_error(416, "Invalid Range header")
            return None

        if start >= size or end >= size or start > end:
            self.send_error(416, "Requested Range Not Satisfiable")
            return None

        length = end - start + 1
        self.send_response(206, "Partial Content")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.send_header(
            "Last-Modified",
            self.date_time_string(int(os.path.getmtime(path))),
        )
        self.end_headers()
        return _RangeFile(path, start, length)


class _RangeFile:
    """Мини-объект «файла», который отдаёт только нужный кусок."""

    def __init__(self, path, start, length):
        self._f = open(path, "rb")
        self._f.seek(start)
        self._left = length

    def read(self, n=-1):
        if self._left <= 0:
            return b""
        if n is None or n < 0:
            n = self._left
        n = min(n, self._left)
        data = self._f.read(n)
        self._left -= len(data)
        return data

    def close(self):
        if not self._f.closed:
            self._f.close()

    # SimpleHTTPRequestHandler вызывает copyfile(reader, wfile)
    def __iter__(self):
        return self

    def __next__(self):
        chunk = self.read(65536)
        if not chunk:
            raise StopIteration
        return chunk


def main():
    os.chdir(ROOT)
    handler = partial(Handler, directory=ROOT)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 128

        def handle_error(self, request, client_address):
            # Клиент отвалился (reset / broken pipe) — это норма для
            # прокси и браузерных прерываний, трейсбек не нужен.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    httpd = Server(("0.0.0.0", PORT), handler)

    print("=" * 62)
    print("  СЕКРЕТНЫЙ ТЕРМИНАЛ — ARG-квест")
    print("=" * 62)
    print("  Папка : %s" % ROOT)
    print("  Порт  : %d  (0.0.0.0 — доступен извне)" % PORT)
    print("  HTTP  : 1.1 + keep-alive + Range")
    print("  Стоп  : Ctrl+C")
    print("=" * 62)
    print("  Локально:  http://127.0.0.1:%d/index.html" % PORT)
    print("  Превью  :  https://%d-%s.e2b.app/" % (
        PORT, os.environ.get("E2B_SANDBOX_ID", "sandbox")))
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Остановлен.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
