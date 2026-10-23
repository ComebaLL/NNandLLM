import ast
import ipaddress
import math
import operator
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from functools import lru_cache
from pathlib import Path

# Папка с документами для search_docs 
DOCS_DIR = Path(__file__).parent / "data" / "docs"
# Лимит длины ответа любого инструмента: длинный вывод забивает контекст маленькой модели
MAX_OUT = 3000

# Как Ollama использует эти функции: из сигнатуры (имя, типы аргументов) и docstring
# она строит JSON-схему инструмента. Поэтому docstring писать для МОДЕЛИ: коротко
# и с примером. Все инструменты возвращают str и не бросают исключений: текст ошибки
# модель видит и может исправить вызов (это тест "инструмент вернул ошибку").


# ======================================================================
# 1 calculator: безопасные вычисления
# ======================================================================
# Почему не eval(): eval выполнит любой код, в том числе опасный.
# Вместо этого разбираем выражение в синтаксическое дерево (ast) и сами считаем
# только разрешённые узлы: числа, арифметику и несколько функций.

# Разрешённые операторы: тип узла дерева -> функция, которая его выполняет
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,   # унарные минус и плюс: -5, +3
}
# Разрешённые функции: всё, чего здесь нет, вызвать нельзя
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
          "log": math.log, "sin": math.sin, "cos": math.cos}


def _eval(node):
    """Рекурсивно вычисляет узел дерева; на всё неразрешённое бросает ValueError."""
    if isinstance(node, ast.Expression):          # корень дерева
        return _eval(node.body)
    # Число (bool сюда тоже попадёт как подкласс int, на результат это не влияет)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    # Бинарная операция: a + b, a ** b и т. д.
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        # Защита от 9**9**9: такая степень вешает процесс на минуты
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("слишком большая степень")
        return _OPS[type(node.op)](left, right)
    # Унарная операция: -x
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    # Вызов функции: только по имени из белого списка и без именованных аргументов
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS and not node.keywords):
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("недопустимое выражение")


def calculator(expression: str) -> str:
    """Вычисляет арифметическое выражение.
    Поддерживает + - * / // % **, скобки и функции sqrt, abs, round, min, max, log, sin, cos."""
    try:
        # mode="eval" разбирает ровно одно выражение (не операторы и не импорты)
        return str(_eval(ast.parse(expression.strip(), mode="eval")))
    except Exception as e:
        return f"Ошибка вычисления: {e}"


# ======================================================================
# 2 run_python: запуск кода в отдельном процессе
# ======================================================================
def run_python(code: str) -> str:
    """Выполняет Python-код и возвращает всё, что он напечатал через print().
    Используй для циклов, списков, статистики, обработки данных. Результат обязательно выводи print()."""
    # Каждый запуск идёт во временной папке, она удаляется при выходе из with
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")
        # Урезанное окружение: чужие переменные (ключи, токены) в код не попадают.
        # SYSTEMROOT нужен Python на Windows, без него процесс не стартует.
        env = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        try:
            p = subprocess.run(
                # sys.executable: тот же интерпретатор, что и у проекта (из .venv uv),
                # поэтому установленные через uv add пакеты доступны коду.
                # -I: изолированный режим, игнорирует пользовательские настройки Python.
                [sys.executable, "-I", str(script)], cwd=tmp, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10)  # защита от бесконечных циклов
        except subprocess.TimeoutExpired:
            return "Ошибка: превышено время выполнения (10 с)."
    out = p.stdout
    if p.returncode != 0:
        # Хвост stderr содержит саму ошибку: по нему модель может исправить код
        out += "\nОшибка выполнения:\n" + p.stderr[-800:]
    # Частая ошибка маленьких моделей: посчитали, но забыли print. Говорим об этом прямо.
    out = out.strip() or "(код выполнился, но ничего не напечатал; используй print)"
    return out[:MAX_OUT]


# ======================================================================
# 3 search_docs: поиск по документам (BM25 без нейросетей)
# ======================================================================
# BM25 это классическая формула ранжирования: фрагмент тем релевантнее запросу,
# чем чаще в нём встречаются слова запроса, и тем сильнее эти слова, чем реже они
# встречаются в остальной базе (редкое слово весит больше частого).

def _tokens(text: str) -> list[str]:
    # Грубый стемминг: берём первые 5 символов слова, чтобы "отпуск", "отпуска",
    # "отпуске" давали один токен. Для русской морфологии это приближение,
    # зато без внешних библиотек и работает и для английского.
    return [w[:5] for w in re.findall(r"\w+", text.lower())]


