import sys

import ollama

from tools import REGISTRY, TOOLS

# Модель берём из аргумента командной строки: uv run main.py qwen3.5:0.8b
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:0.8b"
# Предохранитель: слабая модель может бесконечно вызывать инструменты
MAX_STEPS = 6
# temperature=0 и фиксированный seed нужны для воспроизводимости замеров
OPTIONS = {"temperature": 1, "seed": 42}


def run(question: str) -> str:
    """Агентский цикл: спросили модель -> она просит инструмент -> выполнили ->
    вернули результат -> снова спросили, пока не придёт обычный текстовый ответ."""
    messages = [{"role": "user", "content": question}]
    for _ in range(MAX_STEPS):
        r = ollama.chat(model=MODEL, messages=messages, tools=TOOLS, options=OPTIONS)
        messages.append(r.message)          # ответ модели (с вызовами) идёт в историю
        if not r.message.tool_calls:        # инструменты не нужны: это финальный ответ
            return r.message.content or ""
        for call in r.message.tool_calls:   # модель может запросить несколько вызовов сразу
            name, args = call.function.name, call.function.arguments
            fn = REGISTRY.get(name)         # модель может выдумать несуществующий инструмент
            try:
                result = fn(**args) if fn else f"Ошибка: инструмента {name} нет."
            except TypeError as e:          # неверные или лишние аргументы
                result = f"Ошибка аргументов: {e}"
            print(f"  -> {name}({args}) = {result[:120]!r}")   # трассировка для отладки
            # Результат инструмента возвращаем модели отдельным сообщением с ролью tool
            messages.append({"role": "tool", "tool_name": name, "content": result})
    return "(лимит шагов исчерпан)"


if __name__ == "__main__":
    # По одному случаю на инструмент, одна цепочка и один контрольный вопрос
    questions = [
        "Посчитай (17.5*4 + 3)**2 / 7",                       # calculator
        "Выведи первые 15 чисел Фибоначчи и их сумму.",         # run_python
        "Сколько дней отпуска положено сотруднику?",            # search_docs
        "Сделай GET-запрос на https://api.open-meteo.com/v1/forecast"
        "?latitude=55.75&longitude=37.62&current=temperature_2m и скажи температуру.",  # http_get
        "Найди в документах бюджет на закупку оборудования и посчитай 12% от него.",  # цепочка: search_docs -> calculator
        "Назови столицу Франции.",                              # контроль: инструмент не нужен
    ]
    #questions = ["Выведи первые 15 чисел фибоначчи и их сумму"]
    for q in questions:
        print("\nQ:", q)
        print("A:", run(q))