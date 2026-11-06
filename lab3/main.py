import gradio as gr
import numpy as np
import pandas as pd
from ultralytics import YOLO

MODEL_NAME = "road_damage_best.pt"   
model = YOLO(MODEL_NAME)

NAME2ID = {name: i for i, name in model.names.items()}
CLASS_CHOICES = list(NAME2ID)


def detect(image, classes, max_per_class, conf):
    """Обработчик кнопки: принимает картинку и параметры из интерфейса,
    возвращает картинку с рамками и таблицу найденных объектов."""

    # Проверки ввода: gr.Error показывает пользователю сообщение в интерфейсе, а не падает с трассировкой
    if image is None:
        raise gr.Error("Загрузите изображение")
    if not classes:
        raise gr.Error("Выберите хотя бы один класс")

    # Gradio отдаёт RGB, ultralytics для numpy ждёт BGR; [..., ::-1] переставляет каналы
    bgr = image[..., ::-1]
    result = model.predict(
        bgr,
        conf=conf,                                # порог уверенности: слабее этого рамки отбрасываются
        classes=[NAME2ID[c] for c in classes],    # названия из чекбоксов -> номера классов; остальные классы модель игнорирует
        verbose=False,                            # не печатать лог в консоль на каждый запрос
    )[0]                                          # predict возвращает список (по одному элементу на картинку), берём первый

    # Классы и уверенности всех найденных рамок как numpy-массивы
    # (.cpu() нужен, если модель работала на GPU: numpy не умеет читать тензоры из видеопамяти)
    cls = result.boxes.cls.cpu().numpy().astype(int)
    scores = result.boxes.conf.cpu().numpy()

    # Лимит на класс: у YOLO такого параметра нет, поэтому делаем вручную.
    # Для каждого класса оставляем max_per_class рамок с наибольшей уверенностью.
    keep = []
    for c in np.unique(cls):
        idx = np.where(cls == c)[0]                                   # индексы рамок этого класса
        idx = idx[np.argsort(-scores[idx])][: int(max_per_class)]     # сортировка по убыванию уверенности + обрезка до N
        keep.extend(idx.tolist())

    # Ничего не найдено (или всё отсеяно): возвращаем исходную картинку и пустую таблицу с заголовками
    if not keep:
        return image, pd.DataFrame(columns=["класс", "уверенность", "x1", "y1", "x2", "y2"])

    keep.sort()                        # возвращаем исходный порядок рамок
    result = result[keep]              # оставляем в результате только отобранные рамки
    annotated = result.plot()[..., ::-1]  # рисуем рамки и подписи; plot() отдаёт BGR, переводим обратно в RGB для Gradio

    # Координаты рамок в пикселях: левый верхний (x1, y1) и правый нижний (x2, y2) углы
    xyxy = result.boxes.xyxy.cpu().numpy().round().astype(int)
    table = pd.DataFrame({
        "класс": [model.names[int(c)] for c in result.boxes.cls.cpu().numpy()],   # номер класса -> название
        "уверенность": result.boxes.conf.cpu().numpy().round(3),
        "x1": xyxy[:, 0], "y1": xyxy[:, 1], "x2": xyxy[:, 2], "y2": xyxy[:, 3],
    })
    return annotated, table            # порядок совпадает с outputs в btn.click: [out_img, out_tbl]


with gr.Blocks(title="YOLOv12 Detection") as demo:
    gr.Markdown("## Детекция объектов — YOLOv12")
    with gr.Row():
        with gr.Column():
            img = gr.Image(type="numpy", label="Изображение")
            classes = gr.CheckboxGroup(
                CLASS_CHOICES, value=CLASS_CHOICES, label="Классы для детекции"
            )
            max_per_class = gr.Slider(1, 100, value=20, step=1,
                                      label="Макс. объектов на каждый класс")
            conf = gr.Slider(0.06, 1.0, value=0.30, step=0.05,
                             label="Минимальная уверенность")
            btn = gr.Button("Детектировать", variant="primary")
        with gr.Column():
            out_img = gr.Image(label="Результат")
            out_tbl = gr.Dataframe(label="Найденные объекты")

    btn.click(detect, [img, classes, max_per_class, conf], [out_img, out_tbl])

if __name__ == "__main__":
    demo.launch()