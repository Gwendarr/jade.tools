# -*- coding: utf-8 -*-
"""Реестр инструментов.

Каждый модуль-инструмент экспортирует:
  * bp    — Flask Blueprint со своими роутами (страница + API);
  * TOOL  — словарь с метаданными для лендинга и сайдбара:
              {id, name, desc, icon (SVG-строка), url (путь к странице)}.

Чтобы добавить новый инструмент — создайте модуль рядом и впишите его в ALL.
"""

from . import youtube, compress, trim

ALL = [youtube, compress, trim]


def blueprints():
    return [m.bp for m in ALL]


def tools():
    return [m.TOOL for m in ALL]
