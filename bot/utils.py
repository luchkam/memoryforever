from __future__ import annotations

import glob
import os


def cleanup_uploads_folder():
    """Очистка папки uploads: оставляем не больше 10 файлов каждого типа"""
    # Очистка входящих фото (паттерн: цифры_цифры_hex.jpg)
    user_photos = glob.glob("uploads/*_*_*.jpg")
    user_photos = [f for f in user_photos if not f.startswith("uploads/start")]
    if len(user_photos) > 20:
        # Сортируем по времени модификации (новые первыми)
        user_photos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        # Удаляем старые файлы (оставляем только 10 новейших)
        for old_file in user_photos[10:]:
            try:
                os.remove(old_file)
                print(f"[CLEANUP] Удален старый входящий файл: {old_file}")
            except Exception as e:
                print(f"[CLEANUP] Ошибка удаления {old_file}: {e}")

    # Очистка стартовых кадров (паттерн: start_дата_время_hex.png/jpg)
    start_frames = glob.glob("uploads/start_*.png") + glob.glob("uploads/start_*.jpg") + glob.glob("uploads/startframe_*.jpg")
    if len(start_frames) > 20:
        # Сортируем по времени модификации (новые первыми)
        start_frames.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        # Удаляем старые файлы (оставляем только 10 новейших)
        for old_file in start_frames[10:]:
            try:
                os.remove(old_file)
                print(f"[CLEANUP] Удален старый стартовый кадр: {old_file}")
            except Exception as e:
                print(f"[CLEANUP] Ошибка удаления {old_file}: {e}")
