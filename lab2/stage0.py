import ollama

MODELS = ["lfm2.5-thinking:1.2b", "qwen3.5:0.8b"]  
PROMPT = "Как думаешь, универсалии могут существовать как сущности конкретных вещей, или они являются лишь порождением разума?"


def describe(name: str) -> None:
    info = ollama.show(name)
    caps = getattr(info, "capabilities", None)
    ctx = {k: v for k, v in (info.modelinfo or {}).items() if k.endswith("context_length")}
    print(f"\n=== {name} ===")
    print("размер:", info.details.parameter_size, "| квантизация:", info.details.quantization_level)
    print("capabilities:", caps)
    print("контекст (из метаданных):", ctx)


def speed_test(name: str) -> None:
    r = ollama.chat(
        model=name,
        messages=[{"role": "user", "content": PROMPT}],
        options={"temperature": 1, "seed": 42},
    )
    tps = r.eval_count / (r.eval_duration / 1e9)
    print("ответ:", r.message.content.strip())
    if getattr(r.message, "thinking", None):
        print("(модель выдала thinking)")
    print(f"скорость: {tps:.1f} ток/с, токенов: {r.eval_count}")


if __name__ == "__main__":
    for m in MODELS:
        try:
            describe(m)
            speed_test(m)
        except Exception as e:
            print(f"{m}: ошибка {e}")