@lru_cache(maxsize=1)  # индекс строится один раз за запуск, дальше берётся из кэша
def _index():
    """Режет документы на абзацы и считает статистику слов для BM25."""
    chunks = []  # элемент: (имя файла, номер абзаца, текст, счётчик слов)
    for f in sorted(DOCS_DIR.glob("*")):
        if f.suffix.lower() not in (".md", ".txt"):
            continue
        # Фрагмент = абзац (разделитель: пустая строка)
        for i, para in enumerate(re.split(r"\n\s*\n", f.read_text(encoding="utf-8"))):
            if para.strip():
                chunks.append((f.name, i, para.strip(), Counter(_tokens(para))))
    # df: в скольких фрагментах встречается каждое слово
    df = Counter(t for c in chunks for t in c[3])
    # Средняя длина фрагмента (в токенах), нужна для нормализации по длине
    avg = sum(sum(c[3].values()) for c in chunks) / max(len(chunks), 1)
    return chunks, df, avg


def search_docs(query: str, top_k: int = 3) -> str:
    """Ищет в базе документов компании (кадровая политика, релизы, бюджет) и возвращает
    самые релевантные фрагменты. query — короткий поисковый запрос по смыслу, top_k — сколько фрагментов."""
    try:
        # Модель может передать top_k строкой или числом вне диапазона: приводим к 1..5
        top_k = max(1, min(int(top_k), 5))
        chunks, df, avg = _index()
        if not chunks:
            return "Ошибка: база документов пуста."
        n = len(chunks)
        k1, b = 1.5, 0.75   # стандартные параметры BM25: насыщение частоты и учёт длины
        q = _tokens(query)
        scored = []
        for name, i, text, tf in chunks:
            ln = sum(tf.values())   # длина этого фрагмента в токенах
            s = 0.0
            for t in q:
                if t in tf:
                    # idf: вес слова тем больше, чем реже оно в базе
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    # вклад слова: растёт с частотой, но с насыщением; длинные фрагменты штрафуются
                    s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * ln / avg))
            scored.append((s, name, i, text))
        # Берём лучшие top_k и отбрасываем фрагменты с нулевым счётом (нет общих слов)
        hits = [h for h in sorted(scored, reverse=True)[:top_k] if h[0] > 0]
        if not hits:
            return "Ничего не найдено."
        # В ответ добавляем источник: модель сможет сослаться на файл
        return "\n\n".join(f"[{name}, фрагмент {i}]\n{text}" for _, name, i, text in hits)
    except Exception as e:
        return f"Ошибка поиска: {e}"


# ======================================================================
# 4 http_get: HTTP-запрос с защитой от обращения к внутренним адресам
# ======================================================================
# Угроза называется SSRF: модель (или инъекция в тексте страницы) может заставить
# программу обратиться к localhost или внутренней сети. Поэтому перед запросом
# проверяем, куда разрешается имя хоста.

# Диапазон, который прокси/VPN в режиме fake-ip выдают вместо настоящих адресов.
# Python считает его приватным, и без исключения блокировались бы вообще все запросы.
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _check_url(url: str) -> str | None:
    """Возвращает текст ошибки, если URL небезопасен, и None, если всё в порядке."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "нужен полный URL вида https://..."
    try:
        # У хоста может быть несколько адресов: проверяем каждый
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip in _FAKE_IP_NET:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return "обращение к локальным и внутренним адресам запрещено"
    except socket.gaierror as e:
        return f"не удалось разрешить имя хоста: {e}"
    return None


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Перед переходом по редиректу проверяет новый адрес: иначе внешний сайт мог бы
    перенаправить запрос на внутренний адрес в обход первой проверки."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        err = _check_url(newurl)
        if err:
            raise urllib.error.URLError(f"редирект отклонён: {err}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str) -> str:
    """Выполняет HTTP GET-запрос по полному URL (https://...) и возвращает статус и тело ответа.
    Подходит для открытых API без ключа, которые отдают JSON или текст."""
    err = _check_url(url)
    if err:
        return f"Ошибка: {err}"
    # Некоторые API отклоняют запросы без User-Agent
    req = urllib.request.Request(url, headers={"User-Agent": "llm-lab/1.0",
                                               "Accept": "application/json, text/*"})
    opener = urllib.request.build_opener(_SafeRedirect)
    try:
        with opener.open(req, timeout=15) as r:
            # Читаем с запасом (MAX_OUT*4 байт), чтобы не качать огромные страницы целиком
            body = r.read(MAX_OUT * 4 + 1).decode(r.headers.get_content_charset() or "utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:   # 404, 500 и т. п.: сообщаем код модели
        return f"HTTP {e.code}: {e.reason}"
    except Exception as e:                # таймаут, нет сети, ошибка DNS
        return f"Ошибка запроса: {e}"
    return f"HTTP {status}\n{body[:MAX_OUT]}" + ("\n... (обрезано)" if len(body) > MAX_OUT else "")


# Список функций уходит в ollama.chat(tools=TOOLS); словарь нужен, чтобы по имени,
# которое вернула модель, найти и вызвать нужную функцию
TOOLS = [run_python, calculator, search_docs, http_get]
REGISTRY = {f.__name__: f for f in TOOLS}